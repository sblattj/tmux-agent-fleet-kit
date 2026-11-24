"""Side runs in the roster views (issue #17): lib/fleet_side_roster.py over a temp FLEET_STATE. Stdlib only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import fleet_side_roster as sr  # noqa: E402

T0 = 1798761600  # 2027-01-01T00:00:00Z


class SideRosterTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="sideroster-")
        self._old = os.environ.get("FLEET_STATE")
        os.environ["FLEET_STATE"] = self.state
        self.roster = {"L": {"name": "L", "cli": "codex"}, "P": {"name": "P", "status": "parked"},
                       "D": {"name": "D", "status": "done"}, "A": {"name": "A", "status": "archived"}}
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FLEET_STATE", None)
        else:
            os.environ["FLEET_STATE"] = self._old
        shutil.rmtree(self.state, ignore_errors=True)

    def write(self, *rows):
        with open(os.path.join(self.state, "sides.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def row(self, id, parent="L", **kw):
        r = {"id": id, "parent": parent, "host": "codex", "model": None, "pid": 1, "boundSec": 1800,
             "startedAt": "2027-01-01T00:00:00Z", "status": "running", "endedAt": None,
             "tokensIn": 1500, "tokensOut": 500}
        r.update(kw)
        return r

    def test_line_format_and_order(self):
        self.write(self.row("S10"), self.row("S2", model="gpt-x"))
        self.assertEqual(sr.child_lines("L", self.roster, now=T0 + 420),
                         ["  -> S2 codex gpt-x 7m/30m 2.0k tok", "  -> S10 codex 7m/30m 2.0k tok"])

    def test_ended_rows_hidden(self):
        self.write(self.row("S1"), self.row("S2", status="done", endedAt="2027-01-01T00:01:00Z"),
                   self.row("S3", status="failed", endedAt="2027-01-01T00:01:00Z"),
                   self.row("S4", status="timeout", endedAt="2027-01-01T00:01:00Z"))
        self.assertEqual([ln.split()[1] for ln in sr.child_lines("L", self.roster, T0)], ["S1"])

    def test_orphan_for_each_retired_parent_status(self):
        self.write(self.row("S1"), self.row("S2", parent="P"), self.row("S3", parent="D"),
                   self.row("S4", parent="A"), self.row("S5", status="orphan"))
        by = sr.side_rows(self.roster, T0)
        self.assertFalse(by["L"][0]["orphan"])  # control: a live parent is not an orphan
        self.assertFalse(by["L"][0]["line"].endswith("ORPHAN"))
        for p in ("P", "D", "A"):
            self.assertTrue(by[p][0]["orphan"], p)
            self.assertTrue(by[p][0]["line"].endswith(" ORPHAN"), by[p][0]["line"])
        self.assertTrue(by["L"][1]["line"].endswith(" ORPHAN"))  # watcher-set status=orphan

    def test_gone_parent(self):
        self.write(self.row("S1", parent="GHOST"), self.row("S2"))
        self.assertEqual(sr.gone_lines(self.roster, T0 + 60), ["GHOST (gone)", "  -> S1 codex 1m/30m 2.0k tok ORPHAN"])

    def test_unknown_tokens_fall_back_to_result_file(self):
        res = os.path.join(self.state, "res.json")
        with open(res, "w") as f:
            json.dump({"tokens": {"in": 2000000, "out": 100000}}, f)
        self.write(self.row("S1", tokensIn=None, tokensOut=None, result=res),
                   self.row("S2", tokensIn=None, tokensOut=None))
        self.assertEqual(sr.child_lines("L", self.roster, T0),
                         ["  -> S1 codex 0m/30m 2.1M tok", "  -> S2 codex 0m/30m ? tok"])

    def test_cli_roster_tsv_and_gone(self):
        self.write(self.row("S1"), self.row("S2", parent="GHOST"))
        env = dict(os.environ, FLEET_STATE=self.state)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "lib", "fleet_side.py"), "roster"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^L\t  -> S1 codex \d+m/30m 2\.0k tok\n$")
        g = subprocess.run([sys.executable, os.path.join(ROOT, "lib", "fleet_side.py"), "roster", "--gone"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(g.stdout.split("\n")[0], "GHOST (gone)")
        self.assertIn("S2", g.stdout)

    def test_no_store_no_lines(self):
        self.assertEqual(sr.side_rows(self.roster, T0), {})


if __name__ == "__main__":
    unittest.main()
