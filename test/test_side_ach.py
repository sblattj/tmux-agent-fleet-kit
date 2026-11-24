#!/usr/bin/env python3
"""Side runs: `fleet side adopt-ach` registers running ach runs by process ancestry (issue #19).

Throwaway FLEET_STATE and AGENTIC_CODING_HARNESS_STATE_DIR; stub processes are `sh -c` and `sleep`. Every check
goes through the real entry point (bin/fleet side ...). The watcher checks (#14) run only once
lib/fleet_side_watch.py exists.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
LC = os.path.join(ROOT, "bin", "fleet-lifecycle")
FIX = os.path.join(ROOT, "test", "fixtures", "side", "ach-run-running.json")
HAS_WATCHER = os.path.exists(os.path.join(ROOT, "lib", "fleet_side_watch.py"))
sys.path.insert(0, os.path.join(ROOT, "lib"))


class AdoptAchTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fleet-side-ach-")
        self.ach = tempfile.mkdtemp(prefix="ach-state-")
        os.makedirs(os.path.join(self.ach, "runs"))
        # stub lane process L; the "ach" process is a sleep it started
        self.lane = subprocess.Popen(["sh", "-c", "sleep 120 & echo $!; wait"], stdout=subprocess.PIPE, text=True)
        self.kid = int(self.lane.stdout.readline().strip())
        self.orphan = subprocess.Popen(["sleep", "120"])  # child of the test runner, no lane above it
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"L": {"name": "L", "cli": "claude", "status": "live", "pid": self.lane.pid}}, f)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("FLEET_") and k != "AGENTIC_CODING_HARNESS_STATE_DIR"}
        self.env.update(FLEET_STATE=self.state, AGENTIC_CODING_HARNESS_STATE_DIR=self.ach,
                        FLEET_TMUX_SOCKET="fleet-side-ach-%d" % os.getpid(), FLEET_SIDE_POLL="0.2",
                        FLEET_WAKE_SPAWN="0", FLEET_SIDE_WATCH_SPAWN="0")  # test_3 turns the watcher on

    def tearDown(self):
        for p in (self.orphan,):
            p.kill()
            p.wait()
        try:
            os.kill(self.kid, 9)
        except ProcessLookupError:
            pass
        self.lane.kill()
        self.lane.wait()
        time.sleep(0.3)  # let a spawned watcher see its pid gone before the state dir goes
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.ach, ignore_errors=True)

    def record(self, pid, run_id=None, **over):
        with open(FIX) as f:
            rec = json.load(f)
        rec.update(runId=run_id or str(uuid.uuid4()), pid=pid, startedAt=int(time.time() * 1000), **over)
        p = os.path.join(self.ach, "runs", rec["runId"] + ".json")
        with open(p, "w") as f:
            json.dump(rec, f)
        return p, rec

    def fleet(self, *args):
        return subprocess.run(["bash", FLEET, "side"] + list(args), capture_output=True, text=True,
                              env=self.env, timeout=60)

    def rows(self):
        r = self.fleet("ls", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_1_apply_registers_once_by_ancestry(self):
        path, rec = self.record(self.kid)
        dry = self.fleet("adopt-ach")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("would register", dry.stdout)
        self.assertIn(rec["runId"], dry.stdout)
        self.assertEqual(self.rows(), [], "a dry run registers nothing")

        r = self.fleet("adopt-ach", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("S1 parent=L host=codex source=ach", r.stdout)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row["id"], row["parent"], row["host"], row["source"]), ("S1", "L", "codex", "ach"))
        self.assertEqual(row["achRunId"], rec["runId"])
        self.assertEqual(row["pid"], self.kid)
        self.assertEqual(row["model"], "gpt-x")
        self.assertEqual(row["boundSec"], 1800)
        self.assertEqual(row["log"], rec["rawTranscript"])
        self.assertEqual(row["result"], os.path.abspath(path))

        again = self.fleet("adopt-ach", "--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("parent=L", again.stdout)
        self.assertEqual(len(self.rows()), 1, "a second run registers nothing")

    def test_1b_bound_env_and_terminal_records_skipped(self):
        self.env["FLEET_SIDE_ACH_BOUND"] = "5m"
        self.record(self.kid)
        self.record(self.kid, status="success")  # finished: not a candidate
        r = self.fleet("adopt-ach", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.rows()
        self.assertEqual([x["boundSec"] for x in rows], [300])

    def test_1c_lane_found_by_tmux_pane_pid(self):
        sock = self.env["FLEET_TMUX_SOCKET"]
        if shutil.which("tmux") is None:
            self.skipTest("no tmux")
        base = ["tmux", "-L", sock]
        pidf = os.path.join(self.state, "pane-kid")
        subprocess.run(base + ["-f", "/dev/null", "new-session", "-d", "-s", "t",
                               "sh -c 'sleep 120 & echo $! > %s; wait'" % pidf], check=True, env=self.env)
        try:
            pane = subprocess.run(base + ["list-panes", "-a", "-F", "#{pane_id}"], capture_output=True, text=True,
                                  env=self.env).stdout.split()[0]
            for _ in range(50):
                if os.path.exists(pidf) and open(pidf).read().strip():
                    break
                time.sleep(0.1)
            kid = int(open(pidf).read().strip())
            # a pane-only lane (no pid in the roster): found through #{pane_pid}
            with open(os.path.join(self.state, "roster.json"), "w") as f:
                json.dump({"P": {"name": "P", "cli": "codex", "status": "live", "pane": pane}}, f)
            self.record(kid)
            r = self.fleet("adopt-ach", "--apply")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("parent=P", r.stdout)
        finally:
            subprocess.run(base + ["kill-server"], capture_output=True)

    def test_2_no_lane_ancestor_skipped_with_reason(self):
        self.record(self.orphan.pid)
        r = self.fleet("adopt-ach", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skip", r.stdout)
        self.assertIn("no lane ancestor", r.stdout)
        self.assertEqual(self.rows(), [])
        # control: a record under the lane is registered by the same run
        self.record(self.kid)
        r2 = self.fleet("adopt-ach", "--apply")
        self.assertIn("parent=L", r2.stdout)
        self.assertEqual(len(self.rows()), 1)

    def test_2b_dead_pid_skipped(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.record(dead.pid)
        r = self.fleet("adopt-ach", "--apply")
        self.assertIn("not alive", r.stdout)
        self.assertEqual(self.rows(), [])

    def test_tick_dry_runs_and_adopt_applies(self):
        self.record(self.kid)
        t = subprocess.run(["bash", LC, "tick"], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(t.returncode, 0, t.stderr)
        self.assertIn("would register", t.stdout)
        self.assertEqual(self.rows(), [])
        j = subprocess.run(["bash", LC, "tick", "--json"], capture_output=True, text=True, env=self.env, timeout=60)
        json.loads(j.stdout)  # --json stays pure JSON
        a = subprocess.run(["bash", LC, "tick", "--adopt"], capture_output=True, text=True, env=self.env,
                           timeout=60)
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertIn("S1 parent=L", a.stdout)
        self.assertEqual(len(self.rows()), 1)

    def test_usage_reads_run_record_totals(self):
        import fleet_side_usage as usage
        path, _ = self.record(self.kid, status="success",
                              totals={"inputTokens": 1500, "outputTokens": 40000, "cacheReadTokens": 400,
                                      "cacheWriteTokens": 100, "costUsd": 0})
        u = usage.usage({"host": "codex", "result": path})
        self.assertEqual((u["tokensIn"], u["tokensOut"], u["source"]), (2000, 40000, "ach"))

    def test_model_from_invocation_json(self):
        import fleet_side_ach as ach
        run = tempfile.mkdtemp(prefix="ach-cwd-", dir=self.state)
        os.makedirs(os.path.join(run, "out"))
        with open(os.path.join(run, "out", "invocation.json"), "w") as f:
            json.dump({"runId": "r", "command": "codex", "args": [], "cwd": run, "startedAt": 0, "prompt": "",
                       "model": "gpt-inv"}, f)
        rec = {"runId": "r", "cwd": run, "metadata": None}
        self.assertEqual(ach.model_of(rec, "node ach run codex --output-dir out -p hi"), "gpt-inv")
        self.assertEqual(ach.model_of(rec, "ach run --output-dir=%s/out" % run), "gpt-inv")
        self.assertIsNone(ach.model_of(rec, "ach run codex -p hi"))
        self.assertEqual(ach.model_of(dict(rec, metadata={"model": "gpt-md"}), "--output-dir out"), "gpt-md")

    def test_concurrent_apply_registers_once(self):
        self.record(self.kid)
        procs = [subprocess.Popen(["bash", FLEET, "side", "adopt-ach", "--apply"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=self.env) for _ in range(6)]
        for p in procs:
            p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
        self.assertEqual(len(self.rows()), 1)

    def test_finish_status_from_record(self):
        import fleet_side_ach as ach
        path, _ = self.record(self.kid)
        row = {"source": "ach", "result": path}
        self.assertIsNone(ach.finish_status(row))
        for st, want in (("success", "done"), ("error", "failed"), ("aborted", "failed"),
                         ("unavailable", "failed")):
            self.record(self.kid, run_id=os.path.basename(path)[:-5], status=st)
            self.assertEqual(ach.finish_status(row), want, st)
        self.assertIsNone(ach.finish_status({"source": "cli", "result": path}))

    @unittest.skipUnless(HAS_WATCHER, "needs the #14 watcher (lib/fleet_side_watch.py)")
    def test_3_watcher_sends_one_message_with_totals(self):
        self.env.pop("FLEET_SIDE_WATCH_SPAWN")  # L has no pane, and the socket is throwaway
        path, rec = self.record(self.kid)
        r = self.fleet("adopt-ach", "--apply")
        self.assertIn("S1 parent=L", r.stdout)
        inbox = os.path.join(self.state, "inbox", "L.jsonl")
        time.sleep(1.5)  # ~7 polls with the record unchanged: still running
        self.assertEqual(self.rows()[0]["status"], "running")
        rec.update(status="success", exitStatus="success", endedAt=int(time.time() * 1000),
                   totals={"inputTokens": 1500, "outputTokens": 40000, "cacheReadTokens": 400,
                           "cacheWriteTokens": 100, "costUsd": 0})
        with open(path + ".tmp", "w") as f:
            json.dump(rec, f)
        os.rename(path + ".tmp", path)
        lines = []
        for _ in range(50):
            if os.path.exists(inbox):
                with open(inbox) as f:
                    lines = [json.loads(x) for x in f if x.strip()]
            if lines:
                break
            time.sleep(0.2)
        time.sleep(1)
        with open(inbox) as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(lines), 1, lines)
        text = lines[0].get("text") or lines[0].get("body") or json.dumps(lines[0])
        self.assertTrue(text.startswith("side S1"), text)
        self.assertRegex(text, r"\b2(\.0)?k\b", "in tokens (2000) in the message")
        self.assertRegex(text, r"\b40(\.0)?k\b", "out tokens (40000) in the message")
        row = self.rows()[0]
        self.assertEqual((row["status"], row["tokensIn"], row["tokensOut"]), ("done", 2000, 40000))


if __name__ == "__main__":
    unittest.main()
