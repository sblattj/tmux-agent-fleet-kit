"""Issue #31: a fleet-mesh server notices the kit changed on disk. It re-execs in place (same pid, same stdio
session), or, when it can't, refuses typed wakes; `fleet_mesh.py stale` and `fleet tick` list stale servers.

Every server here runs from a scratch copy of the kit (so the test can edit "the kit on disk"), with a scratch
FLEET_STATE and a fake tmux on PATH. Live mesh servers are never touched: the stale scan only reports servers whose
script is this copy's lib/fleet_mesh.py.
"""
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_TMUX = r"""#!/bin/sh
for a in "$@"; do printf '%s\n' "$a"; done >> "$0.argv"
printf -- '--END--\n' >> "$0.argv"
case "$3" in
  capture-pane)
    if [ -f "$0.typed" ]; then cat "$0.typed"; else printf '\342\235\257 \n'; fi ;;
  display-message)
    if [ "$4" = "-p" ]; then printf '2\t0\t0\n'; fi ;;
  list-clients) printf 'client0\n' ;;
  send-keys)
    if [ "$6" = "-l" ]; then printf '\342\235\257 %s\n' "$7" > "$0.typed"; elif [ "$6" = "Enter" ]; then rm -f "$0.typed"; fi ;;
  set-buffer) printf '%s' "$7" > "$0.buf" ;;
  paste-buffer) { printf '\342\235\257 '; cat "$0.buf"; printf '\n'; } > "$0.typed"; rm -f "$0.buf" ;;
esac
"""


def h12(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


class Server:
    """A `fleet_mesh.py serve` child on pipes, spoken to line by line."""

    def __init__(self, mesh, env):
        self.p = subprocess.Popen([sys.executable, mesh, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env)
        self.n = 0

    def send_raw(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def read(self, timeout=15):
        r, _, _ = select.select([self.p.stdout], [], [], timeout)
        if not r:
            raise AssertionError("no reply from the mesh server within %ss" % timeout)
        line = self.p.stdout.readline()
        if not line:
            raise AssertionError("mesh server closed stdout (exit %s)" % self.p.poll())
        return json.loads(line)

    def req(self, method, params=None):
        self.n += 1
        self.send_raw((json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                   "params": params or {}}) + "\n").encode())
        resp = self.read()
        assert resp.get("id") == self.n, resp
        return resp

    def call(self, name, args):
        return self.req("tools/call", {"name": name, "arguments": args})

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()


class StaleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="meshstale-"))
        self.kit = os.path.join(self.tmp, "kit")
        shutil.copytree(os.path.join(ROOT, "lib"), os.path.join(self.kit, "lib"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "bin"), os.path.join(self.kit, "bin"))
        self.mesh = os.path.join(self.kit, "lib", "fleet_mesh.py")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        self.tmux = os.path.join(self.bin, "tmux")
        with open(self.tmux, "w") as f:
            f.write(FAKE_TMUX)
        os.chmod(self.tmux, 0o755)
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"cl": {"name": "cl", "cli": "claude", "mode": "prompt", "pane": "%3", "session_id": "u-1",
                              "port": None}}, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        self.env.update({"FLEET_STATE": self.state, "FLEET_TMUX_SOCKET": "testsock", "FLEET_NAME": "alpha",
                         "FLEET_CLAUDE_JSON": os.path.join(self.tmp, "claude.json"),
                         "CODEX_HOME": os.path.join(self.tmp, "codex"), "KIRO_HOME": os.path.join(self.tmp, "kiro"),
                         "FLEET_AGY_HOME": os.path.join(self.tmp, "agy"),
                         "XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg"),
                         "FLEET_WAKE_SPAWN": "0", "FLEET_WAKE_QUIET": "0", "CLAUDECODE": "",
                         "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")})
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **extra):
        env = dict(self.env, **extra)
        s = Server(self.mesh, env)
        self.servers.append(s)
        s.req("initialize", {"protocolVersion": "2025-06-18"})
        return s

    def edit_kit(self, old='"version": "1.0.0"', new='"version": "1.0.0-upgraded"'):
        with open(self.mesh) as f:
            src = f.read()
        self.assertIn(old, src)
        with open(self.mesh, "w") as f:
            f.write(src.replace(old, new, 1))
        future = time.time() + 5
        os.utime(self.mesh, (future, future))

    def record(self, pid):
        with open(os.path.join(self.state, "mesh", "%d.json" % pid)) as f:
            return json.load(f)

    def keys(self):
        try:
            with open(self.tmux + ".argv") as f:
                chunks = f.read().split("--END--\n")
        except OSError:
            return [], []
        calls = [c.split("\n")[:-1] for c in chunks if c]
        return [c for c in calls if "send-keys" in c], calls

    def stale_out(self):
        return subprocess.run([sys.executable, self.mesh, "stale"], capture_output=True, text=True,
                              env=self.env, timeout=60).stdout


class TestRecord(StaleBase):
    def test_server_records_the_code_hash_it_loaded(self):
        s = self.start()
        rec = self.record(s.p.pid)
        self.assertEqual(rec["hash"], h12(self.mesh))
        self.assertEqual(rec["lane"], "alpha")
        self.assertEqual(rec["file"], self.mesh)

    def test_a_fresh_server_is_not_listed_stale(self):
        self.start()
        self.assertNotIn("STALE", self.stale_out())


class TestReexec(StaleBase):
    def test_reexec_in_place_keeps_pid_and_stdio_session(self):
        s = self.start()
        pid, old = s.p.pid, h12(self.mesh)
        self.assertEqual(s.req("initialize", {})["result"]["serverInfo"]["version"], "1.0.0")
        self.edit_kit()
        # two requests in one write: the second sits in the read buffer when the server re-execs
        s.n += 2
        s.send_raw((json.dumps({"jsonrpc": "2.0", "id": s.n - 1, "method": "ping"}) + "\n" +
                    json.dumps({"jsonrpc": "2.0", "id": s.n, "method": "tools/call",
                                "params": {"name": "peer_inbox", "arguments": {}}}) + "\n").encode())
        r1, r2 = s.read(), s.read()
        self.assertEqual((r1["id"], r2["id"]), (s.n - 1, s.n))
        self.assertIn("result", r2)
        self.assertEqual(s.p.pid, pid)
        self.assertIsNone(s.p.poll(), "server must still be running")
        self.assertEqual(s.req("initialize", {})["result"]["serverInfo"]["version"], "1.0.0-upgraded",
                         "the new code must be what answers now")
        rec = self.record(pid)
        self.assertEqual(rec["hash"], h12(self.mesh))
        self.assertNotEqual(rec["hash"], old)
        self.assertEqual(rec["reexecs"], 1)
        self.assertNotIn("STALE", self.stale_out())

    def test_broken_code_on_disk_keeps_the_old_server_answering(self):
        s = self.start()
        pid = s.p.pid
        with open(self.mesh, "a") as f:
            f.write("\ndef broken(:\n")
        self.assertIn("result", s.req("ping"))
        self.assertEqual(s.p.pid, pid)
        self.assertEqual(s.req("initialize", {})["result"]["serverInfo"]["version"], "1.0.0")
        r = s.call("peer_send", {"to": "cl", "text": "hello"})
        self.assertIn("result", r)
        sk, _ = self.keys()
        self.assertEqual(sk, [], "a stale server that could not re-exec typed into a pane")


    def test_import_time_error_on_disk_keeps_the_old_server_answering(self):
        s = self.start()
        pid = s.p.pid
        with open(self.mesh, "a") as f:
            f.write("\nraise RuntimeError('half-written upgrade')\n")
        self.assertIn("result", s.req("ping"))
        self.assertEqual(s.p.pid, pid)
        self.assertIsNone(s.p.poll())
        self.assertEqual(s.req("initialize", {})["result"]["serverInfo"]["version"], "1.0.0")


class TestRefuse(StaleBase):
    def test_fresh_server_types_the_wake(self):  # control: the fake pane does receive typed wakes
        s = self.start()
        s.call("peer_send", {"to": "cl", "text": "hello"})
        sk, _ = self.keys()
        self.assertTrue(sk, "control: a fresh server should type the wake")

    def test_stale_server_with_reexec_off_never_types(self):
        s = self.start(FLEET_MESH_REEXEC="0")
        self.edit_kit()
        r = s.call("peer_send", {"to": "cl", "text": "hello"})
        text = r["result"]["content"][0]["text"]
        self.assertIn("stale", text)
        sk, calls = self.keys()
        self.assertEqual(sk, [], "stale server typed into the pane")
        self.assertTrue(any("display-message" in c and any("wake not typed" in a for a in c) for c in calls),
                        "no tmux notice left")
        self.assertTrue(any("@fleet_unread" in c for c in calls), "no @fleet_unread on the pane")
        with open(os.path.join(self.state, "inbox", "cl.jsonl")) as f:
            self.assertIn("hello", f.read())


class TestList(StaleBase):
    def test_stale_lists_a_recorded_server_with_its_lane(self):
        s = self.start(FLEET_MESH_REEXEC="0")
        self.edit_kit()
        out = self.stale_out()
        self.assertIn("STALE mesh alpha pid %d: loaded " % s.p.pid, out)

    def test_stale_lists_a_pre_record_server_by_start_time(self):
        s = self.start(FLEET_MESH_REEXEC="0")
        os.unlink(os.path.join(self.state, "mesh", "%d.json" % s.p.pid))  # what a server from before #31 looks like
        self.edit_kit()
        out = self.stale_out()
        self.assertIn("STALE mesh alpha pid %d: started before the kit changed" % s.p.pid, out)

    def test_fleet_tick_lists_stale_servers(self):
        s = self.start(FLEET_MESH_REEXEC="0")
        self.edit_kit()
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({}, f)
        out = subprocess.run(["bash", os.path.join(self.kit, "bin", "fleet-lifecycle"), "tick"], capture_output=True,
                             text=True, env=dict(self.env, FLEET_CDP_URL="http://127.0.0.1:9"), timeout=60).stdout
        self.assertIn("STALE mesh alpha pid %d" % s.p.pid, out)


if __name__ == "__main__":
    unittest.main()
