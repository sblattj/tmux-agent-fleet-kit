#!/usr/bin/env python3
"""fleet feed (issue #37): a remote call transcript mirrored into a local file `fleet listen --source file` follows.

Stdlib only. Throwaway FLEET_STATE, a stub tmux on PATH, local stdlib HTTP servers (token-checked, growing data) for
the whosaid `live serve` protocol and the `?from=N` text-lines protocol, and one test over the real whosaid server
code when its checkout is present. The poller runs as a real process (lib/fleet_feed.py _run), and the end-to-end
tests go through bin/fleet feed start, i.e. the side-run supervisor.

Run: env -u FLEET_ROLE python3 -m unittest test/test_feed.py
"""
import http.server
import importlib.util
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
FEED = os.path.join(ROOT, "lib", "fleet_feed.py")
LISTEN = os.path.join(ROOT, "lib", "fleet_listen.py")
# whosaid >= v1.52.0 (`live serve`): $WHOSAID_LIB, else a checkout in ~/code/whosaid
WHOSAID_LIBS = [p for p in (os.environ.get("WHOSAID_LIB"), os.path.expanduser("~/code/whosaid/lib")) if p]
TOKEN = "feedtok-" + os.urandom(12).hex()

_spec = importlib.util.spec_from_file_location("fleet_listen", LISTEN)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


def wait_for(pred, secs=10.0, step=0.05):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


# ---------------------------------------------------------------- stub servers

class Stub:
    """Base: a ThreadingHTTPServer on 127.0.0.1:0 with a token check and a `down` switch (503)."""

    def __init__(self, token=TOKEN):
        self.token = token
        self.down = False
        self.requests = []
        self.methods = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                b = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                stub.requests.append(self.path)
                stub.methods.append("GET")
                if stub.down:
                    return self._send(503, "{}")
                if not stub.authorized(self.headers):
                    return self._send(401, '{"ok": false}')
                stub.handle(self)

            def _no(self):
                stub.methods.append(self.command)
                self._send(405, "{}")

            do_POST = do_PUT = do_DELETE = do_PATCH = _no

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class WhosaidStub(Stub):
    """GET /sessions and /sessions/<stem>?cursor=<stem>:nK&wait=S, Bearer token, as whosaid live serve."""

    def __init__(self, token=TOKEN, ignore_cursor=False):
        Stub.__init__(self, token)
        self.lock = threading.Lock()
        self.sessions = {}
        self.ignore_cursor = ignore_cursor  # answer every request with the whole session (reset/full)

    def authorized(self, headers):
        return headers.get("Authorization", "") == "Bearer %s" % self.token

    def add(self, stem, *texts, who="them", name=None, ended=False):
        with self.lock:
            s = self.sessions.setdefault(stem, {"meta": {"started": 1.0, "ended": False, "updated": 1.0},
                                                "entries": []})
            for t in texts:
                n = len(s["entries"]) + 1
                e = {"n": n, "t": float(n), "clock": "10:00:%02d" % n, "who": who, "text": t}
                if name:
                    e["name"] = name
                s["entries"].append(e)
            if ended:
                s["meta"]["ended"] = True

    def handle(self, h):
        u = urllib.parse.urlsplit(h.path)
        q = urllib.parse.parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if parts == ["sessions"]:
            with self.lock:
                ss = [{"stem": k, "started": 1.0, "updated": 1.0, "ended": v["meta"]["ended"],
                       "lines": len(v["entries"])} for k, v in self.sessions.items()]
            return h._send(200, json.dumps({"ok": True, "sessions": ss}))
        if len(parts) == 2 and parts[0] == "sessions":
            stem = parts[1]
            cursor = (q.get("cursor") or [None])[0]
            wait = min(float((q.get("wait") or ["0"])[0]), 30.0)
            deadline = time.monotonic() + wait
            while True:
                with self.lock:
                    s = self.sessions.get(stem)
                    if s is None:
                        return h._send(404, '{"ok": false}')
                    entries = list(s["entries"])
                    meta = dict(s["meta"])
                full = cursor is None or self.ignore_cursor
                k = -1 if full else int(cursor.split(":n")[1])
                new = [e for e in entries if e["n"] > k]
                top = max([k] + [e["n"] for e in entries])
                if new or meta["ended"] or time.monotonic() >= deadline:
                    return h._send(200, json.dumps({"ok": True, "stem": stem, "meta": meta, "entries": new,
                                                    "cursor": "%s:n%d" % (stem, top), "reset": False,
                                                    "full": full}))
                time.sleep(0.05)
        h._send(404, '{"ok": false}')


class LinesStub(Stub):
    """GET /t?from=N: the text lines after the first N; token in X-Token (or another header)."""

    def __init__(self, token=TOKEN, header="X-Token"):
        Stub.__init__(self, token)
        self.header = header
        self.lines = []
        self.partial = ""

    def authorized(self, headers):
        return headers.get(self.header, "") == self.token

    def handle(self, h):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
        n = int((q.get("from") or ["0"])[0])
        body = "".join(ln + "\n" for ln in self.lines[n:]) + self.partial
        h._send(200, body, "text/plain")


# ---------------------------------------------------------------- base

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-feed-")
        self.state = os.path.join(self.tmp, "state")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(bindir)
        with open(os.path.join(bindir, "tmux"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(bindir, "tmux"), 0o755)
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"WATCH": {"name": "WATCH", "cli": "claude", "mode": "prompt", "session_id": "u-1",
                                 "port": None}}, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        self.env.update(FLEET_STATE=self.state, FLEET_NAME="WATCH", FLEET_SIDE_POLL="0.2", FLEET_WAKE_SPAWN="0",
                        FLEET_TMUX_SOCKET="feed-test-none", CLAUDECODE="",
                        PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        self.tokfile = os.path.join(self.tmp, "token")
        with open(self.tokfile, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(self.tokfile, 0o600)
        self.procs = []
        self.stubs = []
        self.outputs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for r in self._side_rows():
            try:
                os.killpg(int(r["pid"]), signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                pass
        for s in self.stubs:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub(self, cls, **kw):
        s = cls(**kw)
        self.stubs.append(s)
        return s

    def _side_rows(self):
        p = os.path.join(self.state, "sides.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def fdir(self, name="call"):
        return os.path.join(self.state, "feeds", name)

    def config(self, url, name="call", protocol="whosaid", **kw):
        d = self.fdir(name)
        os.makedirs(d, exist_ok=True)
        cfg = {"name": name, "url": url, "tokenFile": self.tokfile, "protocol": protocol, "stem": None,
               "fromStart": False, "since": None, "minChars": 12, "batch": 0, "poll": 0.2, "header": "X-Token"}
        cfg.update(kw)
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump(cfg, f)
        return d

    def poller(self, d):
        log = open(os.path.join(d, "log"), "ab")
        p = subprocess.Popen([sys.executable, FEED, "_run", "--dir", d], stdout=log, stderr=log, env=self.env,
                             stdin=subprocess.DEVNULL)
        log.close()
        self.procs.append(p)
        return p

    def stop(self, p):
        p.send_signal(signal.SIGTERM)
        return p.wait(timeout=15)

    def fleet(self, *args):
        r = subprocess.run(["bash", FLEET] + list(args), capture_output=True, text=True, env=self.env, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.outputs.append(r.stdout + r.stderr)
        return r

    def rows(self, d):
        p = os.path.join(d, "transcript.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def utts(self, d):
        return [r for r in self.rows(d) if "text" in r]

    def notes(self, d):
        return [r["note"] for r in self.rows(d) if "note" in r and "text" not in r]

    def status(self, d):
        try:
            with open(os.path.join(d, "status")) as f:
                return f.read().strip()
        except OSError:
            return None

    def log(self, d):
        try:
            with open(os.path.join(d, "log")) as f:
                return f.read()
        except OSError:
            return ""

    def assert_listen_shape(self, d):
        with open(os.path.join(d, "transcript.jsonl")) as f:
            raws = [ln for ln in f if ln.strip()]
        for raw in raws:
            u = L.parse_utterance(raw)
            if '"note"' in raw and '"text"' not in raw:
                self.assertIsNone(u, raw)
                self.assertIsNotNone(L.source_note(raw), raw)
            else:
                self.assertIsNotNone(u, raw)
                self.assertTrue(u["final"])
                self.assertRegex(u["clock"], r"^\d{1,2}:\d{2}:\d{2}$")

    def assert_no_token_anywhere(self, extra=()):
        hits = []
        for dp, _dn, fns in os.walk(self.tmp):
            for fn in fns:
                p = os.path.join(dp, fn)
                if p == self.tokfile or os.path.islink(p):
                    continue
                try:
                    with open(p, "rb") as f:
                        if TOKEN.encode() in f.read():
                            hits.append(p)
                except OSError:
                    pass
        for i, s in enumerate(list(self.outputs) + list(extra)):
            if TOKEN in s:
                hits.append("output #%d" % i)
        self.assertEqual(hits, [], "token found in %s" % hits)


# ---------------------------------------------------------------- whosaid protocol

class WhosaidTest(Base):
    def test_starts_at_end_mirrors_new_lines_filters_filler_names_unknown(self):
        s = self.stub(WhosaidStub)
        s.add("20261126T100000", "an earlier line that was said before the feed started", name="Bob_Example")
        d = self.config(s.url)
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"), self.log(d))
        s.add("20261126T100000", "can you check the deploy before noon today", name="Bob_Example")
        s.add("20261126T100000", "ok yeah")  # filler, under 12 characters
        s.add("20261126T100000", "the speaker has not been named yet here")  # who=them, no name
        s.add("20261126T100000", "I will send the notes after the call", who="me")
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 3), self.rows(d))
        u = self.utts(d)
        self.assertEqual([r["text"] for r in u], ["can you check the deploy before noon today",
                                                  "the speaker has not been named yet here",
                                                  "I will send the notes after the call"])
        self.assertEqual([r["speaker"] for r in u], ["Bob_Example", "unknown", "me"])
        self.assertTrue(u[1]["unnamed"])
        self.assertNotIn("unnamed", u[0])
        self.assertEqual(L.parse_utterance(json.dumps(u[1])).get("unnamed"), True)
        self.assertTrue(any("not named yet" in n for n in self.notes(d)), self.notes(d))
        self.assert_listen_shape(d)
        self.assertEqual(self.stop(p), 0)
        self.assertEqual(set(s.methods), {"GET"})
        self.assertTrue(any("wait=" in r for r in s.requests), s.requests)  # long poll, not fast polling
        self.assert_no_token_anywhere([self.log(d)])

    def test_from_start_then_restart_resumes_without_duplicates(self):
        s = self.stub(WhosaidStub)
        stem = "20261126T100000"
        s.add(stem, "first backlog line of the call", "second backlog line of the call", name="Ann_Example")
        d = self.config(s.url, fromStart=True)
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 2), self.rows(d))
        self.assertEqual(self.stop(p), 0)
        with open(os.path.join(d, "state.json")) as f:
            st = json.load(f)
        self.assertEqual((st["stem"], st["n"]), (stem, 2))
        s.add(stem, "a line said while the feed was down", name="Ann_Example")
        p = self.poller(d)
        s.add(stem, "a line said after the feed came back", name="Ann_Example")
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 4), self.rows(d))
        time.sleep(0.8)
        self.assertEqual([r["n"] for r in self.utts(d)], [1, 2, 3, 4])
        self.assertEqual(self.stop(p), 0)
        # each run ends on one `stopped` note and a restart says `ok` again: never a note per poll
        self.assertEqual(self.notes(d), ["source: http-feed ok", "source: http-feed stopped"] * 2, self.notes(d))

    def test_full_responses_never_duplicate(self):
        s = self.stub(WhosaidStub, ignore_cursor=True)
        stem = "20261126T100000"
        s.add(stem, "backlog line one of this call", name="Ann_Example")
        d = self.config(s.url, fromStart=True)
        p = self.poller(d)
        for i in range(3):
            s.add(stem, "new line number %d of this call" % i, name="Ann_Example")
            time.sleep(0.3)
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 4), self.rows(d))
        time.sleep(1.0)
        self.assertEqual([r["n"] for r in self.utts(d)], [1, 2, 3, 4])
        self.stop(p)

    def test_batching_holds_lines_until_the_interval(self):
        s = self.stub(WhosaidStub)
        stem = "20261126T100000"
        s.add(stem, "backlog line, skipped by default")
        d = self.config(s.url, batch=2.5)
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"))
        time.sleep(0.3)
        t0 = time.monotonic()
        s.add(stem, "batched line number one here", "batched line number two here", name="Ann_Example")
        time.sleep(0.8)
        self.assertEqual(self.utts(d), [], "a line was flushed before the batch interval")
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 2, secs=8), self.rows(d))
        self.assertGreater(time.monotonic() - t0, 1.0)
        self.assertIn("2 line(s)", self.log(d))  # one append for the batch
        self.stop(p)

    def test_stalled_once_then_ok_again(self):
        s = self.stub(WhosaidStub)
        s.add("20261126T100000", "backlog line, skipped by default")
        d = self.config(s.url)
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"))
        s.down = True
        self.assertTrue(wait_for(lambda: self.status(d) == "stalled (3 failed polls)", secs=40), self.log(d))
        n = len(s.requests)
        self.assertTrue(wait_for(lambda: len(s.requests) >= n + 4, secs=20))  # more failed polls
        self.assertEqual(self.notes(d).count("source: http-feed stalled (3 failed polls)"), 1, self.notes(d))
        self.assertEqual(self.log(d).count("source: http-feed stalled"), 1, self.log(d))
        s.down = False
        self.assertTrue(wait_for(lambda: self.status(d) == "ok", secs=40), self.log(d))
        self.assertEqual(self.notes(d), ["source: http-feed ok", "source: http-feed stalled (3 failed polls)",
                                         "source: http-feed ok"])
        self.stop(p)

    def test_wrong_token_is_auth_refused_once_and_stops(self):
        s = self.stub(WhosaidStub, token="the-server-wants-another-token")
        d = self.config(s.url)
        p = self.poller(d)
        self.assertEqual(p.wait(timeout=20), 2)
        self.assertEqual(self.status(d), "auth refused")
        self.assertEqual(self.notes(d), ["source: http-feed auth refused"])
        self.assertEqual(self.log(d).count("source: http-feed auth refused"), 1)
        self.assert_no_token_anywhere([self.log(d)])

    def test_ended_is_gone_once_then_the_next_session_from_its_start(self):
        s = self.stub(WhosaidStub)
        s.add("20261126T100000", "backlog line, skipped by default")
        d = self.config(s.url)
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"))
        s.add("20261126T100000", "the last thing said on the first call", name="Ann_Example", ended=True)
        self.assertTrue(wait_for(lambda: self.status(d) == "gone"), self.log(d))
        time.sleep(1.0)
        self.assertEqual(self.notes(d).count("source: http-feed gone"), 1)
        s.add("20261126T110000", "the first thing said on the next call", name="Cy_Example")
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 2), self.rows(d))
        self.assertEqual([(r["stem"], r["n"]) for r in self.utts(d)],
                         [("20261126T100000", 2), ("20261126T110000", 1)])
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"))
        self.stop(p)

    def test_a_cut_connection_is_a_failed_poll_not_a_dead_feed(self):
        s = self.stub(WhosaidStub)
        orig = s.handle
        s.cut = False

        def handle(h):
            if s.cut and "/sessions/" in h.path:
                h.send_response(200)
                h.send_header("Content-Length", "100")
                h.end_headers()
                h.wfile.write(b'{"ok": true')  # 11 of 100 bytes, then the connection closes
                h.close_connection = True
                return
            orig(h)
        s.handle = handle
        s.add("20261126T100000", "backlog line, skipped by default")
        d = self.config(s.url)
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"))
        s.cut = True
        self.assertTrue(wait_for(lambda: self.status(d) == "stalled (3 failed polls)", secs=40), self.log(d))
        self.assertIsNone(p.poll(), self.log(d))
        s.cut = False
        self.assertTrue(wait_for(lambda: self.status(d) == "ok", secs=40), self.log(d))
        self.assertEqual(self.notes(d).count("source: http-feed stalled (3 failed polls)"), 1)
        self.assertIn("IncompleteRead", self.log(d))
        self.stop(p)

    def test_bad_request_is_stalled_and_stops(self):
        s = self.stub(WhosaidStub)
        orig = s.handle
        s.handle = lambda h: h._send(400, '{"ok": false}') if "/sessions/" in h.path else orig(h)
        s.add("20261126T100000", "backlog line, skipped by default")
        d = self.config(s.url)
        p = self.poller(d)
        self.assertEqual(p.wait(timeout=20), 1)
        self.assertEqual(self.status(d), "stalled (bad request, feed stopped)")


# ---------------------------------------------------------------- lines protocol

class LinesTest(Base):
    def test_lines_end_since_filler_resume_and_header(self):
        s = self.stub(LinesStub, header="X-Feed-Key")
        s.lines = ["[09:00:01] Ann_Example: an old call from this morning, long ago"]
        d = self.config(s.url + "/t?keep=1", protocol="lines", header="X-Feed-Key")
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"))
        s.lines.append("[10:00:01] Bob_Example: can you own the rollback plan")
        s.lines.append("[10:00:02] them: right")  # filler
        s.lines.append("[10:00:03] them: who is going to write the summary")
        s.partial = "[10:00:04] Bob_Example: half a li"  # not newline-terminated yet: not counted
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 2), self.rows(d))
        self.assertEqual(self.stop(p), 0)
        u = self.utts(d)
        self.assertEqual([(r["speaker"], r.get("unnamed")) for r in u], [("Bob_Example", None), ("unknown", True)])
        self.assertEqual([r["i"] for r in u], [1, 3])
        s.lines.append("[10:00:04] Bob_Example: half a line, now finished")
        s.partial = ""
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 3), self.rows(d))
        time.sleep(0.6)
        self.assertEqual([r["i"] for r in self.utts(d)], [1, 3, 4])
        self.assertTrue(all("keep=1" in r and "from=" in r for r in s.requests), s.requests)
        self.stop(p)
        self.assert_listen_shape(d)
        self.assert_no_token_anywhere([self.log(d)])

    def test_since_skips_to_the_current_call(self):
        s = self.stub(LinesStub)
        s.lines = ["[09:00:01] Ann_Example: the morning call, not this one",
                   "[11:30:00] Ann_Example: the current call starts right here",
                   "[11:30:05] Bob_Example: and this follows in the current call"]
        d = self.config(s.url, protocol="lines", since="11:29:00")
        p = self.poller(d)
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 2), self.rows(d))
        self.assertEqual(self.utts(d)[0]["clock"], "11:30:00")
        self.stop(p)

    def test_wrong_header_token_is_auth_refused(self):
        s = self.stub(LinesStub, token="another-token-entirely")
        d = self.config(s.url, protocol="lines")
        p = self.poller(d)
        self.assertEqual(p.wait(timeout=20), 2)
        self.assertEqual(self.status(d), "auth refused")


# ---------------------------------------------------------------- the CLI, through bin/fleet and fleet side run

class CliTest(Base):
    def test_start_ls_status_stop_through_the_side_run_supervisor(self):
        s = self.stub(WhosaidStub)
        s.add("20261126T100000", "backlog line, skipped by default")
        r = self.fleet("feed", "start", s.url + "/?k=" + "v", "--token-file", self.tokfile, "--name", "call",
                       "--batch", "0", "--poll", "0.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        tr = os.path.join(self.fdir(), "transcript.jsonl")
        self.assertIn("transcript=%s" % tr, r.stdout)
        self.assertIn("follow it: fleet listen --source file %s" % tr, r.stdout)
        self.assertNotIn("?k=", r.stdout)  # never the query
        row = [x for x in self._side_rows() if x["host"] == "feed"]
        self.assertEqual(len(row), 1, self._side_rows())
        self.assertEqual((row[0]["model"], row[0]["status"]), ("call", "running"))
        ps = subprocess.run(["ps", "-o", "command=", "-p", str(row[0]["pid"])], capture_output=True, text=True)
        self.assertIn("fleet_feed.py _run --dir", ps.stdout)
        self.outputs.append(ps.stdout)
        d = self.fdir()
        self.assertTrue(wait_for(lambda: self.status(d) == "ok"), self.log(d))
        s.add("20261126T100000", "a line mirrored through the side run", name="Ann_Example")
        self.assertTrue(wait_for(lambda: len(self.utts(d)) == 1), self.rows(d))
        again = self.fleet("feed", "start", s.url, "--token-file", self.tokfile, "--name", "call")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already running", again.stderr)
        ls = self.fleet("feed", "ls")
        self.assertRegex(ls.stdout, r"call\s+S\d+\s+alive\s+ok")
        st = self.fleet("feed", "status", "call")
        self.assertIn("feed call: alive, status ok", st.stdout)
        self.assertIn("(1 line(s)", st.stdout)
        self.assertIn(row[0]["id"], self.fleet("side", "ls").stdout)
        stop = self.fleet("feed", "stop", "call")
        self.assertIn("OK stopped feed call", stop.stdout)
        self.assertTrue(wait_for(lambda: "dead" in self.fleet("feed", "ls").stdout))
        self.assertIn("stopped", self.log(d))
        self.assertEqual(self.status(d), "stopped")  # a stop is one last status change the pane sees
        notes = [r["note"] for r in self.rows(d) if "note" in r and "text" not in r]
        self.assertEqual(notes[-1], "source: http-feed stopped", notes)
        self.assert_no_token_anywhere([self.log(d)])

    def test_refuses_a_token_file_that_others_can_read(self):
        os.chmod(self.tokfile, 0o644)
        r = self.fleet("feed", "start", "http://127.0.0.1:9/", "--token-file", self.tokfile)
        self.assertEqual(r.returncode, 1)
        self.assertIn("mode 644, refused: run chmod 600", r.stderr)
        self.assertEqual(self._side_rows(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "feeds", "feed", "config.json")))

    def test_refuses_mode_400_too(self):
        os.chmod(self.tokfile, 0o400)
        r = self.fleet("feed", "start", "http://127.0.0.1:9/", "--token-file", self.tokfile)
        self.assertEqual(r.returncode, 1)
        self.assertIn("mode 400, refused", r.stderr)

    def test_refuses_credentials_in_the_url(self):
        r = self.fleet("feed", "start", "http://user:%s@127.0.0.1:9/" % TOKEN, "--token-file", self.tokfile)
        self.assertEqual(r.returncode, 1)
        self.assertIn("credentials in the URL are refused", r.stderr)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_no_parent_lane_is_refused_by_the_side_run(self):
        self.env.pop("FLEET_NAME")
        r = self.fleet("feed", "start", "http://127.0.0.1:9/", "--token-file", self.tokfile)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no parent", r.stderr)


# ---------------------------------------------------------------- the listen pane and the WATCH call source

class ListenNoteTest(Base):
    def setUp(self):
        Base.setUp(self)
        env = {"FLEET_STATE": self.state, "FLEET_NAME": "WATCH", "FLEET_DECIDE": "DECISIONS",
               "FLEET_ROUTE": "ORCHESTRATOR", "FLEET_TMUX_SOCKET": "feed-test-none"}
        self._saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        Base.tearDown(self)

    def test_note_line_is_shown_and_never_routed(self):
        path = os.path.join(self.tmp, "transcript.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"note": "source: http-feed stalled (3 failed polls)", "at": "x"}) + "\n")
            f.write(json.dumps({"clock": "10:00:01", "speaker": "Ann_Example", "text": "a normal line of talk",
                                "final": True, "n": 1, "stem": "s"}) + "\n")
        q = queue.Queue()
        src = L.Source("file", path, q, poll=0.1)
        src.start()
        got = []
        end = time.monotonic() + 5
        while time.monotonic() < end and len([g for g in got if g[0] in ("note", "line")]) < 2:
            try:
                got.append(q.get(timeout=0.2))
            except queue.Empty:
                pass
        src.close()
        self.assertIn(("note", "source: http-feed stalled (3 failed polls)"), got)
        self.assertEqual([g for g in got if g[0] == "line" and '"note"' in g[1]], [])
        lis = L.Listener(L.load_config({"batchSec": 0}))
        lis.note("source: http-feed stalled (3 failed polls)")
        self.assertEqual(lis.lines[-1]["note"], "source: http-feed stalled (3 failed polls)")
        self.assertTrue(lis.shown(lis.lines[-1]))
        self.assertEqual((lis.routed, lis.outbox, lis.transcript), ([], [], []))
        lis.close()
        # the WATCH call source: the note is no event, the utterance is one
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        from watch_sources.call import CallSource
        events, cur, _ = CallSource({"name": "call", "kind": "call", "file": path}, {}, {}).poll(0)
        self.assertEqual([e.quote for e in events], ["a normal line of talk"])
        self.assertEqual(cur, os.path.getsize(path))


# ---------------------------------------------------------------- the real whosaid server

class RealWhosaidTest(Base):
    def test_mirrors_the_real_live_serve(self):
        lib = next((p for p in WHOSAID_LIBS if os.path.isfile(os.path.join(p, "live_remote.py"))), None)
        if lib is None:
            self.skipTest("no whosaid checkout with live_remote.py (set WHOSAID_LIB); looked in %s" % WHOSAID_LIBS)
        sys.path.insert(0, lib)
        saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in that checkout
        try:
            import live
            import live_remote
        except Exception as e:  # noqa: BLE001
            self.skipTest("cannot import whosaid live_remote from %s: %s" % (lib, e))
        finally:
            sys.dont_write_bytecode = saved
        if not hasattr(live_remote, "start_server"):
            self.skipTest("whosaid at %s has no live serve (needs v1.52.0)" % lib)
        from pathlib import Path
        into = Path(self.tmp) / "into"
        stem = "20261126T100000"
        d_live = into / live.LIVE_DIR
        md, jl = d_live / (stem + ".md"), d_live / (stem + ".jsonl")
        entries = [{"n": 1, "t": 1.0, "clock": "10:00:01", "who": "them", "text": "said before the feed began"}]
        live.write_session(md, jl, 1795694100.0, entries, False)
        srv = live_remote.start_server(into, "127.0.0.1", 0, TOKEN)
        try:
            url = "http://127.0.0.1:%d" % srv.server_address[1]
            d = self.config(url)
            p = self.poller(d)
            self.assertTrue(wait_for(lambda: self.status(d) == "ok", secs=15), self.log(d))
            time.sleep(0.5)
            entries.append({"n": 2, "t": 2.0, "clock": "10:00:02", "who": "them", "name": "Bob_Example",
                            "text": "can you take the rollback plan"})
            entries.append({"n": 3, "t": 3.0, "clock": "10:00:03", "who": "me", "text": "sure"})
            entries.append({"n": 4, "t": 4.0, "clock": "10:00:04", "who": "them",
                            "text": "a line the voice match has not named"})
            live.write_session(md, jl, 1795694100.0, entries, False)
            self.assertTrue(wait_for(lambda: len(self.utts(d)) == 2, secs=40), self.rows(d))
            u = self.utts(d)
            self.assertEqual([(r["speaker"], r["text"]) for r in u],
                             [("Bob_Example", "can you take the rollback plan"),
                              ("unknown", "a line the voice match has not named")])
            live.write_session(md, jl, 1795694100.0, entries, True)
            self.assertTrue(wait_for(lambda: self.status(d) == "gone", secs=40), self.log(d))
            self.assertEqual(self.stop(p), 0)
            self.assert_listen_shape(d)
            self.assert_no_token_anywhere([self.log(d)])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
