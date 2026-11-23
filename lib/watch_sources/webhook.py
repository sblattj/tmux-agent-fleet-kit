"""WATCH phase 6: optional loopback webhook receiver (docs/design/four-desks.md section 3.5).

The receiver listens on 127.0.0.1 (or ::1) only. The human exposes it through their own tunnel. Each `webhook` source
in watch.json is one route, POST /hook/<name>:

    {"name": "oss-hook", "kind": "webhook", "for": "oss", "signature": "github",
     "auth": {"type": "hmac", "secret": "env:OSS_HOOK_SECRET"}, "bind": "127.0.0.1", "port": 7349, "pollSec": 60}

  pollSec is how often the engine drains this route's spool, so it is the webhook's latency: keep it at the 60 s
  floor (the default 900 would hold a delivery for up to 15 min).

- `for` names the polled source the deliveries belong to (here the github source "oss"). Omit it for a generic route
  whose body is Event-shaped JSON (one object, a list, or JSON lines), like the `command` source.
- `signature`: "github" checks X-Hub-Signature-256 (sha256=<hex HMAC of the raw body>). "hmac" is the generic check
  for every other vendor: `header` (default X-Signature-256), `prefix` (default "sha256="), `algo` (sha256|sha1|sha512).
  "basic" is HTTP basic auth (Azure DevOps service hooks): auth {"type": "basic", "user": <name or ref>,
  "secret": <ref>}, compared in constant time; a request with no Authorization header is refused.
  An adapter with a vendor-specific scheme registers its own: register_verifier("bitbucket", fn), where
  fn(secret, headers, body, route) raises SourceError(status=401) on a mismatch.
- The secret is a reference (env:, keychain:, cmd:) in the auth block, resolved per request, held in memory only, and
  never logged or echoed.

Layering (ROUTE ruling): this receiver is the ONLY place signatures are checked. It resolves auth.secret itself and
verifies per `signature` before anything parses the body. The target adapter's accept_webhook(headers, body) is
called for normalization only, with no secret in its creds; adapters never re-verify.

A delivery is refused (401) unless its signature checks out over the raw body bytes. A good delivery is normalized
by the target adapter's accept_webhook (the same mapping, and so the same eid, as its poll), or by the generic Event
path, and appended to the route's spool $FLEET_STATE/watch/webhook/<name>.jsonl. The engine polls this `webhook`
source like any other: poll() drains the spool (cursor = byte offset) into the normal pipeline, so seen.db dedupe on
the eid makes webhook plus poll yield one event (the poll becomes a catch-up).

Python 3.8+ stdlib only. CLI: python3 lib/watch_sources/webhook.py serve [--config watch.json]
"""
import argparse
import base64
import datetime
import fcntl
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import threading

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_watch_contract as C  # noqa: E402

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = C.WEBHOOK_PORT  # reserved set: 7340 decisions, 7341 learn, 7342 watch page, 7349 this receiver
MAX_BODY = 1024 * 1024  # bytes; GitHub caps payloads at 25 MB, but nothing a desk needs comes near 1 MB
READ_TIMEOUT_SEC = 10
SECRET_CMD_TIMEOUT_SEC = 10
SPOOL_ROTATE_BYTES = 1024 * 1024  # a fully drained spool larger than this is truncated
ALGOS = ("sha256", "sha1", "sha512")
# fields the pipeline owns (filters, scrub, burst collapse); a sender cannot set them
PIPELINE_FIELDS = ("kind", "to", "score", "secretDropped", "count", "origin")


class BindRefused(ValueError):
    """The receiver was asked to bind a non-loopback address."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_dir():
    s = os.environ.get("FLEET_STATE")
    if s:
        return s
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fleet")


def spool_path(state, name):
    if not name or "/" in name or name.startswith("."):
        raise ValueError("bad webhook route name %r" % name)
    return os.path.join(C.state_paths(state)["dir"], "webhook", name + ".jsonl")


# --- bind: loopback only -------------------------------------------------------------------------------------------
def check_bind(host):
    """Return the literal loopback address to bind, or raise BindRefused. Only 127.0.0.0/8, ::1 and 'localhost'
    (mapped to 127.0.0.1) pass; '', 0.0.0.0, ::, LAN addresses and other hostnames are refused before any socket."""
    if host == "localhost":
        return "127.0.0.1"
    h = (host or "").strip("[]")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        raise BindRefused("bind %r refused: the webhook receiver binds a loopback address only (127.0.0.1 or ::1)"
                          % (host,))
    if not ip.is_loopback:
        raise BindRefused("bind %r refused: the webhook receiver binds a loopback address only (127.0.0.1 or ::1)"
                          % (host,))
    return str(ip)


# --- secrets -------------------------------------------------------------------------------------------------------
def resolve_secret(ref):
    """Resolve an env:/keychain:/cmd: reference. Raises ValueError (never echoing a value) when it does not resolve."""
    scheme, arg = C.parse_ref(ref)
    val = None
    if scheme == "env":
        val = os.environ.get(arg)
    elif scheme == "keychain":
        argv = (["security", "find-generic-password", "-s", arg, "-w"] if sys.platform == "darwin"
                else ["secret-tool", "lookup", "service", arg])
        val = _first_line(argv)
    elif scheme == "cmd":
        val = _first_line(shlex.split(arg))
    if not val:
        raise ValueError("secret reference %s:... did not resolve" % scheme)
    return val


def _first_line(argv):
    try:
        out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             timeout=SECRET_CMD_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    lines = out.stdout.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else None


# --- signatures ----------------------------------------------------------------------------------------------------
def _header(headers, name):
    """Case-insensitive header lookup over a dict or an http.client.HTTPMessage."""
    want = name.lower()
    items = headers.items() if hasattr(headers, "items") else headers
    for k, v in items:
        if k.lower() == want:
            return v
    return None


def _check(secret, body, got, prefix, algo):
    if algo not in ALGOS:
        raise C.SourceError("unsupported signature algorithm", status=500)
    if not secret:
        raise C.SourceError("no webhook secret", status=401)
    if not isinstance(body, (bytes, bytearray)):
        raise C.SourceError("signature check needs the raw body bytes", status=500)
    if not got or not got.startswith(prefix):
        raise C.SourceError("signature missing or malformed", status=401)
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    want = prefix + hmac.new(key, bytes(body), getattr(hashlib, algo)).hexdigest()
    if not hmac.compare_digest(want.encode("ascii"), got.strip().encode("ascii", "replace")):
        raise C.SourceError("signature mismatch", status=401)


def verify_github(secret, headers, body):
    """GitHub: X-Hub-Signature-256 = 'sha256=' + hex HMAC-SHA256(secret, raw body). Raises SourceError(401)."""
    _check(secret, body, _header(headers, "X-Hub-Signature-256"), "sha256=", "sha256")


def verify_hmac(secret, headers, body, header="X-Signature-256", prefix="sha256=", algo="sha256"):
    """Generic vendor HMAC over the raw body. Raises SourceError(401)."""
    _check(secret, body, _header(headers, header), prefix, algo)


def sign(secret, body, prefix="sha256=", algo="sha256"):
    """The header value a sender computes; for tests and for `curl` checks of a tunnel."""
    return prefix + hmac.new(secret.encode("utf-8"), body, getattr(hashlib, algo)).hexdigest()


def verify_basic(user, secret, headers):
    """HTTP basic auth: Authorization: Basic base64(user:secret), constant-time. Raises SourceError(401)."""
    got = _header(headers, "Authorization") or ""
    if not user or not secret:
        raise C.SourceError("no webhook credentials", status=401)
    if not got.startswith("Basic "):
        raise C.SourceError("basic auth missing", status=401)
    try:
        u, _, pw = base64.b64decode(got[6:].strip(), validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        raise C.SourceError("basic auth malformed", status=401)
    ok_u = hmac.compare_digest(u.encode("utf-8"), user.encode("utf-8"))
    ok_p = hmac.compare_digest(pw.encode("utf-8"), secret.encode("utf-8"))
    if not (ok_u and ok_p):
        raise C.SourceError("basic auth mismatch", status=401)


# signature name -> fn(secret, headers, body, route); raises SourceError(status=401) on a mismatch
VERIFIERS = {
    "basic": lambda secret, headers, body, route: verify_basic(route.get("_user"), secret, headers),
    "github": lambda secret, headers, body, route: verify_github(secret, headers, body),
    "hmac": lambda secret, headers, body, route: verify_hmac(
        secret, headers, body, route.get("header", "X-Signature-256"), route.get("prefix", "sha256="),
        route.get("algo", "sha256")),
}


def register_verifier(name, fn):
    """Add a vendor signature scheme (e.g. an adapter's verify_bitbucket). Names are unique; github/hmac are fixed."""
    if not isinstance(name, str) or not name or not callable(fn):
        raise ValueError("register_verifier needs a name and a callable")
    if name in VERIFIERS and VERIFIERS[name] is not fn:
        raise ValueError("signature %r is already registered" % name)
    VERIFIERS[name] = fn


def verify_route(route, secret, headers, body):
    fn = VERIFIERS.get(route.get("signature", "github"))
    if fn is None:
        raise C.SourceError("unknown signature type", status=500)
    fn(secret, headers, body, route)


# --- config --------------------------------------------------------------------------------------------------------
def routes_from_config(cfg):
    """{name: webhook source entry} plus errors. A webhook route needs an hmac auth block with a secret reference."""
    errs = []
    routes = {}
    by_name = {s.get("name"): s for s in cfg.get("sources", []) if isinstance(s, dict)}
    for s in cfg.get("sources", []):
        if not isinstance(s, dict) or s.get("kind") != "webhook":
            continue
        n = s.get("name")
        where = "sources[%s]" % n
        try:
            spool_path("/", n)
        except ValueError:
            errs.append("%s: name must be a plain file name" % where)
            continue
        tgt = s.get("for")
        if tgt is not None:
            t = by_name.get(tgt)
            if t is None:
                errs.append("%s.for names no source %r" % (where, tgt))
            elif t.get("kind") == "webhook":
                errs.append("%s.for must name a polled source, not another webhook" % where)
            else:
                _preload(t.get("kind"))  # its module may register_verifier() at import
        sig = s.get("signature", "github")
        if sig not in VERIFIERS:
            errs.append("%s.signature must be one of %s" % (where, ", ".join(sorted(VERIFIERS))))
        if s.get("algo", "sha256") not in ALGOS:
            errs.append("%s.algo must be one of %s" % (where, ", ".join(ALGOS)))
        auth = s.get("auth")
        want = "basic" if sig == "basic" else "hmac"
        if not isinstance(auth, dict) or auth.get("type") != want or not C.is_credential_ref(auth.get("secret")):
            errs.append("%s needs auth {type: %s, secret: env:|keychain:|cmd: reference}" % (where, want))
        elif want == "basic" and (not isinstance(auth.get("user"), str) or not auth.get("user")):
            errs.append("%s needs auth.user for basic" % where)
        try:
            check_bind(s.get("bind", DEFAULT_BIND))
        except BindRefused as e:
            errs.append("%s: %s" % (where, e))
        routes[n] = s
    binds = {(check_bind_or_none(s.get("bind", DEFAULT_BIND)), s.get("port", DEFAULT_PORT)) for s in routes.values()}
    if len(binds) > 1:
        errs.append("webhook sources disagree on bind/port; one receiver serves them all")
    return routes, errs


def _preload(kind):
    try:
        C.load_source(kind)
    except (KeyError, ImportError):
        pass


def check_bind_or_none(h):
    try:
        return check_bind(h)
    except BindRefused:
        return None


# --- normalize -----------------------------------------------------------------------------------------------------
def _generic_events(body, source_name, source_kind):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise C.SourceError("body is not UTF-8", status=400)
    try:
        doc = json.loads(text)
        rows = doc if isinstance(doc, list) else [doc]
    except ValueError:
        try:
            rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        except ValueError:
            raise C.SourceError("body is not JSON or JSON lines", status=400)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise C.SourceError("event is not an object", status=400)
        r = {k: v for k, v in r.items() if k not in PIPELINE_FIELDS}
        r["source"] = source_name
        if source_kind:
            if r.get("sourceKind") not in (None, source_kind):
                raise C.SourceError("event sourceKind does not match the route's source", status=400)
            r["sourceKind"] = source_kind
        errs = C.validate_event(r)
        if errs:
            raise C.SourceError("event invalid: %s" % "; ".join(errs), status=400)
        out.append(C.Event.from_dict(r))
    return out


def normalize(route, target, headers, body, cfg=None):
    """Events for one verified delivery. `target` is the polled source entry the route is `for` (or None). The adapter
    is built with the engine's source_cfg_with_me(cfg, target) and EMPTY creds: the receiver already verified the
    signature, and accept_webhook only normalizes (ROUTE ruling), so the secret never reaches an adapter."""
    if target is None:
        return _generic_events(body, route["name"], None)
    kind = target.get("kind")
    adapter = None
    try:
        adapter = C.load_source(kind)
    except (KeyError, ImportError):
        adapter = None
    if adapter is not None:
        inst = adapter(C.source_cfg_with_me(cfg or {}, target), {}, {})
        try:
            events = inst.accept_webhook(headers, body)
        except NotImplementedError:
            events = None
        if events is not None:
            out = []
            for e in events:
                if e.source != target["name"] or e.sourceKind != kind:
                    raise C.SourceError("adapter returned an event for another source", status=500)
                out.append(e)
            return out
    if kind == "command":  # the escape hatch: Event-shaped bodies, same as its stdout
        return _generic_events(body, target["name"], None)
    raise C.SourceError("source kind %s has no webhook mapping yet" % kind, status=501)


# --- spool ---------------------------------------------------------------------------------------------------------
def spool_append(state, name, events):
    p = spool_path(state, name)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            for e in events:
                f.write(e.to_json() + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def spool_drain(state, name, cursor, limit):
    """(events, new_cursor) from byte offset `cursor`, at most `limit` events, whole lines only. A fully drained spool
    over SPOOL_ROTATE_BYTES is truncated and the cursor resets to 0."""
    p = spool_path(state, name)
    if not os.path.exists(p):
        return [], 0
    pos = cursor if isinstance(cursor, int) and cursor >= 0 else 0
    events = []
    with open(p, "r+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            size = os.fstat(f.fileno()).st_size
            if pos > size:  # truncated underneath us
                pos = 0
            f.seek(pos)
            while len(events) < limit:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break
                pos += len(line)
                try:
                    events.append(C.Event.from_dict(json.loads(line.decode("utf-8"))))
                except (ValueError, TypeError):
                    continue  # a damaged line is skipped, never fatal
            if pos >= size and size > SPOOL_ROTATE_BYTES:
                f.truncate(0)
                pos = 0
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return events, pos


# --- Source --------------------------------------------------------------------------------------------------------
class WebhookSource(C.Source):
    """The `webhook` kind as the engine sees it: poll() drains this route's spool. No network."""
    kind = "webhook"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self._h = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None}

    def poll(self, cursor):
        limit = int(self.cfg.get("maxEventsPerPoll", C.DEFAULTS["maxEventsPerPoll"]))
        try:
            events, cur = spool_drain(self.cfg.get("_state") or state_dir(), self.cfg["name"], cursor, limit)
        except OSError as e:
            self._h["lastError"] = "spool unreadable: %s" % (e.strerror or e)
            self._h["failStreak"] += 1
            raise C.SourceError(self._h["lastError"])
        self._h.update(lastOk=_now(), lastError=None, failStreak=0)
        return events, cur, None

    def health(self):
        return dict(self._h)


# --- receiver ------------------------------------------------------------------------------------------------------
class Receiver:
    """Loopback HTTP receiver for every webhook route in one watch.json. `resolver` turns a secret reference into the
    secret (default resolve_secret); tests inject one."""

    def __init__(self, cfg, state=None, bind=None, port=None, resolver=resolve_secret, log=None):
        self.cfg = cfg
        self.routes, errs = routes_from_config(cfg)
        if errs:
            raise ValueError("; ".join(errs))
        by_name = {s.get("name"): s for s in cfg.get("sources", []) if isinstance(s, dict)}
        self.targets = {n: by_name.get(r.get("for")) for n, r in self.routes.items()}
        first = next(iter(self.routes.values()), {})
        self.bind = check_bind(bind if bind is not None else first.get("bind", DEFAULT_BIND))
        self.port = port if port is not None else first.get("port", DEFAULT_PORT)
        self.state = state or state_dir()
        self.resolver = resolver
        self.log = log or (lambda line: sys.stderr.write(line + "\n"))
        self.srv = None

    def handle(self, method, path, headers, body):
        """(status, response dict). Pure: no socket, so tests can call it directly too."""
        if method != "POST":
            return 405, {"error": "POST only"}
        parts = path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "hook" or parts[1] not in self.routes:
            return 404, {"error": "no such route"}
        name = parts[1]
        route = self.routes[name]
        try:
            secret = self.resolver(route["auth"]["secret"])
            user = route["auth"].get("user")
            if user and C.is_credential_ref(user):
                user = self.resolver(user)
        except (ValueError, KeyError):
            return 503, {"error": "route secret unavailable"}
        route = dict(route, _user=user)
        try:
            verify_route(route, secret, headers, body)
        except C.SourceError as e:
            return e.status or 401, {"error": "refused: %s" % e}
        if route.get("signature", "github") == "github" and (_header(headers, "X-GitHub-Event") or "") == "ping":
            return 200, {"accepted": 0, "ping": True}
        try:
            events = normalize(route, self.targets[name], headers, body, self.cfg)
        except C.SourceError as e:
            return e.status or 400, {"error": str(e)}
        spool_append(self.state, name, events)
        return 202, {"accepted": len(events)}

    def start(self):
        """Bind and serve in a daemon thread; returns the bound (host, port)."""
        rx = self
        fam = socket.AF_INET6 if ":" in self.bind else socket.AF_INET

        class Srv(http.server.ThreadingHTTPServer):
            address_family = fam
            daemon_threads = True

        class H(http.server.BaseHTTPRequestHandler):
            timeout = READ_TIMEOUT_SEC
            server_version = "fleet-watch-webhook"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass

            def _reply(self, code, obj):
                data = (json.dumps(obj) + "\n").encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                rx.log("webhook %s %s -> %d" % (self.command, self.path.split("?", 1)[0], code))

            def do_POST(self):
                n = self.headers.get("Content-Length")
                if n is None or not n.isdigit():
                    return self._reply(411, {"error": "Content-Length required"})
                if int(n) > MAX_BODY:
                    return self._reply(413, {"error": "body over %d bytes" % MAX_BODY})
                try:
                    body = self.rfile.read(int(n))
                except (socket.timeout, OSError):
                    return self._reply(408, {"error": "body read timed out"})
                code, obj = rx.handle("POST", self.path, self.headers, body)
                self._reply(code, obj)

            def _not_post(self):
                self._reply(405, {"error": "POST only"})

            do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_post

        self.srv = Srv((self.bind, self.port), H)
        host = self.srv.server_address[0]
        if not ipaddress.ip_address(host).is_loopback:  # belt and braces after the bind
            self.srv.server_close()
            raise BindRefused("bound %s, which is not loopback" % host)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return host, self.srv.server_address[1]

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet watch webhook", description="loopback webhook receiver (WATCH phase 6)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="serve every webhook source in watch.json on loopback")
    sp.add_argument("--config", help="watch.json (default $FLEET_STATE/watch.json)")
    sp.add_argument("--bind", help="loopback address (127.0.0.1 or ::1); anything else is refused")
    sp.add_argument("--port", type=int)
    a = ap.parse_args(argv)
    state = state_dir()
    path = a.config or C.state_paths(state)["config"]
    try:
        cfg = C.load_config(path)
        rx = Receiver(cfg, state, a.bind, a.port)
        if not rx.routes:
            raise ValueError("no webhook sources in %s" % path)
        host, port = rx.start()
    except (OSError, ValueError) as e:
        sys.stderr.write("fleet watch webhook: %s\n" % e)
        return 2
    sys.stdout.write("webhook receiver on http://%s:%d/hook/<name> (%s)\n" % (
        "[%s]" % host if ":" in host else host, port, ", ".join(sorted(rx.routes))))
    sys.stdout.flush()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        rx.stop()
    return 0


if __name__ == "__main__":
    # Run through the canonical module, so adapters' register_verifier() calls and this CLI share one VERIFIERS dict
    # (run as __main__, this file is a second copy with its own registry).
    from watch_sources import webhook as _canon  # noqa: E402
    sys.exit(_canon.main())
