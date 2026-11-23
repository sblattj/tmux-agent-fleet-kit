"""Side runs never call the decisions desk; their decisions go through the parent lane (issue #12).

A side run's env carries FLEET_SIDE=<id> and FLEET_PARENT=<parent lane>. decision_open, decision_answer and
`fleet decisions ask` refuse; peer_send works only to the parent, with from = "<parent>/<side id>"."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mesh as tm  # noqa: E402

ROOT, mesh = tm.ROOT, tm.mesh

DEC = os.path.join(ROOT, "lib", "fleet_decisions.py")
spec = importlib.util.spec_from_file_location("fleet_decisions_side", DEC)
decisions_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decisions_mod)


class SideGuard(tm.MeshBase):
    def setUp(self):
        super().setUp()
        self.roster["L"] = {"name": "L", "cli": "codex", "mode": "prompt", "pane": "%6", "session_id": "thr-L",
                            "port": None}
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)
        self.dpath = os.path.join(self.state, "decisions.jsonl")
        # one existing call so "unchanged" compares real bytes, not a missing file
        with open(self.dpath, "w") as f:
            f.write(json.dumps({"id": "Q1", "at": "t", "from": "alpha", "question": "old", "options": [],
                                "status": "open", "answer": None, "answeredAt": None}) + "\n")
        for k in ("FLEET_SIDE", "FLEET_PARENT"):
            self._saved.setdefault(k, os.environ.get(k))

    def tearDown(self):
        for k in ("FLEET_SIDE", "FLEET_PARENT"):
            os.environ.pop(k, None)
        super().tearDown()

    def side(self, sid="S1", parent="L"):
        os.environ["FLEET_SIDE"] = sid
        os.environ["FLEET_PARENT"] = parent

    def read(self, p):
        with open(p) as f:
            return f.read()

    def text(self, r):
        return r["content"][0]["text"]

    def test_decision_open_refused_from_side_run(self):
        self.side()
        before = self.read(self.dpath)
        r = mesh.call_tool("decision_open", {"question": "q"})
        print("verdict decision_open side run: isError=%s text=%r" % (r["isError"], self.text(r)))
        self.assertTrue(r["isError"], self.text(r))
        self.assertIn("side run S1 can't open decisions; send it to your parent lane L with peer_send", self.text(r))
        self.assertEqual(self.read(self.dpath), before)

    def test_decision_answer_refused_from_side_run(self):
        self.side()
        before = self.read(self.dpath)
        r = mesh.call_tool("decision_answer", {"id": "Q1", "answer": "yes"})
        self.assertTrue(r["isError"], self.text(r))
        self.assertIn("side run S1 can't answer decisions; send it to your parent lane L", self.text(r))
        self.assertEqual(self.read(self.dpath), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "decisions-log.jsonl")))

    def test_peer_send_to_parent_tagged_and_others_refused(self):
        self.side()
        r = mesh.call_tool("peer_send", {"to": "L", "text": "need a call on X", "from": "spoof"})
        self.assertFalse(r["isError"], self.text(r))
        recs = [json.loads(ln) for ln in self.read(os.path.join(self.state, "inbox", "L.jsonl")).splitlines()]
        print("verdict peer_send to parent: from=%r" % recs[-1]["from"])
        self.assertEqual(recs[-1]["from"], "L/S1")
        r2 = mesh.call_tool("peer_send", {"to": "ROUTE", "text": "hi"})
        print("verdict peer_send to ROUTE: isError=%s text=%r" % (r2["isError"], self.text(r2)))
        self.assertTrue(r2["isError"])
        self.assertIn("only to its parent lane L", self.text(r2))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "ROUTE.jsonl")))

    def test_side_without_parent_refused(self):
        os.environ["FLEET_SIDE"] = "S1"
        os.environ.pop("FLEET_PARENT", None)
        r = mesh.call_tool("peer_send", {"to": "L", "text": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("FLEET_PARENT", self.text(r))
        r = mesh.call_tool("decision_open", {"question": "q"})
        self.assertTrue(r["isError"])

    def test_cli_ask_refused_from_side_run(self):
        self.side()
        before = self.read(self.dpath)
        env = dict(os.environ)
        p = subprocess.run(["bash", os.path.join(ROOT, "bin", "fleet"), "decisions", "ask", "q"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        print("verdict fleet decisions ask side run: rc=%d err=%r" % (p.returncode, p.stderr.strip()))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("send it to your parent lane L", p.stderr + p.stdout)
        self.assertEqual(self.read(self.dpath), before)
        with self.assertRaises(ValueError):
            decisions_mod.ask("q2", [], None, False, 1, True, out=io.StringIO())
        self.assertEqual(self.read(self.dpath), before)

    def test_no_side_env_behaves_as_before(self):
        r = mesh.call_tool("decision_open", {"question": "q"})
        self.assertFalse(r["isError"], self.text(r))
        self.assertTrue(self.text(r).startswith("opened Q2"))
        r = mesh.call_tool("peer_send", {"to": "cx", "text": "hi"})
        self.assertFalse(r["isError"], self.text(r))
        recs = [json.loads(ln) for ln in self.read(os.path.join(self.state, "inbox", "cx.jsonl")).splitlines()]
        self.assertEqual(recs[-1]["from"], "alpha")


# MeshBase carries a few test_ methods of its own; they run in test_mesh.py, not here
for _n in [n for n in dir(tm.MeshBase) if n.startswith("test_")]:
    setattr(SideGuard, _n, None)


if __name__ == "__main__":
    unittest.main()
