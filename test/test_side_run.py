#!/usr/bin/env python3
"""Side runs: `fleet side run [...] -- CMD` launches a detached child and registers it in one step (issue #15).

Stdlib only. Throwaway FLEET_STATE, a stub tmux on PATH (the watcher's wake never reaches a real pane), and
`sh -c` stub children. Every check goes through the real entry point (bin/fleet side run ...).
The inbox assertion needs the #14 watcher (lib/fleet_side_watch.py) and is skipped until it exists.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
HAS_WATCHER = os.path.exists(os.path.join(ROOT, "lib", "fleet_side_watch.py"))


class SideRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-side-run-")
        self.state = os.path.join(self.tmp, "state")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(bindir)
        tmux = os.path.join(bindir, "tmux")
        with open(tmux, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(tmux, 0o755)
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"L": {"name": "L", "cli": "claude", "mode": "prompt", "session_id": "u-1",
                             "port": None}}, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        self.env.update(FLEET_STATE=self.state, FLEET_NAME="L", FLEET_SIDE_POLL="0.2", FLEET_WAKE_SPAWN="0",
                        FLEET_TMUX_SOCKET="side-run-test", CLAUDECODE="",
                        PATH=bindir + os.pathsep + os.environ.get("PATH", ""))

    def tearDown(self):
        for r in self._rows():
            try:
                os.killpg(int(r["pid"]), 9)
            except (OSError, TypeError, ValueError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_side(self, *cmd, extra=()):
        t0 = time.monotonic()
        r = subprocess.run(["bash", FLEET, "side", "run", "--host", "stub", "--bound", "30s"] + list(extra)
                           + ["--"] + list(cmd), capture_output=True, text=True, env=self.env, timeout=30,
                           stdin=subprocess.DEVNULL)
        return r, time.monotonic() - t0

    def _rows(self):
        p = os.path.join(self.state, "sides.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def wait_ended(self, sid, secs=5.0, tokens=False):
        end = time.monotonic() + secs
        row = None
        while time.monotonic() < end:
            row = next((r for r in self._rows() if r["id"] == sid), None)
            if row and row.get("endedAt") and (not tokens or row.get("tokensIn") is not None):
                return row
            time.sleep(0.05)
        return row

    def parse_out(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        parts = r.stdout.strip().split()
        self.assertEqual(len(parts), 4, r.stdout)
        sid, kv = parts[0], dict(p.split("=", 1) for p in parts[1:])
        self.assertRegex(sid, r"^S\d+$")
        self.assertEqual(sorted(kv), ["log", "pid", "result"])
        return sid, kv

    def log_text(self, sid):
        with open(os.path.join(self.state, "sides", sid, "log")) as f:
            return f.read()

    def test_1_done_with_tokens_and_log(self):
        r, took = self.run_side("sh", "-c", 'echo hi; echo "{\\"tokens\\":{\\"in\\":3,\\"out\\":4}}" > '
                                '"$FLEET_SIDE_RESULT"')
        self.assertLess(took, 1.0, "fleet side run took %.2fs" % took)
        sid, kv = self.parse_out(r)
        self.assertEqual(kv["log"], os.path.join(self.state, "sides", sid, "log"))
        self.assertEqual(kv["result"], os.path.join(self.state, "sides", sid, "result.json"))
        row = self.wait_ended(sid, tokens=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "done", row)
        self.assertEqual(row["exit"], 0, row)
        self.assertEqual(row["tokensIn"], 3, row)
        self.assertEqual(row["tokensOut"], 4, row)
        self.assertEqual(row["parent"], "L")
        self.assertEqual(row["host"], "stub")
        self.assertEqual(row["boundSec"], 30)
        self.assertEqual(row["log"], kv["log"])
        self.assertEqual(row["result"], kv["result"])
        self.assertEqual(row["pid"], int(kv["pid"]))
        self.assertIn("hi", self.log_text(sid))

    @unittest.skipUnless(HAS_WATCHER, "needs the #14 watcher (lib/fleet_side_watch.py)")
    def test_1b_parent_gets_one_inbox_line(self):
        r, _ = self.run_side("sh", "-c", "echo hi")
        sid, _ = self.parse_out(r)
        self.wait_ended(sid)
        inbox = os.path.join(self.state, "inbox", "L.jsonl")
        end = time.monotonic() + 5
        lines = []
        while time.monotonic() < end:
            if os.path.exists(inbox):
                with open(inbox) as f:
                    lines = [ln for ln in f if ln.strip()]
                if lines:
                    break
            time.sleep(0.1)
        time.sleep(1.0)  # a second message would land here
        with open(inbox) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0]["text"].startswith("side %s" % sid), lines[0])

    @unittest.skipUnless(HAS_WATCHER, "needs the #14 watcher (lib/fleet_side_watch.py)")
    def test_1c_stable_result_file_does_not_end_a_live_child(self):
        # issue #42: an ach-style host writes its record early, then works. With a 0.2 s poll the watcher's
        # stable-result rule fired after ~0.6 s; the run must stay running until the real child exits.
        r, _ = self.run_side("sh", "-c", 'echo "{\\"tokens\\":{\\"in\\":0,\\"out\\":0}}" > "$FLEET_SIDE_RESULT"; '
                             'sleep 3; echo "{\\"tokens\\":{\\"in\\":7,\\"out\\":8}}" > "$FLEET_SIDE_RESULT"; exit 3')
        sid, kv = self.parse_out(r)
        time.sleep(2.0)  # ~10 polls with the result file unchanged
        row = next(x for x in self._rows() if x["id"] == sid)
        self.assertEqual(row["status"], "running", row)
        self.assertIsNone(row.get("endedAt"), row)
        os.kill(int(kv["pid"]), 0)  # the child really is still alive
        row = self.wait_ended(sid, secs=8, tokens=True)
        self.assertEqual(row["status"], "failed", row)
        self.assertEqual(row["exit"], 3, row)
        self.assertEqual((row["tokensIn"], row["tokensOut"]), (7, 8), row)
        inbox = os.path.join(self.state, "inbox", "L.jsonl")
        end = time.monotonic() + 5
        while time.monotonic() < end and not os.path.exists(inbox):
            time.sleep(0.1)
        time.sleep(1.0)
        with open(inbox) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(" failed ", lines[0]["text"])
        self.assertIn("tok in", lines[0]["text"])

    def test_2_nonzero_exit_is_failed(self):
        r, _ = self.run_side("sh", "-c", "exit 3")
        sid, _ = self.parse_out(r)
        row = self.wait_ended(sid)
        self.assertEqual(row["status"], "failed", row)
        self.assertEqual(row["exit"], 3, row)

    def test_3_env_contract(self):
        r, _ = self.run_side("sh", "-c", "env")
        sid, kv = self.parse_out(r)
        self.wait_ended(sid)
        env = dict(ln.split("=", 1) for ln in self.log_text(sid).splitlines() if "=" in ln)
        self.assertEqual(env.get("FLEET_SIDE"), sid)
        self.assertEqual(env.get("FLEET_PARENT"), "L")
        self.assertEqual(env.get("FLEET_NAME"), "L")
        self.assertEqual(env.get("FLEET_SIDE_RESULT"), kv["result"])

    def test_4_no_tty_and_own_session(self):
        r, _ = self.run_side("sh", "-c", "tty")
        sid, kv = self.parse_out(r)
        self.wait_ended(sid)
        text = self.log_text(sid)
        self.assertIn("not a tty", text)
        self.assertNotIn("/dev/", text.splitlines()[0])

    def test_5_child_in_own_process_group(self):
        r, _ = self.run_side("sh", "-c", "sleep 3")
        sid, kv = self.parse_out(r)
        pid = int(kv["pid"])
        self.assertEqual(os.getpgid(pid), pid)
        self.assertEqual(os.getsid(pid), pid)
        self.assertNotEqual(os.getsid(pid), os.getsid(0))

    def test_6_explicit_parent_and_result(self):
        res = os.path.join(self.tmp, "custom.json")
        env = dict(self.env)
        env.pop("FLEET_NAME")
        r = subprocess.run(["bash", FLEET, "side", "run", "--parent", "L", "--host", "codex", "--model", "m1",
                            "--result", res, "--", "sh", "-c", 'echo "$FLEET_SIDE_RESULT"'],
                           capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL)
        sid, kv = self.parse_out(r)
        self.assertEqual(kv["result"], res)
        row = self.wait_ended(sid)
        self.assertEqual(row["model"], "m1")
        self.assertEqual(row["result"], res)
        self.assertIn(res, self.log_text(sid))

    def test_8_signals(self):
        # killed by a signal before the bound: status killed, exit 128+sig
        r, _ = self.run_side("sleep", "30")
        sid, kv = self.parse_out(r)
        os.killpg(int(kv["pid"]), 15)
        row = self.wait_ended(sid)
        self.assertEqual((row["status"], row["exit"]), ("killed", 143), row)
        # SIGTERM to the supervisor goes to the child's group
        r, _ = self.run_side("sleep", "30")
        sid, kv = self.parse_out(r)
        sup = next(x for x in self._rows() if x["id"] == sid)["supervisorPid"]
        os.kill(sup, 15)
        row = self.wait_ended(sid)
        self.assertEqual((row["status"], row["exit"]), ("killed", 143), row)

    def test_7_refusals(self):
        env = dict(self.env)
        env.pop("FLEET_NAME")
        r = subprocess.run(["bash", FLEET, "side", "run", "--host", "stub", "--", "true"], capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL no parent", r.stderr)
        r = subprocess.run(["bash", FLEET, "side", "run", "--parent", "nope", "--host", "stub", "--", "true"],
                           capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL no lane nope", r.stderr)
        r = subprocess.run(["bash", FLEET, "side", "run", "--host", "stub"], capture_output=True, text=True,
                           env=self.env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL no command", r.stderr)
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
