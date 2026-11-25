"""A typed wake must never splice into text a human is typing (Q158).

Real tmux on a private, throwaway socket (`tmux -L fleetwake-<pid>-<n>`), never the live fleet's server. The fixture
pane is a tiny line-reading prompt that draws `❯ ` like Claude Code's input row and logs every submitted line, so
a splice shows up as a submitted `good morni[fleet] message from ...` line.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "lib", "fleet_mesh.py")

spec = importlib.util.spec_from_file_location("fleet_mesh", MESH)
mesh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mesh)

# a line-reading prompt: rule, `❯ `, the tty echoes what is typed; Enter appends the line to $1
FIXTURE = r'''
log=$1
while :; do
  printf '%s\n' "────────────────────────────────────────"
  IFS= read -r -p "❯ " line || exit 0
  printf 'SUBMIT:%s\n' "$line" >> "$log"
done
'''

_N = [0]


def _wait(pred, timeout=8.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class WakeSpliceTest(unittest.TestCase):
    def setUp(self):
        _N[0] += 1
        self.sock = "fleetwake-%d-%d" % (os.getpid(), _N[0])
        self.tmp = tempfile.mkdtemp(prefix="wakesplice-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.log = os.path.join(self.tmp, "submitted.log")
        open(self.log, "w").close()
        script = os.path.join(self.tmp, "prompt.sh")
        with open(script, "w") as f:
            f.write(FIXTURE)
        self.env = {"FLEET_STATE": self.state, "FLEET_TMUX_SOCKET": self.sock, "FLEET_NAME": "alpha",
                    "FLEET_DECIDE": "DECISIONS", "FLEET_ROUTE": "ORCHESTRATOR",
                    "FLEET_CLAUDE_JSON": os.path.join(self.tmp, "claude.json"),
                    # the deferred waker: short bound and backoff so the tests run in seconds
                    "FLEET_WAKE_DEFER_MAX": "6", "FLEET_WAKE_BACKOFF": "0.3", "FLEET_WAKE_QUIET": "0",
                    "FLEET_NATIVE_GRACE": "120", "CLAUDECODE": "", "FLEET_WAKE_SPAWN": "1"}
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "w", "-x", "120",
                        "-y", "30", "env LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash --norc --noprofile %s %s"
                        % (script, self.log)], check=True)
        self.pane = self.tm("display-message", "-p", "-t", "w", "#{pane_id}").strip()
        roster = {"cl": {"name": "cl", "cli": "claude", "mode": "prompt", "pane": self.pane,
                         "session_id": "u-1", "port": None, "status": "running"}}
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(roster, f)
        self.assertTrue(_wait(lambda: self.cursor_line().startswith("❯")), "control: fixture prompt never drew")

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.sock, "kill-server"], stderr=subprocess.DEVNULL)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # a deferred waker of this test may still be sleeping: it exits once its bound passes; the state is ours
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tm(self, *args):
        return subprocess.run(["tmux", "-L", self.sock] + list(args), stdout=subprocess.PIPE,
                              universal_newlines=True, check=True).stdout

    def cursor_line(self):
        cy = self.tm("display-message", "-p", "-t", self.pane, "#{cursor_y}").strip()
        return self.tm("capture-pane", "-p", "-t", self.pane, "-S", cy, "-E", cy).rstrip("\n").rstrip()

    def submitted(self):
        with open(self.log) as f:
            return [ln[len("SUBMIT:"):].rstrip("\n") for ln in f if ln.startswith("SUBMIT:")]

    def note(self):
        return mesh.wake_note("alpha", "cl", {"cli": "claude"})

    def test_wake_never_splices_into_typed_input(self):
        self.tm("send-keys", "-t", self.pane, "-l", "good morni")  # the human, mid-word
        self.assertTrue(_wait(lambda: self.cursor_line() == "❯ good morni"), "control: human text never showed")
        res = mesh.deliver("alpha", "cl", "the body")
        time.sleep(1.0)
        spliced = [s for s in self.submitted() if "[fleet]" in s]
        print("RESULT typed-input: deliver=%r submitted=%r cursor_line=%r" % (res, self.submitted(),
                                                                             self.cursor_line()))
        self.assertEqual(spliced, [], "the wake was spliced into the human's text and submitted")
        self.assertEqual(self.cursor_line(), "❯ good morni", "the human's input was changed")
        self.assertEqual(json.loads(open(os.path.join(self.state, "inbox", "cl.jsonl")).readline())["text"],
                         "the body", "the inbox write must happen even when the wake is deferred")

    def test_empty_input_gets_the_wake_at_once(self):
        res = mesh.deliver("alpha", "cl", "the body")
        got = _wait(lambda: self.submitted(), timeout=3)
        print("RESULT empty-input: deliver=%r submitted=%r" % (res, got))
        self.assertEqual(got, [self.note()])
        self.assertIn("ok", res)

    def test_deferred_wake_lands_once_the_human_clears_the_input(self):
        self.tm("send-keys", "-t", self.pane, "-l", "good morni")
        self.assertTrue(_wait(lambda: self.cursor_line() == "❯ good morni"), "control: human text never showed")
        res = mesh.deliver("alpha", "cl", "the body")
        self.assertIn("deferred", res)
        time.sleep(1.0)
        self.assertEqual(self.submitted(), [], "control: nothing may be submitted while the human is typing")
        self.tm("send-keys", "-t", self.pane, "C-u")  # the human clears the box (or sends their own line)
        got = _wait(lambda: self.submitted(), timeout=8)
        print("RESULT deferred-then-clear: submitted=%r" % (got,))
        self.assertEqual(got, [self.note()])

    def test_past_the_bound_a_notice_is_left_and_nothing_is_typed(self):
        os.environ["FLEET_WAKE_DEFER_MAX"] = "1"
        self.tm("send-keys", "-t", self.pane, "-l", "good morni")
        self.assertTrue(_wait(lambda: self.cursor_line() == "❯ good morni"), "control: human text never showed")
        mesh.deliver("alpha", "cl", "the body")
        badge = _wait(lambda: self.opt("@fleet_unread"), timeout=10)
        print("RESULT past-bound: @fleet_unread=%r cursor_line=%r submitted=%r" % (badge, self.cursor_line(),
                                                                                  self.submitted()))
        self.assertEqual(badge, "1 from alpha")
        self.assertEqual(self.submitted(), [])
        self.assertEqual(self.cursor_line(), "❯ good morni")
        mesh.t_peer_inbox({"name": "cl"})  # reading the inbox clears the badge
        self.assertEqual(self.opt("@fleet_unread"), "")

    def test_parallel_senders_never_merge_or_jam_the_box(self):
        # two senders racing one empty pane: the per-pane lock serializes check, type, verify and Enter
        script = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('m',%r);m=importlib.util."
                  "module_from_spec(s);s.loader.exec_module(m);print(m.deliver(sys.argv[1],'cl','body'))" % MESH)
        procs = [subprocess.Popen(["python3", "-c", script, frm], env=dict(os.environ, FLEET_WAKE_SPAWN="0"),
                                  stdout=subprocess.PIPE, universal_newlines=True) for frm in ("alpha", "beta")]
        outs = [p.communicate(timeout=30)[0].strip() for p in procs]
        got = _wait(lambda: len(self.submitted()) >= 1 and self.submitted(), timeout=5)
        time.sleep(1)
        print("RESULT parallel: outs=%r submitted=%r cursor_line=%r" % (outs, self.submitted(), self.cursor_line()))
        notes = {mesh.wake_note(f, "cl", {"cli": "claude"}) for f in ("alpha", "beta")}
        self.assertTrue(got)
        self.assertTrue(set(self.submitted()) <= notes, "a merged or partial pointer was submitted")
        self.assertEqual(self.cursor_line(), "❯", "the box was left jammed with an unsent pointer")

    def test_a_pointer_left_unsent_gets_its_enter(self):
        self.tm("send-keys", "-t", self.pane, "-l", self.note())  # an earlier wake whose Enter never went
        self.assertTrue(_wait(lambda: "[fleet]" in self.tm("capture-pane", "-p", "-t", self.pane)),
                        "control: leftover pointer never showed")
        self.assertEqual(mesh.pane_busy(self.pane)[0], True, "control: a box holding text reads busy")
        ok, why = mesh.type_wake(self.pane, self.note())
        got = _wait(lambda: self.submitted(), timeout=3)
        print("RESULT leftover: ok=%r why=%r submitted=%r" % (ok, why, got))
        self.assertTrue(ok)
        self.assertEqual(got, [self.note()])
        # control: a leftover pointer is not cleared for a non-pointer text (fleet park's /compact): busy
        self.tm("send-keys", "-t", self.pane, "-l", self.note())
        _wait(lambda: self.tm("capture-pane", "-p", "-t", self.pane).count("[fleet]") >= 2)
        self.assertIsNone(mesh.type_wake(self.pane, "/compact")[0])

    def test_guarded_type_cli_refuses_a_typed_box(self):
        self.tm("send-keys", "-t", self.pane, "-l", "good morni")
        self.assertTrue(_wait(lambda: self.cursor_line() == "❯ good morni"), "control: human text never showed")
        p = subprocess.run(["python3", MESH, "type", self.pane, "/compact"], stdout=subprocess.PIPE,
                           universal_newlines=True)
        print("RESULT type-cli busy: rc=%d out=%r line=%r" % (p.returncode, p.stdout, self.cursor_line()))
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.cursor_line(), "❯ good morni")
        self.tm("send-keys", "-t", self.pane, "C-u")
        _wait(lambda: self.cursor_line() == "❯")
        p = subprocess.run(["python3", MESH, "type", self.pane, "/compact"], stdout=subprocess.PIPE,
                           universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)  # control: an empty box takes it
        self.assertEqual(_wait(lambda: self.submitted(), timeout=3), ["/compact"])

    def opt(self, name):
        p = subprocess.run(["tmux", "-L", self.sock, "show-options", "-p", "-v", "-t", self.pane, name],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        return p.stdout.strip()

    def claude_sender(self):
        """Make this process look like lane `alpha`'s own fleet MCP server inside Claude Code."""
        with open(os.path.join(self.state, "roster.json")) as f:
            roster = json.load(f)
        roster["alpha"] = {"name": "alpha", "cli": "claude", "mode": "prompt", "pane": "%99", "status": "running"}
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(roster, f)
        os.environ["CLAUDECODE"] = "1"
        os.environ["FLEET_NATIVE_GRACE"] = "2"

    def test_claude_to_claude_goes_native_and_types_nothing(self):
        self.claude_sender()
        # control: the `fleet_mesh.py send` / `fleet send` CLI path never goes native (no agent reads its result)
        self.assertFalse(mesh.deliver("alpha", "cl", "cli body").count("SendMessage"))
        _wait(lambda: self.submitted(), timeout=3)
        self.assertEqual(self.submitted(), [self.note()], "control: the CLI send got the plain guarded wake")
        res = mesh.call_tool("peer_send", {"to": "cl", "text": "the body"})["content"][0]["text"]
        self.assertIn("SendMessage", res)
        rec = json.loads(open(os.path.join(self.state, "inbox", "cl.jsonl")).readlines()[-1])
        self.assertEqual((rec["text"], rec.get("native")), ("the body", "pending"))
        # the sender's SendMessage fires the PostToolUse mirror hook, which links instead of duplicating
        out = mesh.mirror({"tool_name": "SendMessage", "tool_input": {"to": "cl", "message": "the body"}})
        self.assertTrue(out.startswith("linked"), out)
        self.assertEqual(len(open(os.path.join(self.state, "inbox", "cl.jsonl")).readlines()), 2)
        time.sleep(4)  # past the 2s grace: the waker sees the link and types nothing
        print("RESULT native: deliver=%r mirror=%r submitted=%r" % (res, out, self.submitted()))
        self.assertEqual(self.submitted(), [self.note()], "no second pointer: the CLI line was already woken")

    def test_decision_tools_go_native_only_over_mcp(self):
        self.claude_sender()
        with open(os.path.join(self.state, "roster.json")) as f:
            roster = json.load(f)
        roster["DECISIONS"] = dict(roster.pop("cl"), name="DECISIONS")
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(roster, f)
        res = mesh.call_tool("decision_open", {"question": "ship it?"})["content"][0]["text"]
        print("RESULT decision native: %r submitted=%r" % (res, self.submitted()))
        self.assertIn("SendMessage", res)
        # control: an engine (WATCH, probe) calling the function directly gets the guarded typed wake
        res = mesh.t_decision_open({"from": "alpha", "question": "engine q?"})
        self.assertNotIn("SendMessage", res)
        self.assertEqual(len(_wait(lambda: self.submitted(), timeout=3)), 1)

    def test_claude_to_claude_without_sendmessage_falls_back_after_the_grace(self):
        self.claude_sender()
        mesh.call_tool("peer_send", {"to": "cl", "text": "the body"})["content"][0]["text"]
        time.sleep(1)
        self.assertEqual(self.submitted(), [], "control: nothing typed inside the grace")
        got = _wait(lambda: self.submitted(), timeout=10)
        print("RESULT native-fallback: submitted=%r" % (got,))
        self.assertEqual(got, [self.note()])

    # ---- issue #34: the decision desk pane never gets typed keys, empty input or not
    def set_role(self, role, name="cl"):
        with open(os.path.join(self.state, "roster.json")) as f:
            roster = json.load(f)
        roster[name]["role"] = role
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(roster, f)

    def test_decide_pane_with_empty_input_gets_no_keys_only_a_notice(self):
        self.set_role("decide")
        res = mesh.deliver("alpha", "cl", "the body")
        time.sleep(1.0)
        print("RESULT decide-empty: deliver=%r submitted=%r cursor=%r" % (res, self.submitted(), self.cursor_line()))
        self.assertEqual(self.submitted(), [], "a wake was typed into the decision desk pane")
        self.assertEqual(self.cursor_line(), "❯", "the decision desk input box was touched")
        self.assertIn("not typed", res)
        self.assertTrue(self.opt("@fleet_unread"), "no @fleet_unread notice on the pane")
        self.assertEqual(json.loads(open(os.path.join(self.state, "inbox", "cl.jsonl")).readline())["text"],
                         "the body", "the inbox line is the record")

    def test_no_type_roles_is_configurable(self):
        self.set_role("decide")
        os.environ["FLEET_NO_TYPE_ROLES"] = ""  # control: empty set, the decide pane takes a typed wake again
        try:
            mesh.deliver("alpha", "cl", "the body")
            self.assertEqual(_wait(lambda: self.submitted(), timeout=3), [self.note()])
            self.set_role("route")
            os.environ["FLEET_NO_TYPE_ROLES"] = "decide, route"
            mesh.deliver("alpha", "cl", "second")
            time.sleep(1.0)
            self.assertEqual(self.submitted(), [self.note()], "a route pane in FLEET_NO_TYPE_ROLES got keys")
        finally:
            os.environ.pop("FLEET_NO_TYPE_ROLES", None)

    def test_decision_engine_rewake_types_nothing_into_decide(self):
        with open(os.path.join(self.state, "roster.json")) as f:
            roster = json.load(f)
        roster["DECISIONS"] = dict(roster.pop("cl"), name="DECISIONS", role="decide")
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(roster, f)
        res = mesh.t_decision_open({"from": "alpha", "question": "engine q?"})
        time.sleep(1.0)
        print("RESULT decide-engine: %r submitted=%r" % (res, self.submitted()))
        self.assertEqual(self.submitted(), [])
        self.assertEqual(self.cursor_line(), "❯")

    def test_claude_to_decide_goes_native_with_no_typed_fallback(self):
        self.claude_sender()
        self.set_role("decide")
        res = mesh.call_tool("peer_send", {"to": "cl", "text": "the body"})["content"][0]["text"]
        self.assertIn("SendMessage", res)
        self.assertIn("never gets typed keys", res)
        rec = json.loads(open(os.path.join(self.state, "inbox", "cl.jsonl")).readlines()[-1])
        self.assertEqual(rec.get("native"), "pending")
        got = _wait(lambda: self.opt("@fleet_unread"), timeout=10)  # the waker outlives the 2s grace
        print("RESULT decide-native: deliver=%r submitted=%r unread=%r" % (res, self.submitted(), got))
        self.assertTrue(got, "no tmux notice after the grace ran out")
        self.assertEqual(self.submitted(), [], "the typed fallback still fired into the decision desk pane")
        self.assertEqual(self.cursor_line(), "❯")


class InputStateTest(unittest.TestCase):
    """The pane parser on screens captured from the real CLIs (`capture-pane -p -e`, 2026-11-23)."""

    def test_recorded_screens(self):
        with open(os.path.join(ROOT, "test", "fixtures", "wake_screens.json")) as f:
            screens = json.load(f)
        want = {"agy_empty": "empty", "claude_empty": "empty", "kiro_empty": "empty"}
        for key, v in sorted(screens.items()):
            got = mesh.input_state(v["screen"], v["cx"], v["cy"])[0]
            print("LABEL %s -> %s" % (key, got))
            self.assertEqual(got, want.get(key, "busy"), key)
        for cli in ("agy", "claude", "kiro"):
            v = screens[cli + "_typed"]
            self.assertEqual(mesh.input_text(v["screen"], v["cy"]), "good morni", cli)
            v = screens[cli + "_empty"]
            self.assertEqual(mesh.input_text(v["screen"], v["cy"]), "", cli)

    def test_claude_colours_a_typed_slash_command(self):
        # captured from a live Claude Code 2.1.330 lane after `/mcp` was typed (2026-11-23): the command is colour 153
        screen = "\u276f\xa0\x1b[38;5;153m/mcp\x1b[39m\n\x1b[38;5;244m" + "\u2500" * 40 + "\n"
        self.assertEqual(mesh.input_text(screen, 0), "", "control: the solid reading drops coloured cells")
        self.assertEqual(mesh.input_text(screen, 0, every=True), "/mcp")

    def test_claude_vim_normal_mode_is_busy(self):
        # Claude Code vim mode: `-- INSERT --` in the footer while inserting, nothing in NORMAL mode
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "claude.json")
        old = {k: os.environ.get(k) for k in ("FLEET_CLAUDE_JSON", "FLEET_WAKE_QUIET")}
        os.environ.update(FLEET_CLAUDE_JSON=cfg, FLEET_WAKE_QUIET="0")
        rule = "\u2500" * 40
        normal = "\u276f \n%s\n  bypass permissions on\n" % rule
        insert = "\u276f \n%s\n  -- INSERT -- bypass permissions on\n" % rule
        orig = mesh._read_screen
        try:
            for vim, screen, want in ((True, normal, True), (True, insert, False), (False, normal, False)):
                with open(cfg, "w") as f:
                    json.dump({"editorMode": "vim"} if vim else {}, f)
                mesh._read_screen = lambda pane, s=screen: (s, 2, 0, False)
                got = mesh.pane_busy("%0")
                print("LABEL vim=%s insert=%s -> %r" % (vim, "INSERT" in screen, got))
                self.assertEqual(got[0], want, (vim, screen))
        finally:
            mesh._read_screen = orig
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_vim_normal_mode_and_bad_cursor_are_busy(self):
        self.assertEqual(mesh.input_state("❯ \n-- NORMAL --\n", 2, 0)[0], "busy")
        self.assertEqual(mesh.input_state("❯ \n", 2, 5)[0], "busy")
        self.assertEqual(mesh.input_state("❯ \n", 2, 0)[0], "empty")  # control


if __name__ == "__main__":
    unittest.main()
