#!/usr/bin/env python3
"""Side runs: store and the `fleet side start|end|ls|show` CLI (issue #11). Stdlib only, temp FLEET_STATE.

Every check goes through the real entry point (bin/fleet side ...). Stub children are `sleep` processes.
"""
import concurrent.futures
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


class SideTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fleet-side-")
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"L": {"name": "L", "cli": "claude", "pane": "%1"}}, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        self.env["FLEET_STATE"] = self.state
        self.child = subprocess.Popen(["sleep", "120"])

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        shutil.rmtree(self.state, ignore_errors=True)

    def fleet(self, *args, env=None):
        return subprocess.run(["bash", FLEET, "side"] + list(args), capture_output=True, text=True,
                              env=env or self.env, timeout=30)

    def start(self, *extra):
        return self.fleet("start", "--parent", "L", "--host", "codex", "--pid", str(self.child.pid),
                          "--bound", "5m", *extra)

    def ls_json(self, *extra):
        r = self.fleet("ls", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_1_start_prints_id_and_ls_shows_running(self):
        r = self.start("--log", "rel.log")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "S1")
        rows = self.ls_json()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "S1")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["parent"], "L")
        self.assertEqual(row["host"], "codex")
        self.assertEqual(row["pid"], self.child.pid)
        self.assertEqual(row["boundSec"], 300)
        self.assertTrue(os.path.isabs(row["log"]), row["log"])
        self.assertIsNone(row["endedAt"])
        self.assertTrue(row["startedAt"].endswith("Z"))
        for k in ("model", "result", "exit", "tokensIn", "tokensOut", "notifiedAt"):
            self.assertIn(k, row)
        self.assertEqual(row["children"], 0)
        self.assertEqual(row["source"], "cli")
        self.assertIsNone(row["sessionId"])
        # show prints the same row
        s = self.fleet("show", "S1")
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertEqual(json.loads(s.stdout)["id"], "S1")
        # plain ls has a line for S1
        p = self.fleet("ls")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("S1", p.stdout)

    def test_1b_parent_defaults_to_fleet_name(self):
        env = dict(self.env, FLEET_NAME="L")
        r = self.fleet("start", "--host", "codex", "--pid", str(self.child.pid), "--session-id", "th-1", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ls_json()[0]["parent"], "L")
        self.assertEqual(self.ls_json()[0]["sessionId"], "th-1")

    def test_2_unknown_parent_refused(self):
        r = self.fleet("start", "--parent", "NOPE", "--host", "codex", "--pid", str(self.child.pid))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no lane", r.stderr)
        self.assertEqual(self.ls_json(), [])

    def test_2_dead_pid_refused(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        r = self.fleet("start", "--parent", "L", "--host", "codex", "--pid", str(dead.pid))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.ls_json(), [])
        # control: the live pid is accepted
        self.assertEqual(self.start().returncode, 0)

    def test_2_bound_over_12h_refused(self):
        r = self.fleet("start", "--parent", "L", "--host", "codex", "--pid", str(self.child.pid), "--bound", "13h")
        self.assertNotEqual(r.returncode, 0)
        ok = self.fleet("start", "--parent", "L", "--host", "codex", "--pid", str(self.child.pid), "--bound", "12h")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_3_parallel_starts_lose_nothing(self):
        with concurrent.futures.ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda _: self.start(), range(20)))
        for r in res:
            self.assertEqual(r.returncode, 0, r.stderr)
        ids = [r.stdout.strip() for r in res]
        self.assertEqual(len(set(ids)), 20, ids)
        rows = self.ls_json()
        self.assertEqual(len(rows), 20)
        self.assertEqual(sorted(r["id"] for r in rows), sorted(ids))

    def test_4_end_is_idempotent(self):
        self.assertEqual(self.start().returncode, 0)
        r1 = self.fleet("end", "S1", "--status", "done")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        first = self.ls_json()[0]
        self.assertEqual(first["status"], "done")
        self.assertIsNotNone(first["endedAt"])
        time.sleep(1.1)
        r2 = self.fleet("end", "S1", "--status", "done")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        second = self.ls_json()[0]
        self.assertEqual(second["endedAt"], first["endedAt"])
        self.assertEqual(second["status"], "done")

    def test_ls_parent_filter_and_unknown_id(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(len(self.ls_json("--parent", "L")), 1)
        self.assertEqual(self.ls_json("--parent", "OTHER"), [])
        self.assertNotEqual(self.fleet("show", "S99").returncode, 0)
        self.assertNotEqual(self.fleet("end", "S99").returncode, 0)

    def test_python_api(self):
        old = os.environ.get("FLEET_STATE")
        os.environ["FLEET_STATE"] = self.state
        try:
            import fleet_side as side
            row = side.start(parent="L", host="codex", pid=self.child.pid, bound="1m", sessionId="abc")
            self.assertEqual(row["id"], "S1")
            self.assertEqual(row["sessionId"], "abc")
            side.update("S1", lambda r: dict(r, tokensIn=7))
            self.assertEqual(side.get("S1")["tokensIn"], 7)
            ended = side.end("S1", "failed", exit=3)
            self.assertEqual((ended["status"], ended["exit"]), ("failed", 3))
            self.assertEqual([r["id"] for r in side.rows(parent="L")], ["S1"])
            self.assertEqual(side.rows(parent="X"), [])
        finally:
            if old is None:
                os.environ.pop("FLEET_STATE", None)
            else:
                os.environ["FLEET_STATE"] = old


if __name__ == "__main__":
    unittest.main()
