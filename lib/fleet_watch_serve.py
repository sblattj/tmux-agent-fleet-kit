#!/usr/bin/env python3
"""fleet watch serve: the WATCH web page. A second front end on the WATCH desk's state, never a new source of truth.

Python 3.8+ stdlib only. Reads fleet_watch.snapshot() and calls the same verbs as the terminal status pane
(fleet_watch.poll_now, pause, resume, release_held), so a page action and the same pane key end in the same state.

  fleet watch serve [--port P] [--bind 127.0.0.1] [--open]   local web page (loopback + per-launch token)

Default port 7342 (decisions 7340, learn 7341). The URL, token included, goes to $FLEET_STATE/watch-ui.url (0600)
and stdout; the file is removed on exit.

HTTP (every request needs the token; a GET may carry it as ?t=, a POST only as the X-Fleet-Token header):
  GET  /              the page
  GET  /api/state     fleet_watch.snapshot(), checked by fleet_watch_contract.validate_snapshot
  POST /api/poll      {"source": name}   fleet_watch.poll_now      (pane key p)
  POST /api/pause     {"source": name}   fleet_watch.pause         (pane key z)
  POST /api/resume    {"source": name}   fleet_watch.resume        (pane key r)
  POST /api/release   {}                 fleet_watch.release_held  (pane key h)
"""
import argparse
import hmac
import http.server
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fleet_watch_contract as contract  # noqa: E402

HTML = os.path.join(HERE, "watch-ui.html")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 7342
URL_FILE = "watch-ui.url"
ROLES_OK = ("", "watch")

# page route -> (fleet_watch function name, takes a source name, pane key). One table, so the page can only reach
# what the pane can.
VERBS = {
    "/api/poll": ("poll_now", True, "p"),
    "/api/pause": ("pause", True, "z"),
    "/api/resume": ("resume", True, "r"),
    "/api/release": ("release_held", False, "h"),
}


def state_dir():
    s = os.environ.get("FLEET_STATE") or os.path.join(os.path.expanduser("~"), ".local", "state", "fleet")
    return os.path.abspath(s)


def load_watch():
    try:
        import fleet_watch  # lib/fleet_watch.py, owned by the WATCH core
    except ImportError as e:
        raise ValueError("the WATCH core (lib/fleet_watch.py) is not available: %s" % e)
    return fleet_watch


def role_guard(verb):
    role = os.environ.get("FLEET_ROLE") or ""
    if role not in ROLES_OK:
        raise PermissionError("role %s can't %s: only the human (or WATCH) runs the WATCH page" % (role, verb))


def _log(msg):
    sys.stderr.write("watch-ui %s\n" % msg)
    sys.stderr.flush()


def get_state(watch, state):
    snap = watch.snapshot(state)
    errs = contract.validate_snapshot(snap)
    if errs:
        raise RuntimeError("snapshot breaks the WATCH contract: %s" % "; ".join(errs[:5]))
    return snap


class UnknownSource(LookupError):
    pass


def act(watch, state, path, body):
    """Run the verb behind a page route. Returns a JSON-able result."""
    fn_name, takes_source, key = VERBS[path]
    fn = getattr(watch, fn_name)
    if takes_source:
        name = body.get("source")
        if not isinstance(name, str) or not name:
            raise ValueError("source is required")
        if name not in {s.get("name") for s in watch.snapshot(state).get("sources") or []}:
            raise UnknownSource(name)
        res = fn(state, name)
    else:
        res = fn(state)
    return {"ok": True, "verb": fn_name, "key": key, "result": res}


def make_handler(token, watch, state, allowed_hosts):
    with open(HTML, "rb") as f:
        page = f.read()

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "fleet-watch"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # never log the token (it rides in the query string)
            _log("%s %s" % (self.command, urllib.parse.urlsplit(self.path).path))

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, bytes):
                data = body
            else:
                data = (body if isinstance(body, str) else json.dumps(body, default=str)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype == "text/html":
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; img-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return (self.headers.get("Host") or "") in allowed_hosts

        def _token_ok(self, allow_query):
            got = self.headers.get("X-Fleet-Token") or ""
            if not got and allow_query:
                got = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t") or [""])[0]
            return bool(got) and hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8"))

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._host_ok() or not self._token_ok(allow_query=True):
                return self._send(403, {"error": "forbidden"})
            if path == "/":
                return self._send(200, page, "text/html")
            if path == "/api/state":
                try:
                    return self._send(200, get_state(watch, state))
                except Exception as e:
                    _log("state error: %s" % e)
                    return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self._host_ok() or not self._token_ok(allow_query=False) or (
                    origin and origin != "http://%s" % self.headers.get("Host")):
                return self._send(403, {"error": "forbidden"})
            path = urllib.parse.urlsplit(self.path).path
            if path not in VERBS:
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            try:
                res = act(watch, state, path, body)
            except UnknownSource as e:
                return self._send(404, {"error": "unknown source %s" % e})
            except (ValueError, PermissionError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                _log("%s error: %s" % (path, e))
                return self._send(500, {"error": str(e)})
            return self._send(200, res)

    return H


def serve(port, bind, open_page, out=sys.stdout):
    role_guard("serve the WATCH page")
    if bind not in LOOPBACK:
        raise ValueError("--bind %s refused: the WATCH page binds loopback only (127.0.0.1 or ::1)" % bind)
    watch = load_watch()
    state = state_dir()
    token = secrets.token_urlsafe(24)
    port = DEFAULT_PORT if port is None else port
    srv_cls = http.server.ThreadingHTTPServer
    if bind == "::1":
        class V6(srv_cls):
            address_family = socket.AF_INET6
        srv_cls = V6
    allowed = set()  # filled once the port is known
    try:
        srv = srv_cls(("localhost" if bind == "localhost" else bind, port), make_handler(token, watch, state, allowed))
    except OSError as e:
        raise ValueError("can't listen on %s:%s (%s); pick another --port" % (bind, port, e.strerror or e))
    srv.daemon_threads = True
    real_port = srv.server_address[1]
    for h in ("127.0.0.1", "localhost", "[::1]"):
        allowed.add("%s:%d" % (h, real_port))
    url = "http://%s:%d/?t=%s" % ("[::1]" if bind == "::1" else "127.0.0.1", real_port, token)
    urlfile = os.path.join(state, URL_FILE)
    os.makedirs(state, exist_ok=True)
    fd = os.open(urlfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(url + "\n")
    out.write("WATCH UI: %s\n" % url)
    out.flush()
    if open_page:
        opener = os.environ.get("FLEET_OPEN_CMD") or ("open -g" if sys.platform == "darwin" else "xdg-open")
        subprocess.Popen(shlex.split(opener) + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _term(signum, frame):  # SIGTERM (kill, tmux kill-pane) cleans up like Ctrl-C: the URL file goes away
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            os.remove(urlfile)
        except OSError:
            pass
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":  # tolerate `fleet_watch_serve.py serve ...` as well as bare flags
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="fleet watch serve", description="WATCH web page on loopback, with a token")
    ap.add_argument("--port", type=int, default=None, help="port (default %d; 0 picks a free one)" % DEFAULT_PORT)
    ap.add_argument("--bind", default="127.0.0.1", help="127.0.0.1 (default), ::1 or localhost")
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    a = ap.parse_args(argv)
    try:
        return serve(a.port, a.bind, a.open)
    except (ValueError, PermissionError) as e:
        sys.stderr.write("fleet watch serve: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
