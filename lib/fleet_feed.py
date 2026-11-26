#!/usr/bin/env python3
"""fleet feed: follow a live call transcript served by another machine into a local file WATCH reads (issue #37).

  fleet feed start URL --token-file PATH [--name N] [--protocol whosaid|lines] [--stem S] [--from-start]
                   [--since HH:MM:SS] [--min-chars 12] [--batch 60] [--poll 3] [--header X-Token]
                   [--parent LANE] [--bound 12h]
  fleet feed ls | stop N | status N

Python 3.8+ stdlib only. `start` checks the token file, writes $FLEET_STATE/feeds/<name>/config.json (no token, only
its path) and launches the poller detached under the side-run supervisor (`fleet side run --host feed --model <name>
-- python3 lib/fleet_feed.py _run --dir <feed dir>`), so it outlives the pane or tool call that started it and shows in
`fleet side ls`. The poller (`_run`) appends each new line to feeds/<name>/transcript.jsonl in a shape
fleet_listen.parse_utterance reads ({"clock","speaker","text","final"}), so `fleet listen --source file <that path>`
and the WATCH `call` source follow it unchanged. Its position is in feeds/<name>/state.json, advanced only after the
lines are appended, so a restart resumes and never re-sends a line.

Protocols (GET only, never a write):
  whosaid  `whosaid live serve`: GET /sessions, then GET /sessions/<stem>?cursor=C&wait=S (long poll), header
           `Authorization: Bearer <token>`. Newest session unless --stem; starts at its end unless --from-start (a
           session that appears after the feed started is read from its start). Dedupe on entry "n" per session.
           `meta.ended` -> status gone once, then wait for a newer stem. A name the voice match sets later on an
           earlier line is not rewritten: lines already written keep the speaker they had.
  lines    GET URL?from=N -> text lines `[HH:MM:SS] speaker: text` after the first N lines; token in --header
           (default X-Token). --since HH:MM:SS skips to the first line at or after that clock.

Status (ok | stalled (N failed polls) | auth refused | gone | stopped), once per change, goes to feeds/<name>/status, the
side-run log, and the transcript as {"note": "source: http-feed <status>"}, which fleet listen shows as a NOTE and never
routes. 401/403 (auth refused) and 400 (a bad stem, cursor or wait) stop the poller: the token is read only at start.
The token is never in argv, env, stdout, the log, status, state or the transcript.
"""
import argparse
import datetime
import fcntl
import http.client
import importlib.util
import json
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleet_mesh as mesh  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
PROTOCOLS = ("whosaid", "lines")
PLACEHOLDER = ("", "them")  # whosaid's "not named yet"; becomes "unknown" + unnamed
UNKNOWN = "unknown"
STALL_AFTER = 3  # failed polls in a row before `stalled`
LONG_WAIT = 25.0  # whosaid long poll (server cap 30)
HTTP_SLACK = 15.0  # client timeout = wait + this, so an idle long poll is never a failure
UNNAMED_NOTE = ("http-feed: a speaker the transcriber has not named yet shows as %r; a speaker filter cannot match "
                "those lines, so they stay visible but are routed only with no filter" % UNKNOWN)


class FeedError(Exception):
    """A refused request; the CLI prints FAIL <message> and exits 1."""


class Stop(Exception):
    def __init__(self, code=0):
        Exception.__init__(self, code)
        self.code = code


_listen = None


def _listen_mod():
    global _listen
    if _listen is None:
        spec = importlib.util.spec_from_file_location("fleet_listen", os.path.join(HERE, "fleet_listen.py"))
        _listen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_listen)
    return _listen


# ---------------------------------------------------------------- paths, token, url

def feeds_root():
    return os.path.join(mesh.state_dir(), "feeds")


def feed_dir(name):
    if not NAME_RE.match(name or ""):
        raise FeedError("bad feed name %r (letters, digits, . _ -)" % (name,))
    return os.path.join(feeds_root(), name)


def read_token(path):
    """The token from a regular file owned by this user, mode exactly 600."""
    p = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.stat(p)
    except OSError as e:
        raise FeedError("cannot read token file %s: %s" % (p, e.strerror or e))
    if not stat.S_ISREG(st.st_mode):
        raise FeedError("token file %s is not a regular file" % p)
    if st.st_uid != os.getuid():
        raise FeedError("token file %s is not owned by you" % p)
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise FeedError("token file %s is mode %o, refused: run chmod 600 %s" % (p, stat.S_IMODE(st.st_mode),
                                                                             shlex.quote(p)))
    try:
        with open(p) as f:
            tok = f.read().strip()
    except OSError as e:
        raise FeedError("cannot read token file %s: %s" % (p, e.strerror or e))
    if not tok or any(c.isspace() for c in tok):
        raise FeedError("token file %s must hold one token on one line" % p)
    return tok


def check_url(url):
    u = urllib.parse.urlsplit(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FeedError("bad URL (want http:// or https://host[:port][/path])")
    if u.username or u.password:
        raise FeedError("credentials in the URL are refused: put the token in --token-file")
    return url


def safe_url(url):
    """scheme://host[:port]/path: never the query or userinfo."""
    u = urllib.parse.urlsplit(url or "")
    host = u.hostname or ""
    if ":" in host:
        host = "[%s]" % host
    if u.port:
        host += ":%d" % u.port
    return "%s://%s%s" % (u.scheme, host, u.path)


def _with_query(url, **kw):
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k not in kw]
    q += [(k, str(v)) for k, v in kw.items()]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib forwards Authorization to a redirect target; a feed never follows one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class PollFailed(Exception):
    pass


def http_get(url, headers, timeout):
    """(status, body bytes). HTTP errors come back as their status; a network failure raises PollFailed with a
    message that holds no URL."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except OSError:
            body = b""
        return e.code, body
    except (socket.timeout, TimeoutError):
        raise PollFailed("timed out")
    except urllib.error.URLError as e:
        reason = e.reason
        raise PollFailed("unreachable (%s)" % (getattr(reason, "strerror", None) or type(reason).__name__))
    except (OSError, ValueError, http.client.HTTPException) as e:  # a cut connection mid-body included
        raise PollFailed("read failed (%s)" % type(e).__name__)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path, text, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class _held:
    """Hold SIGTERM/SIGINT/SIGHUP while a batch and its position are written, so a stop never splits them."""
    SIGS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}

    def __enter__(self):
        self.old = signal.pthread_sigmask(signal.SIG_BLOCK, self.SIGS)

    def __exit__(self, *a):
        signal.pthread_sigmask(signal.SIG_SETMASK, self.old)


def _secs(clock):
    parts = [int(p) for p in clock.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


# ---------------------------------------------------------------- the poller

class Feed:
    def __init__(self, d, cfg, token, sleep=time.sleep):
        self.d, self.cfg, self.token, self.sleep = d, cfg, token, sleep
        self.name = cfg["name"]
        self.url = cfg["url"]
        self.proto = cfg.get("protocol") or "whosaid"
        self.min_chars = int(cfg.get("minChars", 12))
        self.batch = float(cfg.get("batch", 60))
        self.poll = max(0.05, float(cfg.get("poll", 3)))
        self.transcript = os.path.join(d, "transcript.jsonl")
        self.state_path = os.path.join(d, "state.json")
        self.status_path = os.path.join(d, "status")
        self.pending = []
        self.fails = 0
        self.last_flush = time.monotonic()
        try:
            with open(self.status_path) as f:
                self.status = f.read().strip() or None
        except OSError:
            self.status = None
        key = {"protocol": self.proto, "url": safe_url(self.url), "stem": cfg.get("stem")}
        st = _read_json(self.state_path, {}) or {}
        if st and st.get("key") != key:
            self.say("source changed (protocol, URL or stem); starting a new position")
            st = {}
        if not st:
            st = {"key": key, "size": self._size()}
            self.status = None
        self.state = st
        self.pos = dict(st)
        self._recover()

    # -- output
    def scrub(self, s):
        s = str(s)
        if self.token:
            s = s.replace(self.token, "***")
        if self.url != safe_url(self.url):
            s = s.replace(self.url, safe_url(self.url))
        return s

    def say(self, msg):
        print("%s feed %s: %s" % (time.strftime("%H:%M:%S"), self.name, self.scrub(msg)), flush=True)

    def _size(self):
        try:
            return os.path.getsize(self.transcript)
        except OSError:
            return 0

    def _recover(self):
        """Lines appended after the last saved position (a kill between append and save) move the position on."""
        size, have = self._size(), int(self.state.get("size") or 0)
        if size <= have:
            return
        with open(self.transcript, "rb") as f:
            f.seek(have)
            tail = f.read()
        for raw in tail.split(b"\n"):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if isinstance(d.get("n"), int) and d.get("stem") == self.pos.get("stem"):
                self.pos["n"] = max(int(self.pos.get("n", -1)), d["n"])
            if isinstance(d.get("i"), int):
                self.pos["from"] = max(int(self.pos.get("from") or 0), d["i"] + 1)
        self.say("recovered the position past %d unsaved byte(s)" % (size - have))
        self._commit()

    def _append(self, rows):
        if not rows:
            return
        with open(self.transcript, "a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
            f.flush()
            os.fsync(f.fileno())

    def _commit(self):
        self.pos["size"] = self._size()
        self.state = dict(self.pos)
        _write_atomic(self.state_path, self.scrub(json.dumps(self.state, indent=1)) + "\n")

    def flush(self):
        """Append the held lines, then save the position they reach (in that order)."""
        with _held():
            rows, self.pending = self.pending, []
            self._append(rows)
            if rows or self.pos != self.state:
                self._commit()
        if rows:
            self.say("%d line(s) -> %s" % (len(rows), self.transcript))
        self.last_flush = time.monotonic()

    def maybe_flush(self):
        if self.batch <= 0 or time.monotonic() - self.last_flush >= self.batch:
            self.flush()

    def note(self, text):
        self._append([{"note": self.scrub(text), "at": _now_iso()}])

    def set_status(self, s):
        if s == self.status:
            return
        self.flush()
        with _held():
            self.status = s
            _write_atomic(self.status_path, s + "\n", 0o644)
            self.say("source: http-feed %s" % s)
            self.note("source: http-feed %s" % s)
            self._commit()

    def ok(self):
        self.fails = 0
        self.set_status("ok")

    def failed(self, why):
        self.fails += 1
        if self.fails == 1 or self.fails == STALL_AFTER:
            self.say("poll failed: %s" % why)
        if self.fails >= STALL_AFTER:
            self.set_status("stalled (%d failed polls)" % STALL_AFTER)
        self.sleep(self.poll)

    def line(self, clock, speaker, text, **extra):
        """Queue one utterance, or drop it as filler. Returns True when queued."""
        text = " ".join(str(text or "").split())
        if len(text) < self.min_chars:
            return False
        speaker = str(speaker or "").strip()
        row = {"clock": clock or "", "speaker": speaker, "text": text, "final": True}
        if speaker.lower() in PLACEHOLDER:
            row["speaker"], row["unnamed"] = UNKNOWN, True
            if not self.pos.get("unnamedNoted"):
                self.pos["unnamedNoted"] = True
                self.pending.append({"note": UNNAMED_NOTE, "at": _now_iso()})
        row.update(extra)
        self.pending.append(row)
        return True

    def _refused(self, code):
        if code in (401, 403):
            self.set_status("auth refused")
            self.say("HTTP %d: the server refused the token; fix the token file, then fleet feed start again" % code)
            raise Stop(2)
        if code == 400:
            self.set_status("stalled (bad request, feed stopped)")
            self.say("HTTP 400: the server refused the request (bad stem, cursor or wait); stopping")
            raise Stop(1)

    def get_json(self, url, wait=0.0):
        """Parsed JSON body for a 200, the int status for any other answer, or None after a failed poll."""
        hdr = {"Authorization": "Bearer %s" % self.token, "Accept": "application/json"}
        try:
            code, body = http_get(url, hdr, wait + HTTP_SLACK)
        except PollFailed as e:
            self.failed(str(e))
            return None
        self._refused(code)
        if code != 200:
            return code
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            d = None
        if not isinstance(d, dict):
            self.failed("not a JSON object")
            return None
        return d

    # -- whosaid live serve
    def _base(self):
        u = urllib.parse.urlsplit(self.url)  # whosaid paths hang off the base; a query on it is dropped
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))

    def _newer_stem(self):
        """The stem to follow next, or None (no newer session yet)."""
        if self.cfg.get("stem"):
            if self.pos.get("stem") == self.cfg["stem"]:
                return None
            if not self.pos.get("began"):
                self.pos["began"] = True
                self.pos["skipFirst"] = not self.cfg.get("fromStart")
            return self.cfg["stem"]
        d = self.get_json(self._base() + "/sessions")
        if d is None:
            return False
        if isinstance(d, int):
            self.failed("HTTP %d on /sessions" % d)
            return False
        self.fails = 0
        self.set_status("gone" if self.pos.get("ended") else "ok")  # waiting for the next call stays gone
        stems = sorted(str(s.get("stem")) for s in (d.get("sessions") or []) if isinstance(s, dict) and s.get("stem"))
        cur = self.pos.get("stem")
        newer = [s for s in stems if cur is None or s > cur]
        if not self.pos.get("began"):
            self.pos["began"] = True
            if newer:  # the call already running when the feed first started: its end, unless --from-start
                self.pos["skipFirst"] = not self.cfg.get("fromStart")
        return newer[-1] if newer else None

    def _take(self, stem, entries):
        top = int(self.pos.get("n", -1))
        for e in sorted((e for e in entries if isinstance(e, dict) and isinstance(e.get("n"), int)),
                        key=lambda e: e["n"]):
            n = e["n"]
            if n <= top:
                continue
            top = n
            if self.pos.get("skipFirst"):
                continue
            who = e.get("who")
            spk = "me" if who == "me" else (e.get("name") or who)
            self.line(e.get("clock"), spk, e.get("text"), stem=stem, n=n)
        self.pos["n"] = top
        self.pos.pop("skipFirst", None)

    def _gone(self):
        self.pos["ended"] = True
        self.flush()
        self.set_status("gone")
        if self.cfg.get("stem"):
            self.say("session %s ended; --stem pins one session, so the feed stops" % self.cfg["stem"])
            raise Stop(0)

    def step_whosaid(self):
        stem = self.pos.get("stem")
        if not stem or self.pos.get("ended"):
            nxt = self._newer_stem()
            if not nxt:
                if nxt is None:  # False: a failed poll already waited
                    self.sleep(self.poll)
                return
            self.pos.update(stem=nxt, n=-1, cursor=None, ended=False)
            self.say("following session %s" % nxt)
            self.ok()
            stem = nxt
        cursor = self.pos.get("cursor")
        wait = LONG_WAIT if self.batch <= 0 else max(1.0, min(LONG_WAIT, self.batch))
        q = {"wait": "%g" % wait}
        if cursor:
            q["cursor"] = cursor
        else:
            wait = 0.0
            q = {}
        url = _with_query("%s/sessions/%s" % (self._base(), urllib.parse.quote(stem, safe="")), **q)
        d = self.get_json(url, wait)
        if d is None:
            return
        if isinstance(d, int):
            if d == 404:  # the session is gone from the server
                self._gone()
                return
            self.failed("HTTP %d" % d)
            return
        self.ok()
        with _held():  # a stop mid-answer must not save rows without the position they reach
            self._take(stem, d.get("entries") or [])
            if isinstance(d.get("cursor"), str):
                self.pos["cursor"] = d["cursor"]
        self.maybe_flush()
        if (d.get("meta") or {}).get("ended"):
            self._gone()

    # -- text lines with ?from=N
    def step_lines(self):
        frm = self.pos.get("from")
        hdr = {self.cfg.get("header") or "X-Token": self.token}
        try:
            code, body = http_get(_with_query(self.url, **{"from": frm or 0}), hdr, self.poll + HTTP_SLACK)
        except PollFailed as e:
            self.failed(str(e))
            return
        self._refused(code)
        if code in (404, 410):
            self.flush()
            self.set_status("gone")
            self.sleep(self.poll)
            return
        if code != 200:
            self.failed("HTTP %d" % code)
            return
        self.ok()
        complete = body.decode("utf-8", "replace").split("\n")[:-1]  # only newline-terminated lines count
        start = frm or 0
        since = self.cfg.get("since")
        if frm is None and not self.cfg.get("fromStart") and not since:
            self.pos["from"] = len(complete)  # start at the current end
            self.flush()
        else:
            with _held():  # a stop mid-answer must not save rows without the position they reach
                for i, raw in enumerate(complete, start=start):
                    u = _listen_mod().parse_utterance(raw)
                    if not u:
                        continue
                    if since and not self.pos.get("sinceReached"):
                        if not u["clock"] or _secs(u["clock"]) < _secs(since):
                            continue
                        self.pos["sinceReached"] = True
                    self.line(u["clock"], u["speaker"], u["text"], i=i)
                self.pos["from"] = start + len(complete)
            self.maybe_flush()
        self.sleep(self.poll)

    def run(self):
        step = self.step_whosaid if self.proto == "whosaid" else self.step_lines
        while True:
            try:
                step()
            except Stop:
                raise
            except Exception as e:  # anything else is one failed poll, never a dead feed
                self.failed("error (%s)" % type(e).__name__)


def _on_signal(signum, _frame):
    raise Stop(0)


def run_main(argv):
    ap = argparse.ArgumentParser(prog="fleet_feed.py _run")
    ap.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    cfg = _read_json(os.path.join(a.dir, "config.json"))
    if not isinstance(cfg, dict):
        print("FAIL no feed config in %s" % a.dir, flush=True)
        return 1
    lockf = open(os.path.join(a.dir, "lock"), "a")
    try:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("FAIL feed %s is already running" % cfg.get("name"), flush=True)
        return 1
    try:
        token = read_token(cfg["tokenFile"])
    except FeedError as e:
        print("FAIL %s" % e, flush=True)
        return 2
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _on_signal)
    feed = None
    code = 0
    try:
        feed = Feed(a.dir, cfg, token)
        feed.say("started: %s %s -> %s" % (feed.proto, safe_url(feed.url), feed.transcript))
        feed.run()
    except Stop as e:
        code = e.code
    except Exception as e:  # a crash is a line in the log, scrubbed, never a traceback holding a header
        code = 1
        msg = "crashed: %s" % type(e).__name__
        print(feed.scrub(msg) if feed else msg, flush=True)
    finally:
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        if feed is not None:
            try:
                if feed.status == "auth refused" or str(feed.status).startswith("stalled (bad request"):
                    feed.flush()  # keep the reason it stopped as the last status the pane saw
                else:
                    feed.set_status("stopped")  # fleet feed stop, the bound, a signal: one pane note, then done
            except OSError:
                pass
            feed.say("stopped")
    return code


# ---------------------------------------------------------------- CLI

def _lock_busy(d):
    p = os.path.join(d, "lock")
    if not os.path.exists(p):
        return False
    with open(p, "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return False


def _side():
    import fleet_side
    return fleet_side


def _row(d):
    s = _read_json(os.path.join(d, "side.json"), {}) or {}
    if not s.get("id"):
        return None
    r = _side().get(s["id"])
    if r is None or (s.get("nonce") and r.get("nonce") != s.get("nonce")):
        return None
    return r


def _alive(r):
    return bool(r) and r.get("status") == "running" and _side().pid_alive(r.get("pid"))


def cmd_start(argv):
    ap = argparse.ArgumentParser(prog="fleet feed start")
    ap.add_argument("url")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--name", default="feed")
    ap.add_argument("--protocol", choices=PROTOCOLS, default="whosaid")
    ap.add_argument("--stem")
    ap.add_argument("--from-start", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--min-chars", type=int, default=12)
    ap.add_argument("--batch", type=float, default=60.0)
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--header", default="X-Token")
    ap.add_argument("--parent")
    ap.add_argument("--bound", default="12h")
    a = ap.parse_args(argv)
    d = feed_dir(a.name)
    check_url(a.url)
    read_token(a.token_file)  # refuse a bad file now; the poller reads it again itself
    if a.since and not CLOCK_RE.match(a.since):
        raise FeedError("bad --since %r (want HH:MM:SS)" % a.since)
    if a.since and a.protocol != "lines":
        raise FeedError("--since is for --protocol lines (whosaid picks sessions by stem)")
    if a.stem and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$", a.stem):
        raise FeedError("bad --stem %r" % a.stem)
    if a.poll <= 0 or a.batch < 0 or a.min_chars < 0:
        raise FeedError("--poll must be > 0, --batch and --min-chars >= 0")
    os.makedirs(d, mode=0o700, exist_ok=True)
    if _lock_busy(d) or _alive(_row(d)):
        raise FeedError("feed %s is already running (fleet feed stop %s)" % (a.name, a.name))
    cfg = {"name": a.name, "url": a.url, "tokenFile": os.path.abspath(os.path.expanduser(a.token_file)),
           "protocol": a.protocol, "stem": a.stem, "fromStart": a.from_start, "since": a.since,
           "minChars": a.min_chars, "batch": a.batch, "poll": a.poll, "header": a.header}
    _write_atomic(os.path.join(d, "config.json"), json.dumps(cfg, indent=1) + "\n")
    cmd = [sys.executable, os.path.abspath(__file__), "_run", "--dir", d]
    side_cmd = [sys.executable, os.path.join(HERE, "fleet_side.py"), "run", "--host", "feed", "--model", a.name,
                "--bound", a.bound, "--log", os.path.join(d, "log")]
    if a.parent:
        side_cmd += ["--parent", a.parent]
    r = subprocess.run(side_cmd + ["--"] + cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise FeedError((r.stderr.strip() or r.stdout.strip() or "fleet side run failed").replace("FAIL ", "", 1))
    parts = r.stdout.split()
    sid = parts[0] if parts else "?"
    kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    row = _side().get(sid) or {}
    _write_atomic(os.path.join(d, "side.json"), json.dumps({"id": sid, "nonce": row.get("nonce")}) + "\n")
    tr = os.path.join(d, "transcript.jsonl")
    print("OK feed %s %s pid=%s %s %s" % (a.name, sid, kv.get("pid", "?"), a.protocol, safe_url(a.url)))
    print("transcript=%s" % tr)
    print("log=%s" % os.path.join(d, "log"))
    print("follow it: fleet listen --source file %s" % shlex.quote(tr))


def _names():
    try:
        return sorted(n for n in os.listdir(feeds_root()) if NAME_RE.match(n) and
                      os.path.isfile(os.path.join(feeds_root(), n, "config.json")))
    except OSError:
        return []


def _status_of(d):
    try:
        with open(os.path.join(d, "status")) as f:
            return f.read().strip() or "-"
    except OSError:
        return "-"


def cmd_ls(argv):
    argparse.ArgumentParser(prog="fleet feed ls").parse_args(argv)
    names = _names()
    if not names:
        print("no feeds")
        return
    for n in names:
        d = feed_dir(n)
        r = _row(d)
        cfg = _read_json(os.path.join(d, "config.json"), {}) or {}
        print("%-14s %-5s %-5s %-28s %s %s" % (n, (r or {}).get("id") or "-", "alive" if _alive(r) else "dead",
                                              _status_of(d), cfg.get("protocol"), safe_url(cfg.get("url"))))


def _need(name):
    d = feed_dir(name)
    if not os.path.isfile(os.path.join(d, "config.json")):
        raise FeedError("no feed %s" % name)
    return d


def cmd_stop(argv):
    ap = argparse.ArgumentParser(prog="fleet feed stop")
    ap.add_argument("name")
    a = ap.parse_args(argv)
    d = _need(a.name)
    r = _row(d)
    if not _alive(r):
        print("OK feed %s is not running" % a.name)
        return
    try:
        os.killpg(int(r["pid"]), signal.SIGTERM)  # the child leads its own session (fleet side run)
    except OSError:
        os.kill(int(r["pid"]), signal.SIGTERM)
    end = time.monotonic() + 10
    while time.monotonic() < end:
        rr = _side().get(r["id"]) or {}
        if rr.get("endedAt"):
            break
        time.sleep(0.1)
    print("OK stopped feed %s (%s)" % (a.name, r["id"]))


def cmd_status(argv):
    ap = argparse.ArgumentParser(prog="fleet feed status")
    ap.add_argument("name")
    a = ap.parse_args(argv)
    d = _need(a.name)
    r = _row(d)
    cfg = _read_json(os.path.join(d, "config.json"), {}) or {}
    st = _read_json(os.path.join(d, "state.json"), {}) or {}
    tr = os.path.join(d, "transcript.jsonl")
    lines = notes = 0
    try:
        with open(tr) as f:
            for ln in f:
                if '"note"' in ln and '"text"' not in ln:
                    notes += 1
                elif ln.strip():
                    lines += 1
    except OSError:
        pass
    pos = ("session %s line n%s" % (st.get("stem"), st.get("n"))) if cfg.get("protocol") == "whosaid" \
        else ("from=%s" % st.get("from"))
    print("feed %s: %s, status %s" % (a.name, "alive" if _alive(r) else "dead", _status_of(d)))
    print("side run %s pid=%s" % ((r or {}).get("id") or "-", (r or {}).get("pid") or "-"))
    print("source %s %s" % (cfg.get("protocol"), safe_url(cfg.get("url"))))
    print("position %s" % pos)
    print("transcript %s (%d line(s), %d note(s))" % (tr, lines, notes))
    print("log %s" % os.path.join(d, "log"))


COMMANDS = {"start": cmd_start, "ls": cmd_ls, "stop": cmd_stop, "status": cmd_status}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_run"]:
        return run_main(argv[1:])
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write("usage: fleet feed start URL --token-file PATH [...] | ls | stop NAME | status NAME\n")
        return 2
    try:
        COMMANDS[argv[0]](argv[1:])
    except FeedError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
