import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "lib", "fleet_mesh.py")

spec = importlib.util.spec_from_file_location("fleet_mesh", MESH)
mesh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mesh)

FAKE = "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done >> \"$0.argv\"\nprintf -- '--END--\\n' >> \"$0.argv\"\n"


class Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.server.posts.append((self.path, json.loads(self.rfile.read(n))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def calls(path):
    """Return list of argv lists recorded by a fake binary."""
    with open(path + ".argv") as f:
        chunks = f.read().split("--END--\n")
    return [c.split("\n")[:-1] for c in chunks if c]


class MeshBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meshtest-")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.codex = os.path.join(self.bin, "codex")
        self.tmux = os.path.join(self.bin, "tmux")
        for p in (self.codex, self.tmux):
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        self.srv.posts = []
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = {"FLEET_STATE": self.state, "FLEET_CODEX_BIN": self.codex,
                    "FLEET_TMUX_SOCKET": "testsock", "FLEET_NAME": "alpha",
                    "FLEET_DESK": "DECISIONS", "FLEET_ORCHESTRATOR": "ORCHESTRATOR",
                    "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        os.makedirs(self.state)
        self.roster = {
            "cx": {"name": "cx", "cli": "codex", "mode": "prompt", "pane": "%1", "session_id": "thr-1",
                   "port": None},
            "oc": {"name": "oc", "cli": "opencode", "mode": "prompt", "pane": "%2",
                   "session_id": "ses_abc", "port": self.port},
            "cl": {"name": "cl", "cli": "claude", "mode": "prompt", "pane": "%3", "session_id": "u-1",
                   "port": None},
            "DECISIONS": {"name": "DECISIONS", "cli": "claude", "mode": "desk", "pane": "%9",
                          "session_id": None, "port": None},
            "ORCHESTRATOR": {"name": "ORCHESTRATOR", "cli": "claude", "mode": "prompt", "pane": "%8",
                             "session_id": None, "port": None},
        }
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decision_rows(self):
        with open(os.path.join(self.state, "decisions.jsonl")) as f:
            return [json.loads(x) for x in f if x.strip()]

    def inbox_lines(self, name):
        p = os.path.join(self.state, "inbox", name + ".jsonl")
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]


class TestDelivery(MeshBase):
    def test_roster_load(self):
        r = mesh.load_roster()
        self.assertEqual(sorted(r), sorted(self.roster))
        self.assertEqual(r["oc"]["port"], self.port)

    def test_roster_missing_is_empty(self):
        os.remove(os.path.join(self.state, "roster.json"))
        self.assertEqual(mesh.load_roster(), {})

    def test_send_codex(self):
        res = mesh.deliver("alpha", "cx", "hello")
        self.assertTrue(os.path.exists(self.codex + ".argv"), "control: fake codex never ran")
        self.assertEqual(calls(self.codex),
                         [["queue", "--thread", "thr-1", "--message", "[fleet message from alpha] hello"]])
        self.assertIn("codex queue", res)
        self.assertIn("ok", res)
        line = self.inbox_lines("cx")[0]
        self.assertEqual((line["from"], line["to"], line["text"]), ("alpha", "cx", "hello"))
        self.assertTrue(line["id"] and line["at"])

    def test_send_opencode(self):
        res = mesh.deliver("alpha", "oc", "ping there")
        self.assertEqual(len(self.srv.posts), 1, "control: no POST recorded")
        path, body = self.srv.posts[0]
        self.assertEqual(path, "/session/ses_abc/prompt_async")
        self.assertEqual(body, {"parts": [{"type": "text", "text": "[fleet message from alpha] ping there"}]})
        self.assertIn("opencode", res)
        self.assertEqual(self.inbox_lines("oc")[0]["text"], "ping there")

    def test_send_claude(self):
        res = mesh.deliver("alpha", "cl", "secret body")
        self.assertTrue(os.path.exists(self.tmux + ".argv"), "control: fake tmux never ran")
        self.assertEqual(calls(self.tmux), [
            ["-L", "testsock", "send-keys", "-t", "%3", "-l",
             "[fleet] message from alpha: run peer_inbox to read it"],
            ["-L", "testsock", "send-keys", "-t", "%3", "Enter"],
        ])
        with open(self.tmux + ".argv") as fh:
            self.assertNotIn("secret body", fh.read())
        self.assertIn("tmux", res)
        self.assertEqual(self.inbox_lines("cl")[0]["text"], "secret body")

    def test_send_missing_target(self):
        res = mesh.deliver("alpha", "ghost", "anyone?")
        self.assertIn("queued (no live lane)", res)
        self.assertEqual(self.inbox_lines("ghost")[0]["text"], "anyone?")
        self.assertFalse(os.path.exists(self.codex + ".argv"))
        self.assertFalse(os.path.exists(self.tmux + ".argv"))
        self.assertEqual(self.srv.posts, [])

    def test_failed_wake_keeps_message(self):
        os.environ["FLEET_CODEX_BIN"] = os.path.join(self.bin, "nonexistent")
        res = mesh.deliver("alpha", "cx", "still here")
        self.assertIn("FAILED", res)
        self.assertEqual(self.inbox_lines("cx")[0]["text"], "still here")

    def test_inbox_cursor(self):
        mesh.deliver("alpha", "ghost", "one")
        mesh.deliver("alpha", "ghost", "two")
        first = mesh.read_inbox("ghost")
        self.assertEqual([m["text"] for m in first], ["one", "two"])
        self.assertEqual(mesh.read_inbox("ghost"), [])
        mesh.deliver("alpha", "ghost", "three")
        self.assertEqual([m["text"] for m in mesh.read_inbox("ghost")], ["three"])
        self.assertEqual(len(mesh.read_inbox("ghost", all_msgs=True)), 3)
        self.assertEqual(mesh.read_inbox("ghost"), [])  # all did not move cursor
        with open(os.path.join(self.state, "inbox", "ghost.cursor")) as f:
            self.assertEqual(int(f.read()), 3)

    def test_peer_list_unread(self):
        mesh.deliver("alpha", "cx", "a")
        mesh.deliver("alpha", "cx", "b")
        out = mesh.call_tool("peer_list", {})["content"][0]["text"]
        cx = [ln for ln in out.split("\n") if ln.startswith("cx ")][0]
        self.assertIn("unread=2", cx)
        mesh.read_inbox("cx")
        out = mesh.call_tool("peer_list", {})["content"][0]["text"]
        self.assertIn("unread=0", [ln for ln in out.split("\n") if ln.startswith("cx ")][0])

    def test_send_requires_sender(self):
        del os.environ["FLEET_NAME"]
        r = mesh.call_tool("peer_send", {"to": "cx", "text": "x"})
        self.assertTrue(r["isError"])
        self.assertFalse(os.path.exists(self.codex + ".argv"))


FLEET_FAKE = """#!/bin/sh
# fake fleet: log argv, then flip roster status of $2 to live (unless FAKE_FLEET_FAIL is set)
for a in "$@"; do printf '%s\\n' "$a"; done >> "$0.argv"
printf -- '--END--\\n' >> "$0.argv"
[ -n "$FAKE_FLEET_FAIL" ] && { echo "boom" >&2; exit 3; }
python3 - "$2" <<'PY'
import json, os, sys
p = os.path.join(os.environ["FLEET_STATE"], "roster.json")
r = json.load(open(p))
r[sys.argv[1]]["status"] = "live"
json.dump(r, open(p, "w"))
PY
"""


class TestReopen(MeshBase):
    def setUp(self):
        MeshBase.setUp(self)
        self.fleet = os.path.join(self.bin, "fleet")
        with open(self.fleet, "w") as f:
            f.write(FLEET_FAKE)
        os.chmod(self.fleet, 0o755)
        os.environ["FLEET_BIN"] = self.fleet
        self._saved["FLEET_BIN"] = None
        self._saved["FAKE_FLEET_FAIL"] = os.environ.get("FAKE_FLEET_FAIL")
        os.environ.pop("FAKE_FLEET_FAIL", None)
        self.roster["cx"]["status"] = "parked"
        self.roster["cl"]["status"] = "done"
        self.roster["oc"]["status"] = "live"
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)

    def test_parked_is_reopened_then_woken(self):
        res = mesh.deliver("alpha", "cx", "wake up")
        self.assertEqual(calls(self.fleet), [["reopen", "cx"]])
        self.assertIn("reopened", res)
        self.assertIn("codex queue", res)
        self.assertEqual(calls(self.codex),
                         [["queue", "--thread", "thr-1", "--message", "[fleet message from alpha] wake up"]])
        self.assertEqual(self.inbox_lines("cx")[0]["text"], "wake up")

    def test_done_is_reopened_too(self):
        res = mesh.deliver("alpha", "cl", "back to work")
        self.assertEqual(calls(self.fleet), [["reopen", "cl"]])
        self.assertIn("reopened", res)
        self.assertTrue(os.path.exists(self.tmux + ".argv"), "wake after reopen did not run")

    def test_live_is_not_reopened(self):
        res = mesh.deliver("alpha", "oc", "hi")
        self.assertFalse(os.path.exists(self.fleet + ".argv"), "control: live lane must not be reopened")
        self.assertNotIn("reopened", res)
        self.assertEqual(len(self.srv.posts), 1)

    def test_roster_reread_after_reopen(self):
        # reopen flips status only; a stale entry would still be woken, so prove the re-read via the status
        mesh.deliver("alpha", "cx", "one")
        self.assertEqual(mesh.load_roster()["cx"]["status"], "live")
        res = mesh.deliver("alpha", "cx", "two")
        self.assertNotIn("reopened", res)
        self.assertEqual(calls(self.fleet), [["reopen", "cx"]])

    def test_failed_reopen_keeps_inbox_and_says_so(self):
        os.environ["FAKE_FLEET_FAIL"] = "1"
        res = mesh.deliver("alpha", "cx", "stuck")
        self.assertIn("reopen FAILED", res)
        self.assertNotIn("reopened", res)
        self.assertEqual(self.inbox_lines("cx")[0]["text"], "stuck")
        self.assertFalse(os.path.exists(self.codex + ".argv"), "no wake after a failed reopen")

    def test_missing_fleet_bin_is_a_failed_reopen(self):
        os.environ["FLEET_BIN"] = os.path.join(self.bin, "nonexistent")
        res = mesh.deliver("alpha", "cx", "x")
        self.assertIn("reopen FAILED", res)
        self.assertEqual(self.inbox_lines("cx")[0]["text"], "x")

    def test_default_fleet_bin_is_repo_bin_fleet(self):
        del os.environ["FLEET_BIN"]
        self.assertEqual(mesh.fleet_bin(), os.path.join(ROOT, "bin", "fleet"))

    def test_peer_send_tool_reopens(self):
        r = mesh.call_tool("peer_send", {"to": "cx", "text": "via tool"})
        self.assertFalse(r["isError"], r)
        self.assertIn("reopened", r["content"][0]["text"])


class TestDecisions(MeshBase):
    def test_open_and_answer(self):
        r = mesh.call_tool("decision_open", {"question": "ship it?", "options": ["yes", "no"], "from": "cx"})
        self.assertFalse(r["isError"], r)
        desk = self.inbox_lines("DECISIONS")
        self.assertEqual(desk[0]["text"], "[decision Q1 from cx] ship it? options: yes | no")
        r2 = mesh.call_tool("decision_open", {"question": "again?", "from": "oc"})
        self.assertIn("Q2", r2["content"][0]["text"])
        rows = self.decision_rows()
        self.assertEqual([x["id"] for x in rows], ["Q1", "Q2"])
        self.assertEqual(rows[0]["status"], "open")
        self.assertEqual(rows[0]["options"], ["yes", "no"])

        os.environ["FLEET_NAME"] = "DECISIONS"
        a = mesh.call_tool("decision_answer", {"id": "Q1", "answer": "yes"})
        self.assertFalse(a["isError"], a)
        self.assertEqual(self.inbox_lines("cx")[-1]["text"], "[answer Q1] yes")
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "ORCHESTRATOR.jsonl")),
                         "answer must go only to the asker")
        rows = self.decision_rows()
        self.assertEqual((rows[0]["status"], rows[0]["answer"]), ("answered", "yes"))
        self.assertTrue(rows[0]["answeredAt"])
        self.assertEqual(rows[1]["status"], "open")
        # codex asker was woken
        self.assertIn("[fleet message from DECISIONS] [answer Q1] yes",
                      [c[-1] for c in calls(self.codex)])
        listed = mesh.call_tool("decision_list", {"status": "open"})["content"][0]["text"]
        self.assertIn("Q2", listed)
        self.assertNotIn("Q1", listed)

    def test_answer_to_orchestrator_asker_delivers_once(self):
        mesh.call_tool("decision_open", {"question": "q?", "from": "ORCHESTRATOR"})
        mesh.call_tool("decision_answer", {"id": "Q1", "answer": "ok"})
        self.assertEqual(len(self.inbox_lines("ORCHESTRATOR")), 1)

    def test_answer_appends_decisions_log(self):
        mesh.call_tool("decision_open", {"question": "q?", "from": "cx"})
        mesh.call_tool("decision_open", {"question": "r?", "from": "oc"})
        mesh.call_tool("decision_answer", {"id": "Q2", "answer": "exactly this, verbatim"})
        mesh.call_tool("decision_answer", {"id": "Q1", "answer": "second"})
        with open(os.path.join(self.state, "decisions-log.jsonl")) as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([r["id"] for r in rows], ["Q2", "Q1"])
        self.assertEqual(rows[0]["asker"], "oc")
        self.assertEqual(rows[0]["verbatim"], "exactly this, verbatim")
        self.assertEqual(rows[0]["relayedTo"], ["oc"])
        self.assertEqual(sorted(rows[0]), ["asker", "at", "id", "relayedTo", "verbatim"])
        self.assertTrue(rows[0]["at"])
        # control: nobody but the asker was messaged, and the orchestrator inbox is absent
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "ORCHESTRATOR.jsonl")))

    def test_failed_answer_writes_no_log(self):
        mesh.call_tool("decision_answer", {"id": "Q99", "answer": "x"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "decisions-log.jsonl")))

    def test_answer_unknown_id(self):
        r = mesh.call_tool("decision_answer", {"id": "Q99", "answer": "x"})
        self.assertTrue(r["isError"])


class TestStdio(MeshBase):
    def test_full_session(self):
        env = dict(os.environ)
        p = subprocess.Popen([sys.executable, MESH, "serve"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, env=env)

        def rpc(obj, expect=True):
            p.stdin.write(json.dumps(obj) + "\n")
            p.stdin.flush()
            if not expect:
                return None
            return json.loads(p.stdout.readline())

        try:
            r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
            self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
            self.assertEqual(r["result"]["serverInfo"]["name"], "fleet-mesh")
            self.assertEqual(r["result"]["capabilities"], {"tools": {}})
            r = rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                     "params": {"protocolVersion": "1999-01-01"}})
            self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
            rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
            r = rpc({"jsonrpc": "2.0", "id": 3, "method": "ping"})
            self.assertEqual(r["result"], {})
            r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
            tools = r["result"]["tools"]
            self.assertEqual(len(tools), 6)
            self.assertEqual(sorted(t["name"] for t in tools),
                             ["decision_answer", "decision_list", "decision_open",
                              "peer_inbox", "peer_list", "peer_send"])
            for t in tools:
                self.assertEqual(t["inputSchema"]["type"], "object")
                self.assertTrue(t["description"])
            r = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "peer_send", "arguments": {"to": "cx", "text": "via mcp"}}})
            self.assertFalse(r["result"]["isError"], r)
            self.assertEqual(r["result"]["content"][0]["type"], "text")
            self.assertEqual(calls(self.codex),
                             [["queue", "--thread", "thr-1", "--message", "[fleet message from alpha] via mcp"]])
            r = rpc({"jsonrpc": "2.0", "id": 6, "method": "bogus/method"})
            self.assertEqual(r["error"]["code"], -32601)
            self.assertEqual(r["id"], 6)
        finally:
            p.stdin.close()
            out_rest = p.stdout.read()
            p.wait(timeout=10)
            p.stderr.close()
            p.stdout.close()
        self.assertEqual(out_rest, "", "stdout carried non-JSON-RPC extras")

    def test_cli_send_inbox_roster(self):
        env = dict(os.environ)
        out = subprocess.check_output([sys.executable, MESH, "send", "alpha", "ghost", "hi", "there"],
                                      env=env, universal_newlines=True)
        self.assertIn("queued (no live lane)", out)
        out = subprocess.check_output([sys.executable, MESH, "inbox", "ghost"], env=env,
                                      universal_newlines=True)
        self.assertIn("hi there", out)
        out = subprocess.check_output([sys.executable, MESH, "inbox", "ghost"], env=env,
                                      universal_newlines=True)
        self.assertEqual(out, "")
        out = subprocess.check_output([sys.executable, MESH, "roster"], env=env, universal_newlines=True)
        self.assertEqual(sorted(json.loads(out)), sorted(self.roster))


if __name__ == "__main__":
    unittest.main()
