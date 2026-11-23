"""Tests for the WATCH page (lib/fleet_watch_serve.py, `fleet watch serve`), four-desks phase 7.

Acceptance (docs/design/four-desks.md section 7, phase 7): the page is served on loopback with a token (403 without
one), and an action in the page and the same action in the terminal status pane end in the same state (same test
shape as test/test_decisions.py TestSameStateAsDesk).

The page drives the real lib/fleet_watch.py verbs; the pane path is fleet_watch.pane_key, the function the status
pane calls for a key. Every test runs on a temp FLEET_STATE with a stub tmux and a scrubbed env.
Run: python3 -m unittest test/test_watch_serve.py
"""
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
SERVE = os.path.join(LIB, "fleet_watch_serve.py")
sys.path.insert(0, LIB)
import fleet_watch_contract as contract  # noqa: E402

FAKE = "#!/bin/sh\nexit 0\n"
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")

CONFIG = {
    "me": {"names": ["Ada Example"], "ids": {}},
    "defaults": {"pollSec": 900},
    "sources": [
        {"name": "acme-cmd", "kind": "command", "argv": ["true"], "timeoutSec": 10},
        {"name": "acme-calls", "kind": "call"},
    ],
    "quiet": {"hours": "19:00-08:00", "decideHolds": True},
}
CURSORS = {"acme-cmd": {"cursor": "c1", "nextPollAt": "2026-11-23T09:00:00Z"},
           "acme-calls": {"cursor": None, "nextPollAt": "2026-11-23T09:30:00Z"}}
HELD = [  # fleet_watch's held.jsonl row: {question, watch: {sourceKind, source, ref, url, eid}}
    {"question": 'acme-cmd PROJ-1: Bea Example asks "@Ada Example can you approve the retry cap?"',
     "watch": {"sourceKind": "command", "source": "acme-cmd", "ref": "PROJ-1",
               "url": "https://acme.example.com/PROJ-1",
               "eid": "command:acme-cmd:PROJ-1:2026-11-22T20:00:00Z:comment"}},
    {"question": 'acme-cmd PROJ-2: Cy Example asks "@Ada Example ship the docs fix alone?"',
     "watch": {"sourceKind": "command", "source": "acme-cmd", "ref": "PROJ-2",
               "url": "https://acme.example.com/PROJ-2",
               "eid": "command:acme-cmd:PROJ-2:2026-11-22T21:00:00Z:comment"}},
]


def scrubbed_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FLEET_") and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    env.update(extra)
    return env


def load_watch():
    import fleet_watch  # the WATCH core; a missing module fails these tests, it never skips them
    return fleet_watch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="watchserve-")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for b in ("tmux", "notify"):
            p = os.path.join(self.bin, b)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.env = {"FLEET_STATE": self.state, "FLEET_TMUX_SOCKET": "watchserve-none",
                    "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self._saved = {k: os.environ.get(k) for k in list(self.env) + ["FLEET_NAME", "FLEET_ROLE"]}
        os.environ.update(self.env)
        os.environ.pop("FLEET_NAME", None)
        os.environ.pop("FLEET_ROLE", None)
        self.fixture()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fixture(self):
        w = contract.state_paths(self.state)
        os.makedirs(w["dir"], exist_ok=True)
        with open(w["config"], "w") as f:
            json.dump(CONFIG, f)
        with open(w["cursors"], "w") as f:
            json.dump(CURSORS, f)
        with open(os.path.join(w["dir"], "held.jsonl"), "w") as f:
            for e in HELD:
                f.write(json.dumps(e) + "\n")

    def reset(self):
        shutil.rmtree(self.state)
        self.fixture()

    def tree(self):
        """Every file under FLEET_STATE (the page's URL file aside), with ISO timestamps masked: the state both
        front ends must agree on. sqlite files compare by their row dump."""
        out = {}
        for d, _, files in os.walk(self.state):
            for fn in sorted(files):
                p = os.path.join(d, fn)
                rel = os.path.relpath(p, self.state)
                if rel == "watch-ui.url" or fn.endswith((".lock", "-journal", "-wal", "-shm")) or fn == "mesh.log":
                    continue
                if fn.endswith(".db"):
                    import sqlite3
                    con = sqlite3.connect(p)
                    out[rel] = sorted(con.iterdump())
                    con.close()
                    continue
                with open(p, "rb") as f:
                    out[rel] = ISO.sub("<ts>", f.read().decode("utf-8", "replace"))
        return out

    def snap(self):
        s = load_watch().snapshot(self.state)
        s = json.loads(ISO.sub("<ts>", json.dumps(s, sort_keys=True)))
        s.pop("at", None)
        return s


class Server:
    def __init__(self, t, args=(), env=None):
        self.errpath = os.path.join(t.tmp, "serve.%d.err" % time.time_ns())
        self.err = open(self.errpath, "w")  # a file, not a pipe: the server logs every request to stderr
        self.p = subprocess.Popen([sys.executable, SERVE, "serve", "--port", "0"] + list(args),
                                  env=scrubbed_env(env or t.env), stdout=subprocess.PIPE, stderr=self.err,
                                  universal_newlines=True)
        line = self.p.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)", line)
        if not m:
            self.stop()
            with open(self.errpath) as f:
                raise AssertionError("no URL line: %r %s" % (line, f.read()))
        self.port, self.token = int(m.group(1)), m.group(2)

    def req(self, method, path, body=None, token=True, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Fleet-Token"] = self.token
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, data, hdrs

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=10)
        self.p.stdout.close()
        self.err.close()


def run_serve(t, *args, env=None, timeout=20):
    return subprocess.run([sys.executable, SERVE, "serve"] + list(args), env=scrubbed_env(env or t.env),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)


# ------------------------------------------------------------------ loopback + token

class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()

    def test_listens_on_loopback_only(self):
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(self.s.p.pid), "-iTCP", "-sTCP:LISTEN"],
                             stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("127.0.0.1:%d" % self.s.port, out)
        self.assertNotIn("*:%d" % self.s.port, out)

    def test_403_without_token_200_with_it(self):
        self.assertEqual(self.s.req("GET", "/", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/api/state?t=wrong", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/api/state", token=False, headers={"X-Fleet-Token": "x" * 32})[0], 403)
        # controls: the right token opens both, by query and by header
        self.assertEqual(self.s.req("GET", "/?t=%s" % self.s.token, token=False)[0], 200)
        self.assertEqual(self.s.req("GET", "/api/state")[0], 200)

    def test_post_needs_header_token_and_refused_posts_change_nothing(self):
        before = self.tree()
        for path, body in (("/api/pause", {"source": "acme-cmd"}), ("/api/poll", {"source": "acme-cmd"}),
                           ("/api/release", {})):
            self.assertEqual(self.s.req("POST", path, body, token=False)[0], 403, path)
            # a link click carries only the query token: never enough for a POST
            self.assertEqual(self.s.req("POST", path + "?t=" + self.s.token, body, token=False)[0], 403, path)
            st = self.s.req("POST", path, body, headers={"Host": "evil.example:%d" % self.s.port})[0]
            self.assertEqual(st, 403, "foreign Host refused (DNS rebinding): " + path)
            st = self.s.req("POST", path, body, headers={"Origin": "http://evil.example"})[0]
            self.assertEqual(st, 403, "foreign Origin refused: " + path)
        self.assertEqual(self.tree(), before, "no refused request changed any state")
        # control: the same POST with the header token does change state
        st, body, _ = self.s.req("POST", "/api/pause", {"source": "acme-cmd"})
        self.assertEqual(st, 200, body)
        self.assertNotEqual(self.tree(), before)

    def test_foreign_host_refused_on_get(self):
        st = self.s.req("GET", "/api/state", headers={"Host": "evil.example:%d" % self.s.port})[0]
        self.assertEqual(st, 403)
        st = self.s.req("GET", "/api/state", headers={"Host": "localhost:%d" % self.s.port})[0]
        self.assertEqual(st, 200, "control: a loopback Host name is accepted")

    def test_state_is_the_contract_snapshot(self):
        st, body, _ = self.s.req("GET", "/api/state")
        self.assertEqual(st, 200, body)
        s = json.loads(body)
        self.assertEqual(contract.validate_snapshot(s), [])
        self.assertEqual(sorted(x["name"] for x in s["sources"]), ["acme-calls", "acme-cmd"])
        self.assertNotIn("auth", body)
        # same data the pane reads
        got = json.loads(ISO.sub("<ts>", json.dumps(s, sort_keys=True)))
        got.pop("at", None)
        self.assertEqual(got, self.snap())

    def test_unknown_source_is_404_and_changes_nothing(self):
        before = self.tree()
        st, body, _ = self.s.req("POST", "/api/pause", {"source": "no-such-source"})
        self.assertEqual(st, 404, body)
        st, _, _ = self.s.req("POST", "/api/pause", {})
        self.assertEqual(st, 400, "a missing source is a 400, not a pause of everything")
        st, _, _ = self.s.req("POST", "/api/nuke", {"source": "acme-cmd"})
        self.assertEqual(st, 404, "only the pane's verbs are reachable")
        self.assertEqual(self.tree(), before)

    def test_page_shape(self):
        st, html, hdrs = self.s.req("GET", "/?t=%s" % self.s.token, token=False)
        self.assertEqual(st, 200)
        self.assertIn("frame-ancestors 'none'", hdrs.get("Content-Security-Policy", ""))
        self.assertEqual(hdrs.get("Cache-Control"), "no-store")
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html, "feed text never goes through innerHTML")
        for path in ("/api/poll", "/api/pause", "/api/resume", "/api/release"):
            self.assertIn(path, html)
        self.assertIn("safeUrl", html, "event links are limited to http(s)")
        self.assertNotIn("\u2014", html)

    def test_url_file_is_private_and_removed_on_exit(self):
        p = os.path.join(self.state, "watch-ui.url")
        self.assertTrue(os.path.exists(p))
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with open(p) as f:
            self.assertIn(self.s.token, f.read())
        self.s.stop()
        self.assertFalse(os.path.exists(p), "SIGTERM removes the URL file")

    def test_port_in_use_is_a_one_line_error(self):
        p = run_serve(self, "--port", str(self.s.port))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("can't listen", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_token_never_logged(self):
        self.s.req("GET", "/?t=%s" % self.s.token, token=False)
        self.s.req("GET", "/api/state")
        self.s.stop()
        with open(self.s.errpath) as f:
            log = f.read()
        self.assertIn("GET /api/state", log, "control: requests are logged")
        self.assertNotIn(self.s.token, log)


class TestBindAndRole(Base):
    def test_non_loopback_bind_refused(self):
        p = run_serve(self, "--port", "0", "--bind", "0.0.0.0")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("loopback only", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "watch-ui.url")))

    def test_worker_role_refused_watch_role_allowed(self):
        p = run_serve(self, "--port", "0", env=dict(self.env, FLEET_ROLE="worker"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("role worker", p.stderr)
        s = Server(self, env=dict(self.env, FLEET_ROLE="watch"))  # control
        try:
            self.assertEqual(s.req("GET", "/api/state")[0], 200)
        finally:
            s.stop()


# ------------------------------------------------------------------ same state as the status pane

class TestSameStateAsPane(Base):
    """Each page action and the pane key for it, on two fresh copies of one fixture, end in the same state."""

    CASES = [
        ("pause", "/api/pause", {"source": "acme-cmd"}, "z", "acme-cmd"),
        ("resume", "/api/resume", {"source": "acme-cmd"}, "r", "acme-cmd"),
        ("poll", "/api/poll", {"source": "acme-calls"}, "p", "acme-calls"),
        ("release", "/api/release", {}, "h", "acme-cmd"),
    ]

    def page(self, steps):
        s = Server(self)
        try:
            for path, body in steps:
                st, data, _ = s.req("POST", path, body)
                self.assertEqual(st, 200, "%s %s" % (path, data))
        finally:
            s.stop()
        return self.tree(), self.snap()

    def pane(self, keys):
        watch = load_watch()
        for key, sel in keys:
            watch.pane_key(self.state, key, sel)
        return self.tree(), self.snap()

    def test_each_page_action_and_pane_key_end_in_the_same_state(self):
        for name, path, body, key, sel in self.CASES:
            with self.subTest(verb=name):
                self.reset()
                pre = [("/api/pause", {"source": "acme-cmd"})] if name == "resume" else []
                prek = [("z", "acme-cmd")] if name == "resume" else []
                start = self.tree()
                ui = self.page(pre + [(path, body)])
                self.assertNotEqual(ui[0], start, "the page action changed state (else equality proves nothing)")
                self.reset()
                tui = self.pane(prek + [(key, sel)])
                self.assertEqual(ui, tui)

    def test_a_sequence_ends_in_the_same_state(self):
        steps = [("/api/pause", {"source": "acme-cmd"}), ("/api/poll", {"source": "acme-calls"}),
                 ("/api/release", {}), ("/api/resume", {"source": "acme-cmd"}), ("/api/pause", {"source": "acme-calls"})]
        keys = [("z", "acme-cmd"), ("p", "acme-calls"), ("h", "acme-cmd"), ("r", "acme-cmd"), ("z", "acme-calls")]
        ui = self.page(steps)
        self.reset()
        self.assertEqual(ui, self.pane(keys))

    def test_control_different_actions_end_in_different_states(self):
        ui = self.page([("/api/pause", {"source": "acme-cmd"})])
        self.reset()
        other = self.pane([("z", "acme-calls")])  # same verb, other source
        self.assertNotEqual(ui, other)
        self.reset()
        noop = self.pane([("x", "acme-cmd")])  # a key that is not a verb
        self.assertNotEqual(ui, noop)

    def test_release_empties_held_and_paused_shows_in_snapshot(self):
        self.page([("/api/pause", {"source": "acme-cmd"}), ("/api/release", {})])
        s = load_watch().snapshot(self.state)
        src = {x["name"]: x for x in s["sources"]}
        self.assertTrue(src["acme-cmd"].get("paused"))
        self.assertFalse(src["acme-calls"].get("paused"))
        self.assertEqual(s["held"], 0)
        held = os.path.join(self.state, "watch", "held.jsonl")
        if os.path.exists(held):  # emptied or removed: either way nothing stays held
            with open(held) as f:
                self.assertEqual(f.read().strip(), "")
        with open(os.path.join(self.state, "decisions.jsonl")) as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(rows), 2, "one decision per held ask")
        self.assertIn("PROJ-1", json.dumps(rows[0]))
        self.assertIn("PROJ-2", json.dumps(rows[1]))
        # control: a second release has nothing left, opens nothing and says so
        s = Server(self)
        try:
            st, body, _ = s.req("POST", "/api/release", {})
        finally:
            s.stop()
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["result"], [])
        with open(os.path.join(self.state, "decisions.jsonl")) as f:
            self.assertEqual(len([ln for ln in f if ln.strip()]), 2)


if __name__ == "__main__":
    unittest.main()
