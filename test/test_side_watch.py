#!/usr/bin/env python3
"""Side-run watcher (issue #14): completion, the bound, and exactly one parent message. Stdlib only.

Throwaway FLEET_STATE and tmux socket; the stub parent lane P has no pane, so a wake can never type anywhere.
Every run goes through the real entry point (bin/fleet side start spawns the watcher). Stub children are sleeps.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
sys.path.insert(0, os.path.join(ROOT, "lib"))

GRACE = 1.0


class SideWatchTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fleet-side-watch-")
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"P": {"name": "P", "cli": "claude"}}, f)  # no pane: deliver writes the inbox, wakes nothing
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_") and k != "CLAUDECODE"}
        self.env.update(FLEET_STATE=self.state, FLEET_SIDE_POLL="0.2", FLEET_SIDE_GRACE=str(GRACE),
                        FLEET_TMUX_SOCKET="fleet-side-watch-test-%d" % os.getpid(), FLEET_WAKE_SPAWN="0")
        self.kids = []

    def tearDown(self):
        for c in self.kids:
            if c.poll() is None:
                c.kill()
            c.wait()
        self.wait_watchers_gone()
        shutil.rmtree(self.state, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    def fleet(self, *args):
        return subprocess.run(["bash", FLEET, "side"] + list(args), capture_output=True, text=True,
                              env=self.env, timeout=30)

    def child(self, *argv):
        c = subprocess.Popen(list(argv))
        self.kids.append(c)
        return c

    def start(self, pid, *extra):
        r = self.fleet("start", "--parent", "P", "--host", "codex", "--model", "gpt-x", "--pid", str(pid), *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def row(self, id):
        r = self.fleet("show", id)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def inbox(self):
        try:
            with open(os.path.join(self.state, "inbox", "P.jsonl")) as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except FileNotFoundError:
            return []

    def wait_for(self, pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.1)
        return pred()

    def wait_watchers_gone(self, timeout=10):
        """Every watcher holds an flock on side/<id>.watch.lock for its life; wait until all are released."""
        d = os.path.join(self.state, "side")
        end = time.time() + timeout
        while time.time() < end:
            busy = False
            for fn in (os.listdir(d) if os.path.isdir(d) else []):
                if not fn.endswith(".watch.lock"):
                    continue
                with open(os.path.join(d, fn)) as f:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        busy = True
            if not busy:
                return True
            time.sleep(0.1)
        return False

    def finished(self, id):
        r = self.row(id)
        return r if r["status"] != "running" and r.get("notifiedAt") else None

    # ------------------------------------------------------------ acceptance

    def test_1_completion_one_message(self):
        c = self.child("sleep", "1")
        sid = self.start(c.pid, "--bound", "30s")
        self.assertEqual(sid, "S1")
        row = self.wait_for(lambda: self.finished("S1"), 5)
        self.assertIsNotNone(row, "S1 not finished and notified within 5s: %s" % self.row("S1"))
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["endedAt"])
        self.assertGreaterEqual(row["children"], 1, row)
        self.assertTrue(self.wait_watchers_gone())
        lines = self.inbox()
        self.assertEqual(len(lines), 1, "inbox lines %d != 1: %s" % (len(lines), lines))
        msg = lines[0]
        self.assertEqual(msg["from"], "side:S1")
        self.assertTrue(msg["text"].startswith("side S1 codex gpt-x done in "), msg["text"])
        self.assertIn("(bound 30s)", msg["text"])
        self.assertIn("? tok in / ? out", msg["text"])

    def test_2_bound_kills_and_times_out(self):
        c = self.child("sleep", "60")
        t0 = time.time()
        sid = self.start(c.pid, "--bound", "2s")
        try:
            c.wait(timeout=2 + GRACE + 3)
        except subprocess.TimeoutExpired:
            self.fail("pid %d still alive %.1fs after a 2s bound" % (c.pid, time.time() - t0))
        self.assertLess(time.time() - t0, 2 + GRACE + 3)
        row = self.wait_for(lambda: self.finished(sid), 5)
        self.assertIsNotNone(row, "not notified: %s" % self.row(sid))
        self.assertEqual(row["status"], "timeout")
        self.assertTrue(self.wait_watchers_gone())
        lines = self.inbox()
        self.assertEqual(len(lines), 1, "inbox lines %d != 1: %s" % (len(lines), lines))
        self.assertIn("timeout", lines[0]["text"])

    def test_3_two_extra_watchers_still_one_message(self):
        c = self.child("sleep", "1")
        sid = self.start(c.pid, "--bound", "30s")
        extra = [subprocess.Popen(["bash", FLEET, "side", "watch", sid], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
        for p in extra:
            p.wait(timeout=15)
        self.assertIsNotNone(self.wait_for(lambda: self.finished(sid), 5), self.row(sid))
        self.assertTrue(self.wait_watchers_gone())
        # a watcher started after the message went out (a restart) must not send another
        late = self.fleet("watch", sid)
        self.assertEqual(late.returncode, 0, late.stderr)
        lines = self.inbox()
        self.assertEqual(len(lines), 1, "inbox lines %d != 1: %s" % (len(lines), lines))

    def test_4_ach_error_result_is_failed(self):
        res = os.path.join(self.state, "result.json")
        with open(res, "w") as f:
            json.dump({"runId": "r1", "exitStatus": "error", "tokens": [
                {"inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 0, "cacheWriteTokens": 0}]}, f)
        c = self.child("sleep", "30")  # still running: the stable result file alone ends the run
        sid = self.start(c.pid, "--bound", "30s", "--result", res)
        row = self.wait_for(lambda: self.finished(sid), 5)
        self.assertIsNotNone(row, "not finished from a stable result file: %s" % self.row(sid))
        self.assertEqual(row["status"], "failed")
        self.assertEqual((row["tokensIn"], row["tokensOut"]), (1000, 200))
        self.assertTrue(self.wait_watchers_gone())
        lines = self.inbox()
        self.assertEqual(len(lines), 1, "inbox lines %d != 1: %s" % (len(lines), lines))
        self.assertIn(" failed in ", lines[0]["text"])
        self.assertIn("1.0k tok in / 200 out", lines[0]["text"])
        self.assertIn("result %s" % res, lines[0]["text"])

    # ------------------------------------------------------------ extra

    def test_5_side_end_while_watching_sends_one_message(self):
        c = self.child("sleep", "30")
        sid = self.start(c.pid, "--bound", "30s")
        time.sleep(0.5)
        e = self.fleet("end", sid, "--status", "failed", "--exit", "3")
        self.assertEqual(e.returncode, 0, e.stderr)
        row = self.wait_for(lambda: self.finished(sid), 5)
        self.assertIsNotNone(row, self.row(sid))
        self.assertEqual((row["status"], row["exit"]), ("failed", 3))
        self.assertTrue(self.wait_watchers_gone())
        lines = self.inbox()
        self.assertEqual(len(lines), 1, "inbox lines %d != 1: %s" % (len(lines), lines))
        self.assertIn(" failed in ", lines[0]["text"])

    def test_6_unknown_id_fails_cleanly(self):
        r = self.fleet("watch", "S99")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("FAIL no side run S99", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_7_duration_format(self):
        import fleet_side_watch as W
        self.assertEqual([W.dur(s) for s in (0, 2, 59, 60, 432, 1800, 3720)],
                         ["0s", "2s", "59s", "1m", "7m12s", "30m", "1h2m"])

    def test_8_reused_id_is_not_closed_by_the_old_watcher(self):
        """A wiped store reuses S1. The old S1 watcher must not close the new S1 when its own pid dies."""
        a = self.child("sleep", "60")
        sid = self.start(a.pid)
        time.sleep(0.5)  # the watcher is polling S1 (pid a)
        b = self.child("sleep", "60")
        row = self.row(sid)
        row.update(pid=b.pid, startedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5)),
                   nonce="fresh-run")
        with open(os.path.join(self.state, "sides.jsonl"), "w") as f:
            f.write(json.dumps(row) + "\n")
        a.kill()
        a.wait()
        time.sleep(1.5)  # several polls with pid a dead
        r = self.row(sid)
        self.assertEqual(r["status"], "running", "old watcher closed the reused id: %s" % r)
        self.assertIsNone(r.get("endedAt"))
        self.assertEqual(self.inbox(), [])


if __name__ == "__main__":
    unittest.main()
