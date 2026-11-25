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

FAKE = "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done >> \"$0.argv\"\nprintf -- '--END--\\n' >> \"$0.argv\"\nif [ \"$1 $2 $3\" = \"-L testsock capture-pane\" ] && [ -f \"$0.screen\" ]; then cat \"$0.screen\"; fi\n"

# a fake tmux that behaves like an idle prompt: capture-pane shows `❯ ` (or $0.screen), display-message reports the
# cursor right after the glyph, `send-keys -l` (or set-buffer + paste-buffer) makes the input row read the typed
# text, Enter clears it
FAKE_TMUX = r"""#!/bin/sh
for a in "$@"; do printf '%s\n' "$a"; done >> "$0.argv"
printf -- '--END--\n' >> "$0.argv"
case "$3" in
  capture-pane)
    if [ -f "$0.typed" ]; then cat "$0.typed"; elif [ -f "$0.screen" ]; then cat "$0.screen"; else printf '\342\235\257 \n'; fi ;;
  display-message)
    if [ "$4" = "-p" ]; then printf '2\t0\t0\n'; fi ;;
  send-keys)
    if [ "$6" = "-l" ]; then printf '\342\235\257 %s\n' "$7" > "$0.typed"; elif [ "$6" = "Enter" ]; then rm -f "$0.typed"; fi ;;
  set-buffer) printf '%s' "$7" > "$0.buf" ;;
  paste-buffer) { printf '\342\235\257 '; cat "$0.buf"; printf '\n'; } > "$0.typed"; rm -f "$0.buf" ;;
esac
"""


def keys(path):
    """Only the calls a fake tmux recorded that put keys or text into a pane: send-keys, and set-buffer +
    paste-buffer (a Claude pane's bracketed paste, issue #38). The reads around them are the guard, tested elsewhere."""
    return [c for c in calls(path) if "send-keys" in c or "set-buffer" in c or "paste-buffer" in c]


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
        for p, body in ((self.codex, FAKE), (self.tmux, FAKE_TMUX)):
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        self.srv.posts = []
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = {"FLEET_STATE": self.state, "FLEET_CODEX_BIN": self.codex,
                    "FLEET_TMUX_SOCKET": "testsock", "FLEET_NAME": "alpha",
                    "FLEET_DECIDE": "DECISIONS", "FLEET_ROUTE": "ORCHESTRATOR",
                    # has_mesh reads CLI configs: point every one at the temp dir, never the real home
                    "FLEET_CLAUDE_JSON": os.path.join(self.tmp, "claude.json"),
                    "CODEX_HOME": os.path.join(self.tmp, "codex"), "KIRO_HOME": os.path.join(self.tmp, "kiro"),
                    "FLEET_AGY_HOME": os.path.join(self.tmp, "agy"),
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg"),
                    # no detached wakers out of unit tests, no key-activity probe, and not a Claude caller
                    "FLEET_WAKE_SPAWN": "0", "FLEET_WAKE_QUIET": "0", "CLAUDECODE": "",
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
            "ki": {"name": "ki", "cli": "kiro", "mode": "prompt", "pane": "%4", "session_id": "sess_fake-1",
                   "port": None},
            "ag": {"name": "ag", "cli": "agy", "mode": "prompt", "pane": "%5",
                   "session_id": "00000000-0000-4000-8000-000000000005", "port": None},
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

    def note(self, frm, to):  # the wake line, built from the test's own state dir, not from fleet_mesh
        return "[fleet] message from %s: run peer_inbox to read it (no peer_inbox tool? read %s)" % (
            frm, os.path.join(self.state, "inbox", to + ".jsonl"))

    def test_mesh_lane_gets_the_short_wake_line(self):
        """mesh=yes (launch record passes fleet_mesh.py) types no fallback; a mesh=no lane gets it."""
        os.makedirs(os.path.join(self.state, "launch"))
        with open(os.path.join(self.state, "launch", "cl.sh"), "w") as fh:
            fh.write("exec claude --mcp-config /tmp/m.json  # fleet_mesh.py serve\n")
        self.assertTrue(mesh.has_mesh("cl", self.roster["cl"]))
        mesh.deliver("alpha", "cl", "hi")
        self.assertEqual(keys(self.tmux)[0][-1], "[fleet] message from alpha: run peer_inbox to read it")
        # control: kiro lane with no fleet server in its config and no roster flag
        self.assertFalse(mesh.has_mesh("ki", self.roster["ki"]))
        self.assertEqual(mesh.wake_note("alpha", "ki", self.roster["ki"]), self.note("alpha", "ki"))

    def test_has_mesh_evidence_sources(self):
        cl = self.roster["cl"]
        self.assertFalse(mesh.has_mesh("cl", cl))  # control: no launch record, no config
        with open(os.path.join(self.state, "launch.sh"), "w") as fh:  # a record for a different path is ignored
            fh.write("fleet_mesh.py")
        self.assertFalse(mesh.has_mesh("cl", cl))
        self.assertTrue(mesh.has_mesh("cl", dict(cl, mesh=True)))
        self.assertFalse(mesh.has_mesh("cl", dict(cl, mesh=False)))
        with open(os.environ["FLEET_CLAUDE_JSON"], "w") as fh:
            json.dump({"mcpServers": {"other": {}}}, fh)
        self.assertFalse(mesh.has_mesh("cl", cl))  # control: some other server only
        with open(os.environ["FLEET_CLAUDE_JSON"], "w") as fh:
            json.dump({"mcpServers": {"fleet": {}}}, fh)
        self.assertTrue(mesh.has_mesh("cl", cl))
        os.makedirs(os.path.join(self.tmp, "kiro", "settings"))
        with open(os.path.join(self.tmp, "kiro", "settings", "mcp.json"), "w") as fh:
            json.dump({"mcpServers": {"fleet": {}}}, fh)
        self.assertTrue(mesh.has_mesh("ki", self.roster["ki"]))
        self.assertFalse(mesh.has_mesh("ag", self.roster["ag"]))  # control: agy config absent

    def test_mesh_cli_lists_yes_and_no(self):
        os.makedirs(os.path.join(self.state, "launch"))
        with open(os.path.join(self.state, "launch", "cl.sh"), "w") as fh:
            fh.write("fleet_mesh.py")
        out = subprocess.run([sys.executable, MESH, "mesh"], capture_output=True, text=True, env=os.environ.copy())
        rows = dict(line.split() for line in out.stdout.splitlines())
        self.assertEqual(rows.get("cl"), "yes")
        self.assertEqual(rows.get("ki"), "no")  # control

    def test_wake_names_an_inbox_file_that_holds_the_message(self):
        """A lane with no fleet MCP reads the file the wake line names; that file must hold the message."""
        mesh.deliver("alpha", "cl", "body for a lane with no mcp")
        typed = keys(self.tmux)[0][-1]
        path = typed.split("no peer_inbox tool? read ", 1)[1].rstrip(")")
        self.assertTrue(path.startswith(self.state), path)
        with open(path) as fh:
            self.assertIn("body for a lane with no mcp", fh.read())
        # control: the file for another lane does not get it
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "ki.jsonl")))
        self.assertNotIn("body for a lane with no mcp", typed)  # still a pointer only

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
        buf = "fleet-wake-%d" % os.getpid()  # a Claude pane gets one bracketed paste, then Enter (issue #38)
        self.assertEqual(keys(self.tmux), [
            ["-L", "testsock", "set-buffer", "-b", buf, "--", self.note("alpha", "cl")],
            ["-L", "testsock", "paste-buffer", "-p", "-d", "-b", buf, "-t", "%3"],
            ["-L", "testsock", "send-keys", "-t", "%3", "Enter"],
        ])
        with open(self.tmux + ".argv") as fh:
            self.assertNotIn("secret body", fh.read())
        self.assertIn("tmux", res)
        self.assertEqual(self.inbox_lines("cl")[0]["text"], "secret body")

    def test_send_kiro(self):
        res = mesh.deliver("alpha", "ki", "kiro secret body")
        self.assertTrue(os.path.exists(self.tmux + ".argv"), "control: fake tmux never ran")
        self.assertEqual(keys(self.tmux), [
            ["-L", "testsock", "send-keys", "-t", "%4", "-l",
             self.note("alpha", "ki")],
            ["-L", "testsock", "send-keys", "-t", "%4", "Enter"],
        ])
        with open(self.tmux + ".argv") as fh:
            self.assertNotIn("kiro secret body", fh.read())
        self.assertFalse(os.path.exists(self.codex + ".argv"), "kiro must not go through codex queue")
        self.assertIn("tmux send-keys", res)
        self.assertEqual(self.inbox_lines("ki")[0]["text"], "kiro secret body")

    def test_send_agy(self):
        res = mesh.deliver("alpha", "ag", "agy secret body")
        self.assertTrue(os.path.exists(self.tmux + ".argv"), "control: fake tmux never ran")
        self.assertEqual(calls(self.tmux)[0], ["-L", "testsock", "capture-pane", "-p", "-t", "%5"])
        self.assertEqual(keys(self.tmux), [
            ["-L", "testsock", "send-keys", "-t", "%5", "-l",
             self.note("alpha", "ag")],
            ["-L", "testsock", "send-keys", "-t", "%5", "Enter"],
        ])
        with open(self.tmux + ".argv") as fh:
            self.assertNotIn("agy secret body", fh.read())
        self.assertFalse(os.path.exists(self.codex + ".argv"), "agy must not go through codex queue")
        self.assertIn("tmux send-keys", res)
        self.assertEqual(self.inbox_lines("ag")[0]["text"], "agy secret body")

    def test_send_agy_never_types_into_a_blocking_screen(self):
        for screen in ("Terms of Service\n[x] Yes, I agree to help improve Antigravity CLI\n",
                       "Welcome to Antigravity CLI!\nChoose your color scheme:\n",
                       "Do you trust the contents of this project?\n", "Waiting for authentication...\n"):
            with open(self.tmux + ".screen", "w") as fh:
                fh.write(screen)
            if os.path.exists(self.tmux + ".argv"):
                os.remove(self.tmux + ".argv")
            res = mesh.deliver("alpha", "ag", "x")
            self.assertIn("FAILED", res, screen)
            self.assertIn("not typing into it", res, screen)
            sent = [c for c in calls(self.tmux) if "send-keys" in c]
            self.assertEqual(sent, [], "typed into %r" % screen)
        # control: an idle agy prompt still gets the wake
        with open(self.tmux + ".screen", "w") as fh:
            fh.write("> \n" + "\u2500" * 40 + "\n? for shortcuts\n")
        os.remove(self.tmux + ".argv")
        res = mesh.deliver("alpha", "ag", "x")
        self.assertNotIn("FAILED", res)
        self.assertEqual(len([c for c in calls(self.tmux) if "send-keys" in c]), 2)

    def test_send_agy_without_pane_fails_loud(self):
        r = json.load(open(os.path.join(self.state, "roster.json")))
        r["ag"]["pane"] = None
        json.dump(r, open(os.path.join(self.state, "roster.json"), "w"))
        res = mesh.deliver("alpha", "ag", "x")
        self.assertIn("FAILED", res)
        self.assertFalse(os.path.exists(self.tmux + ".argv"))

    def test_send_kiro_without_pane_fails_loud(self):
        r = json.load(open(os.path.join(self.state, "roster.json")))
        r["ki"]["pane"] = None
        json.dump(r, open(os.path.join(self.state, "roster.json"), "w"))
        res = mesh.deliver("alpha", "ki", "x")
        self.assertIn("FAILED", res)
        self.assertFalse(os.path.exists(self.tmux + ".argv"))

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

    def test_peer_list_side_runs_under_parent(self):
        # issue #17: one running side run S1 and one done S2 under lane cx; only S1 shows, indented under cx
        rows = [{"id": "S1", "parent": "cx", "host": "codex", "model": "gpt-x", "pid": 1, "boundSec": 1800,
                 "startedAt": "2026-01-01T00:00:00Z", "status": "running", "endedAt": None,
                 "tokensIn": 2000000, "tokensOut": 100000},
                {"id": "S2", "parent": "cx", "host": "agy", "pid": 1, "boundSec": 600,
                 "startedAt": "2026-01-01T00:00:00Z", "status": "done", "endedAt": "2026-01-01T00:05:00Z"}]
        with open(os.path.join(self.state, "sides.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        lines = mesh.call_tool("peer_list", {})["content"][0]["text"].split("\n")
        i = [n for n, ln in enumerate(lines) if ln.startswith("cx ")][0]
        self.assertRegex(lines[i + 1], r"^  -> S1 codex gpt-x \d+m/30m 2\.1M tok$")
        self.assertFalse(any("S2" in ln for ln in lines), lines)
        # parked parent: the child line is flagged ORPHAN
        self.roster["cx"]["status"] = "parked"
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)
        lines = mesh.call_tool("peer_list", {})["content"][0]["text"].split("\n")
        i = [n for n, ln in enumerate(lines) if ln.startswith("cx ")][0]
        self.assertTrue(lines[i + 1].startswith("  -> S1 ") and lines[i + 1].endswith(" ORPHAN"), lines[i + 1])
        # control: no sides.jsonl, no child lines
        os.remove(os.path.join(self.state, "sides.jsonl"))
        self.assertNotIn("->", mesh.call_tool("peer_list", {})["content"][0]["text"])

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


class TestMirror(MeshBase):
    """Native SendMessage -> mesh inbox copy (role PostToolUse hook runs `fleet_mesh.py mirror`)."""

    def setUp(self):
        super().setUp()
        self.ccdir = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(self.ccdir, "sessions"))
        os.environ["CLAUDE_CONFIG_DIR"] = self.ccdir
        self.roster["cl"]["pid"] = 4242
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)
        self.sock = os.path.join(self.tmp, "4242.sock")
        self.write_session(4242, "u-1", self.sock)

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        super().tearDown()

    def write_session(self, pid, sid, sock):
        with open(os.path.join(self.ccdir, "sessions", "%d.json" % pid), "w") as f:
            json.dump({"pid": pid, "sessionId": sid, "messagingSocketPath": sock}, f)

    def hook(self, to, msg="native body", tool="SendMessage"):
        return {"hook_event_name": "PostToolUse", "tool_name": tool,
                "tool_input": {"to": to, "message": msg, "summary": "s"}}

    def assert_no_wake(self):
        self.assertFalse(os.path.exists(self.tmux + ".argv"), "mirror must not wake (tmux ran)")
        self.assertFalse(os.path.exists(self.codex + ".argv"), "mirror must not wake (codex ran)")

    def test_mirror_by_name(self):
        self.assertEqual(mesh.mirror(self.hook("cl")), "mirrored alpha -> cl")
        rec = self.inbox_lines("cl")[0]
        self.assertEqual((rec["from"], rec["to"], rec["text"], rec["via"]), ("alpha", "cl", "native body", "native"))
        self.assert_no_wake()

    def test_mirror_strips_ref(self):
        self.assertEqual(mesh.mirror(self.hook("cl [3fa9c1]")), "mirrored alpha -> cl")

    def test_mirror_by_uds_socket(self):
        self.assertEqual(mesh.mirror(self.hook("uds:" + self.sock)), "mirrored alpha -> cl")
        self.assert_no_wake()

    def test_uds_with_stale_session_is_not_mirrored(self):
        self.write_session(4242, "someone-else", self.sock)  # pid reused by another session
        self.assertTrue(mesh.mirror(self.hook("uds:" + self.sock)).startswith("skip"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "cl.jsonl")))

    def test_uds_unknown_socket_is_not_mirrored(self):
        self.assertTrue(mesh.mirror(self.hook("uds:/tmp/cc-socks/1.sock")).startswith("skip"))

    def test_non_lane_and_self_are_skipped(self):
        self.assertTrue(mesh.mirror(self.hook("some-session")).startswith("skip"))
        self.assertTrue(mesh.mirror(self.hook("alpha")).startswith("skip"))
        self.assertTrue(mesh.mirror(self.hook("cl", tool="Bash")).startswith("skip"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "cl.jsonl")))

    def test_no_fleet_name_is_skipped(self):
        os.environ.pop("FLEET_NAME")
        self.assertTrue(mesh.mirror(self.hook("cl")).startswith("skip"))

    def test_peer_inbox_marks_native_copy(self):
        mesh.mirror(self.hook("cl"))
        out = mesh.t_peer_inbox({"name": "cl"})
        self.assertIn("from alpha (native copy): native body", out)
        mesh.deliver("alpha", "cx", "mesh body")
        self.assertNotIn("native copy", mesh.t_peer_inbox({"name": "cx"}))  # control

    def test_cli_mirror_always_exits_zero(self):
        env = dict(os.environ)
        for stdin in (json.dumps(self.hook("cl")), "not json", "[]"):
            p = subprocess.run([sys.executable, MESH, "mirror"], input=stdin, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 0, stdin)
        self.assertEqual(len(self.inbox_lines("cl")), 1)


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
        self.roster["ki"]["status"] = "parked"
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

    def test_parked_kiro_is_reopened_then_woken(self):
        res = mesh.deliver("alpha", "ki", "wake kiro")
        self.assertEqual(calls(self.fleet), [["reopen", "ki"]])
        self.assertIn("reopened", res)
        self.assertIn("tmux send-keys", res)
        self.assertEqual(keys(self.tmux), [
            ["-L", "testsock", "send-keys", "-t", "%4", "-l",
             self.note("alpha", "ki")],
            ["-L", "testsock", "send-keys", "-t", "%4", "Enter"],
        ])
        self.assertEqual(mesh.load_roster()["ki"]["status"], "live")

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
        with open(os.path.join(self.state, "decisions-log.jsonl")) as f:
            self.assertEqual(json.loads(f.read().splitlines()[-1])["by"], "DECISIONS", "the log row names who answered")
        self.assertTrue(rows[0]["answeredAt"])
        self.assertEqual(rows[1]["status"], "open")
        # codex asker was woken
        self.assertIn("[fleet message from DECISIONS] [answer Q1] yes",
                      [c[-1] for c in calls(self.codex)])
        listed = mesh.call_tool("decision_list", {"status": "open"})["content"][0]["text"]
        self.assertIn("Q2", listed)
        self.assertNotIn("Q1", listed)

    def test_decision_open_keeps_only_the_watch_keys_and_http_urls(self):
        mesh.call_tool("decision_open", {"question": "acme/widget#12 review?", "from": "WATCH", "watch": {
            "sourceKind": "github", "source": "acme-gh", "ref": "acme/widget#12", "eid": "github:acme-gh:acme/widget#12:x:y",
            "url": "https://github.example.com/acme/widget/pull/12", "auth": "env:GH_TOKEN", "token": "x"}})
        mesh.call_tool("decision_open", {"question": "PROJ-1?", "from": "WATCH",
                                         "watch": {"sourceKind": "jira", "ref": "PROJ-1", "url": "javascript:alert(1)"}})
        mesh.call_tool("decision_open", {"question": "lane call", "from": "cx"})
        rows = self.decision_rows()
        self.assertEqual(rows[0]["watch"], {"sourceKind": "github", "source": "acme-gh", "ref": "acme/widget#12",
                                            "eid": "github:acme-gh:acme/widget#12:x:y",
                                            "url": "https://github.example.com/acme/widget/pull/12"},
                         "unknown keys (auth, token) are dropped")
        self.assertEqual(rows[1]["watch"], {"sourceKind": "jira", "ref": "PROJ-1"}, "a non-http(s) url is dropped")
        self.assertNotIn("watch", rows[2], "control: a call with no watch object has none")
        r = mesh.call_tool("decision_open", {"question": "bad", "from": "WATCH", "watch": "github"})
        self.assertTrue(r["isError"], "a non-object watch is refused")

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
        self.assertEqual(sorted(rows[0]), ["asker", "at", "by", "id", "relayedTo", "verbatim"])
        self.assertTrue(rows[0]["at"])
        # control: nobody but the asker was messaged, and the orchestrator inbox is absent
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "ORCHESTRATOR.jsonl")))

    def test_failed_answer_writes_no_log(self):
        mesh.call_tool("decision_answer", {"id": "Q99", "answer": "x"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "decisions-log.jsonl")))

    def test_answer_unknown_id(self):
        r = mesh.call_tool("decision_answer", {"id": "Q99", "answer": "x"})
        self.assertTrue(r["isError"])


class TestRetiredDeskNames(MeshBase):
    """A lane launched before the desk rename still has FLEET_DECIDE=DECISIONS / FLEET_ROUTE=ORCHESTRATOR."""

    def setUp(self):
        super().setUp()
        del self.roster["DECISIONS"], self.roster["ORCHESTRATOR"]
        self.roster["DECIDE"] = {"name": "DECIDE", "cli": "claude", "mode": "desk", "pane": "%9",
                                 "session_id": None, "port": None}
        self.roster["ROUTE"] = {"name": "ROUTE", "cli": "claude", "mode": "desk", "pane": "%8",
                                "session_id": None, "port": None}
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)

    def path(self, name):
        return os.path.join(self.state, "inbox", "%s.jsonl" % name)

    def test_decision_open_with_old_env_lands_in_decide(self):
        r = mesh.call_tool("decision_open", {"question": "ship it?", "from": "cx"})
        self.assertFalse(r.get("isError"), r)
        lines = self.inbox_lines("DECIDE")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["to"], "DECIDE")
        self.assertIn("-DECIDE-1", lines[0]["id"])
        self.assertIn("ship it?", lines[0]["text"])
        self.assertFalse(os.path.exists(self.path("DECISIONS")), "nothing is stranded under the old name")

    def test_peer_send_to_orchestrator_lands_in_route(self):
        mesh.call_tool("peer_send", {"to": "ORCHESTRATOR", "text": "status: done", "from": "cx"})
        self.assertEqual([m["text"] for m in self.inbox_lines("ROUTE")], ["status: done"])
        self.assertFalse(os.path.exists(self.path("ORCHESTRATOR")))

    def test_answer_to_old_orchestrator_asker_reaches_route(self):
        mesh.call_tool("decision_open", {"question": "q?", "from": "ORCHESTRATOR"})
        mesh.call_tool("decision_answer", {"id": "Q1", "answer": "ok"})
        self.assertEqual(len(self.inbox_lines("ROUTE")), 1)
        self.assertFalse(os.path.exists(self.path("ORCHESTRATOR")))

    def test_other_names_are_untouched(self):
        self.assertEqual(mesh.live_name("cx"), "cx")
        self.assertEqual(mesh.live_name("DECIDE"), "DECIDE")
        self.assertEqual(mesh.live_name("unknown-lane"), "unknown-lane")


class TestRetiredNameStillInRoster(MeshBase):
    """Control: a live roster lane that still has the old name keeps its own inbox (MeshBase's roster has both)."""

    def test_roster_lane_named_decisions_keeps_its_inbox(self):
        mesh.call_tool("decision_open", {"question": "q?", "from": "cx"})
        self.assertEqual(len(self.inbox_lines("DECISIONS")), 1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "DECIDE.jsonl")))
        self.assertEqual(mesh.live_name("ORCHESTRATOR"), "ORCHESTRATOR")

    def test_no_successor_desk_keeps_the_old_name(self):
        self.assertEqual(mesh.live_name("DECISIONS", {"cx": {}}), "DECISIONS", "no DECIDE lane: queue under the given name")
        self.assertEqual(mesh.live_name("ORCHESTRATOR", {}), "ORCHESTRATOR")
        self.assertEqual(mesh.live_name("ORCHESTRATOR", {"ROUTE": {}}), "ROUTE")


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
            self.assertEqual(len(tools), 11)
            self.assertEqual(sorted(t["name"] for t in tools),
                             ["decision_answer", "decision_find", "decision_list", "decision_open",
                              "decision_report", "peer_inbox", "peer_list", "peer_send", "side_end", "side_list",
                              "side_start"])
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
