"""Tests for versioned call revisions (issue #60): lib/fleet_revisions.py and its hooks in fleet_mesh
(decision_revise, decision_answer rev), fleet_decisions (revise, cards, pick), desk.sh (revise, amend) and
`fleet learn revisions`.

Every test runs on a temp FLEET_STATE with stub tmux/codex/fleet binaries and a scrubbed env, so nothing reaches a
live fleet. Run: python3 -m unittest test/test_revisions.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FLEET = os.path.join(ROOT, "bin", "fleet")
DESK = os.path.join(ROOT, "skills", "running-the-decisions-desk", "desk.sh")
GOAL = os.path.join(ROOT, "bin", "fleet-goal")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rv = _load("fleet_revisions", os.path.join(LIB, "fleet_revisions.py"))

FAKE = ("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done >> \"$0.argv\"\n"
        "printf -- '--END--\\n' >> \"$0.argv\"\nexit 0\n")

Q = "Cap the widget retry loop at 3 attempts, or at 5?"
Q_REWORD = "Cap the widget retry loop at 3 attempts with jitter, or at 5 attempts?"
Q_OTHER = "Which font should the release notes page use?"


def row(**kw):
    r = {"id": "Q1", "from": "lane-a", "status": "open", "question": Q,
         "options": [{"letter": "a", "text": "cap at 3", "rec": True}, {"letter": "b", "text": "cap at 5", "rec": False}]}
    r.update(kw)
    return r


# ---------------------------------------------------------------- pure logic

class TestApplyRevision(unittest.TestCase):
    def test_rev_bumps_and_keeps_old_and_new(self):
        r = row()
        e = rv.apply_revision(r, {"question": Q_REWORD}, "lane found the jitter fix", "lane-a", source="test/x.py:12",
                              at="2026-01-02T00:00:00Z")
        self.assertEqual(r["rev"], 2)
        self.assertEqual(r["question"], Q_REWORD)
        self.assertEqual(e, {"rev": 2, "at": "2026-01-02T00:00:00Z", "by": "lane-a", "why": "lane found the jitter fix",
                             "source": "test/x.py:12", "old": {"question": Q}, "new": {"question": Q_REWORD},
                             "changed": ["question"]})
        self.assertEqual(r["revisions"], [e])

    def test_second_revision_appends(self):
        r = row()
        rv.apply_revision(r, {"question": Q_REWORD}, "one", "lane-a")
        opts = [{"letter": "a", "text": "cap at 3, jittered", "rec": True}, {"letter": "b", "text": "cap at 5", "rec": False}]
        e = rv.apply_revision(r, {"options": opts}, "two", "lane-a")
        self.assertEqual(r["rev"], 3)
        self.assertEqual([x["rev"] for x in r["revisions"]], [2, 3])
        self.assertEqual(e["changed"], ["option a"])
        self.assertEqual(rv.original(r, "question"), Q)
        self.assertEqual(rv.summary(r), "rev 3, changed: option a")

    def test_needs_a_reason(self):
        for why in (None, "", "   "):
            with self.assertRaises(rv.Refused):
                rv.apply_revision(row(), {"question": Q_REWORD}, why, "lane-a")

    def test_nothing_changed_is_refused(self):
        with self.assertRaises(rv.Refused):
            rv.apply_revision(row(), {"question": Q}, "same", "lane-a")

    def test_only_open_calls(self):
        for st in ("answered", "answered-elsewhere"):
            with self.assertRaises(rv.Refused):
                rv.apply_revision(row(status=st), {"question": Q_REWORD}, "late", "lane-a")

    def test_unknown_field_is_refused(self):
        with self.assertRaises(rv.Refused):
            rv.apply_revision(row(), {"from": "lane-z"}, "steal", "lane-a")

    def test_topic_guard_refuses_a_new_topic(self):
        r = row()
        with self.assertRaises(rv.TopicChanged):
            rv.apply_revision(r, {"question": Q_OTHER}, "repurpose", "lane-a")
        self.assertEqual(r["question"], Q)
        self.assertNotIn("revisions", r)

    def test_topic_guard_compares_with_rev_1_not_the_last_rev(self):
        r = row(question="Cap the widget retry loop at 3?")
        rv.apply_revision(r, {"question": "Cap the widget retry loop at 3, and move the release notes font?"}, "x",
                          "lane-a")
        # close to rev 2 (release notes font), far from rev 1 (widget retry): refused
        with self.assertRaises(rv.TopicChanged):
            rv.apply_revision(r, {"question": "Move the release notes font to serif?"}, "drift", "lane-a")

    def test_options_only_skips_the_topic_check(self):
        r = row()
        e = rv.apply_revision(r, {"options": ["serif", "sans"]}, "new options", "lane-a")
        self.assertEqual(e["changed"], ["option a", "option b"])

    def test_amend_refuses_an_open_call(self):
        with self.assertRaises(rv.Refused):
            rv.amend_allowed(row())
        self.assertTrue(rv.amend_allowed(row(status="answered")))


class TestViews(unittest.TestCase):
    def test_word_diff(self):
        self.assertEqual(rv.word_diff("cap at 3 attempts", "cap at 5 attempts now"),
                         [["=", "cap at"], ["-", "3"], ["+", "5"], ["=", "attempts"], ["+", "now"]])

    def test_card_view_rev_1(self):
        v = rv.card_view(row())
        self.assertEqual((v["rev"], v["revSummary"], v["revision"], v["changed"], v["diff"]), (1, "", None, [], {}))

    def test_card_view_after_revision(self):
        r = row()
        opts = [{"letter": "a", "text": "cap at 3, jittered", "rec": True}, {"letter": "b", "text": "cap at 5", "rec": False}]
        rv.apply_revision(r, {"question": Q_REWORD, "options": opts}, "jitter", "lane-a", source="cmd: make test")
        v = rv.card_view(r)
        self.assertEqual(v["rev"], 2)
        self.assertEqual(v["revSummary"], "rev 2, changed: question, option a")
        self.assertEqual(v["changed"], ["question", "option a"])
        self.assertEqual(v["diff"]["options"], [{"letter": "a", "diff": rv.word_diff("cap at 3", "cap at 3, jittered")}])
        self.assertIn(["+", "attempts with jitter,"], v["diff"]["question"])
        self.assertEqual(v["revision"]["why"], "jitter")
        self.assertEqual(v["revision"]["source"], "cmd: make test")

    def test_stale_note(self):
        r = row(rev=2)
        self.assertEqual(rv.stale_note(1, r), "answered rev 1, now rev 2")
        self.assertEqual(rv.stale_note(2, r), "")
        self.assertEqual(rv.stale_note(None, r), "")
        self.assertEqual(rv.answered_rev(None, r), 2)
        self.assertEqual(rv.answered_rev("1", r), 1)


class TestCounts(unittest.TestCase):
    def rows(self):
        def revs(asker, days):
            return [{"rev": i + 2, "at": d + "T01:00:00Z", "by": "DECIDE"} for i, d in enumerate(days)]
        return [
            {"id": "Q1", "from": "lane-a", "revisions": revs("lane-a", ["2026-01-01", "2026-01-02"])},
            {"id": "Q2", "from": "lane-a", "revisions": revs("lane-a", ["2026-01-03"] * 4)},
            {"id": "Q3", "from": "lane-a", "revisions": revs("lane-a", ["2026-01-04"])},
            {"id": "Q4", "from": "lane-b", "revisions": revs("lane-b", ["2026-01-02"])},
            {"id": "Q4", "from": "lane-b", "revisions": revs("lane-b", ["2026-01-02"] * 9)},  # desk twin: counted once
            {"id": "Q5", "from": "lane-c"},
        ]

    def test_count_by_asker_not_reviser(self):
        c = rv.count_by_lane_day(self.rows())
        self.assertEqual(c, {("lane-a", "2026-01-01"): 1, ("lane-a", "2026-01-02"): 1, ("lane-a", "2026-01-03"): 4,
                             ("lane-a", "2026-01-04"): 1, ("lane-b", "2026-01-02"): 1})
        self.assertEqual(rv.count_by_lane_day(self.rows(), since="2026-01-03"),
                         {("lane-a", "2026-01-03"): 4, ("lane-a", "2026-01-04"): 1})

    def test_signal_flags_above_usual(self):
        sig = {s["lane"]: s for s in rv.lane_signal(rv.count_by_lane_day(self.rows()))}
        self.assertEqual(sig["lane-a"]["total"], 7)
        self.assertEqual(sig["lane-a"]["usual"], 1)
        self.assertEqual(sig["lane-a"]["above"], [{"day": "2026-01-03", "n": 4}])
        self.assertEqual(sig["lane-b"]["above"], [])
        self.assertIn("ABOVE USUAL on 2026-01-03 (4)", rv.format_report({"since": None, "lanes": list(sig.values())}))


# ---------------------------------------------------------------- wired through the fleet

def scrubbed_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FLEET_") and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    env.update(extra)
    return env


class Wired(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="revtest-")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        for b in ("tmux", "codex", "fleet", "notify"):
            p = os.path.join(self.bin, b)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.goal = os.path.join(self.state, "desk.goal.json")
        self.env = {"FLEET_STATE": self.state, "FLEET_DESK_GOAL": self.goal, "FLEET_TMUX_SOCKET": "revtest-none",
                    "FLEET_CODEX_BIN": os.path.join(self.bin, "codex"), "FLEET_BIN": os.path.join(self.bin, "fleet"),
                    "FLEET_GOAL_BIN": GOAL, "FLEET_DECIDE": "DECISIONS", "FLEET_ROUTE": "ORCHESTRATOR",
                    "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self._saved = {k: os.environ.get(k) for k in list(self.env) + ["FLEET_NAME", "FLEET_ROLE", "FLEET_SIDE"]}
        os.environ.update(self.env)
        for k in ("FLEET_NAME", "FLEET_ROLE", "FLEET_SIDE"):
            os.environ.pop(k, None)
        roster = {n: {"name": n, "cli": "claude", "mode": "goal", "pane": "%%%d" % i, "session_id": "u-%d" % i}
                  for i, n in enumerate(["lane-a", "lane-b", "ORCHESTRATOR", "DECISIONS"])}
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(roster, f)
        self.mesh = _load("fleet_mesh", os.path.join(LIB, "fleet_mesh.py"))
        self.dec = _load("fleet_decisions", os.path.join(LIB, "fleet_decisions.py"))
        self.mesh.call_tool("decision_open", {"from": "lane-a", "question": Q, "options": ["(rec) cap at 3", "cap at 5"]})
        self.run_ok([GOAL, "new", self.goal, "--goal", "Run the decisions desk"])
        self.run_ok([DESK, "add", "Q1", "lane-a", Q, "lane-a"])

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, argv, env=None):
        e = scrubbed_env(self.env)
        e.update(env or {})
        return subprocess.run(argv, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=60)

    def run_ok(self, argv, **kw):
        p = self.sh(argv, **kw)
        self.assertEqual(p.returncode, 0, "%s failed: %s %s" % (argv, p.stdout, p.stderr))
        return p

    def rows(self):
        with open(os.path.join(self.state, "decisions.jsonl")) as f:
            return [json.loads(x) for x in f if x.strip()]

    def goal_item(self, cid="Q1"):
        with open(self.goal) as f:
            g = json.load(f)
        return [x for x in g["openCalls"] if x["id"] == cid][0], g

    def inbox(self, name):
        p = os.path.join(self.state, "inbox", name + ".jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x)["text"] for x in f if x.strip()]

    def log_lines(self):
        p = os.path.join(self.state, "decisions-log.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def tool(self, name, args):
        return self.mesh.call_tool(name, args)

    def tool_text(self, res):
        return json.dumps(res)

    # ---- decision_revise (MCP)
    def test_mcp_revise_both_stores_and_tells_the_desk(self):
        n0 = len(self.rows())
        res = self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD,
                                            "why": "jitter keeps 3 safe", "source": "test/x.py:3"})
        self.assertIn("revised Q1 to rev 2", self.tool_text(res))
        rows = self.rows()
        self.assertEqual(len(rows), n0, "a revision never appends a line (ids come from the rows)")
        r = rows[0]
        self.assertEqual((r["rev"], r["question"]), (2, Q_REWORD))
        self.assertEqual(r["revisions"][0]["old"], {"question": Q})
        self.assertEqual(r["revisions"][0]["by"], "lane-a")
        gi, _ = self.goal_item()
        self.assertEqual((gi["rev"], gi["question"]), (2, Q_REWORD))
        self.assertEqual(gi["revisions"], r["revisions"])
        self.assertEqual(self.sh([GOAL, "check", self.goal]).stdout.splitlines()[0], "OK")
        desk = self.inbox("DECISIONS")
        self.assertTrue(any(t.startswith("[revised Q1 rev 2 by lane-a] changed: question; why: jitter keeps 3 safe; "
                                         "source: test/x.py:3; now: " + Q_REWORD) for t in desk), desk)

    def test_mcp_revise_refuses_new_topic_and_other_lanes(self):
        res = self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_OTHER, "why": "reuse"})
        self.assertIn("new topic", self.tool_text(res))
        res = self.tool("decision_revise", {"from": "lane-b", "id": "Q1", "question": Q_REWORD, "why": "mine now"})
        self.assertIn("only its asker or the desk", self.tool_text(res))
        res = self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD})
        self.assertIn("`id` and `why` are required", self.tool_text(res))
        self.assertNotIn("revisions", self.rows()[0])
        self.assertNotIn("revisions", self.goal_item()[0])

    def test_side_run_cannot_revise(self):
        os.environ["FLEET_SIDE"] = "S1"
        os.environ["FLEET_PARENT"] = "lane-a"
        try:
            res = self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD, "why": "x"})
        finally:
            os.environ.pop("FLEET_SIDE", None)
            os.environ.pop("FLEET_PARENT", None)
        self.assertIn("can't revise", self.tool_text(res))

    # ---- decision_answer against an older rev
    def test_answer_against_older_rev_warns(self):
        self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD, "why": "jitter"})
        os.environ["FLEET_NAME"] = "DECISIONS"
        try:
            res = self.tool("decision_answer", {"id": "Q1", "answer": "a: cap at 3", "rev": 1})
        finally:
            os.environ.pop("FLEET_NAME", None)
        self.assertIn("answered rev 1, now rev 2", self.tool_text(res))
        self.assertEqual(self.rows()[0]["answeredRev"], 1)
        lg = self.log_lines()[-1]
        self.assertEqual((lg["answeredRev"], lg["warning"]), (1, "answered rev 1, now rev 2"))
        self.assertIn("[answer Q1] (answered rev 1, now rev 2) a: cap at 3", self.inbox("lane-a"))

    def test_answer_without_rev_is_current(self):
        self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD, "why": "jitter"})
        self.tool("decision_answer", {"id": "Q1", "answer": "a"})
        self.assertEqual(self.rows()[0]["answeredRev"], 2)
        self.assertNotIn("warning", self.log_lines()[-1])
        self.assertNotIn("answeredRev", self.log_lines()[-1])
        self.assertIn("[answer Q1] a", self.inbox("lane-a"))

    def test_revise_after_answer_is_refused(self):
        self.tool("decision_answer", {"id": "Q1", "answer": "a"})
        res = self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD, "why": "late"})
        self.assertIn("revise only an open call", self.tool_text(res))

    # ---- desk.sh revise / amend: the same path
    def test_desk_revise_same_path_and_tells_the_asker(self):
        p = self.run_ok([DESK, "revise", "Q1", Q_REWORD, "the lane's test shows jitter"])
        self.assertEqual(p.stdout.strip(), "OK")
        r = self.rows()[0]
        gi, g = self.goal_item()
        self.assertEqual((r["rev"], gi["rev"]), (2, 2))
        self.assertEqual(r["revisions"][0]["why"], "the lane's test shows jitter")
        self.assertEqual(gi["revisions"], r["revisions"])
        self.assertTrue(any(c["change"] == "Revised Q1" and c["why"].startswith("rev 2: ") for c in g["changelog"]))
        self.assertTrue(any(t.startswith("[revised Q1 rev 2 by DECISIONS]") for t in self.inbox("lane-a")))

    def test_desk_revise_refuses_new_topic(self):
        p = self.sh([DESK, "revise", "Q1", Q_OTHER, "repurpose"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("new topic", p.stderr)
        self.assertEqual(self.goal_item()[0]["question"], Q)
        self.assertNotIn("rev", self.rows()[0])

    def test_desk_revise_goal_only_item(self):
        self.run_ok([DESK, "add", "Q90", "lane-b", "Pin the docs build to the old theme?"])
        if any(r["id"] == "Q90" for r in self.rows()):
            self.skipTest("desk.sh add writes a mesh row here (#58): covered by the both-stores tests")
        self.run_ok([DESK, "revise", "Q90", "Pin the docs build to the old theme until the fix?", "fix due"])
        gi, _ = self.goal_item("Q90")
        self.assertEqual(gi["rev"], 2)
        self.assertEqual(gi["revisions"][0]["old"], {"question": "Pin the docs build to the old theme?"})

    def test_desk_amend_refuses_open_call(self):
        p = self.sh([DESK, "amend", "Q1", "and tag it"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("is open", p.stderr)
        gi, _ = self.goal_item()
        self.assertFalse(gi.get("answer"))
        self.run_ok([DESK, "answer", "Q1", "a", "lane-a"])
        self.run_ok([DESK, "amend", "Q1", "and tag it"])
        self.assertIn("amended: and tag it", self.goal_item()[0]["answer"])

    # ---- UI cards and pick
    def test_card_shows_rev_and_diff(self):
        self.dec.revise("Q1", question=Q_REWORD, why="jitter", by="lane-a")
        cards, _ = self.dec.open_cards()
        c = [x for x in cards if x["id"] == "Q1"][0]
        self.assertEqual(c["rev"], 2)
        self.assertEqual(c["revSummary"], "rev 2, changed: question")
        self.assertIn(["+", "attempts with jitter,"], c["diff"]["question"])
        lines = self.dec.Tui.render_card(None, c, False) if hasattr(self.dec, "Tui") else None
        if lines is not None:
            self.assertIn("    rev 2, changed: question (why: jitter)", lines)

    def test_pick_against_older_rev_warns(self):
        self.dec.revise("Q1", question=Q_REWORD, why="jitter", by="lane-a")
        res = self.dec.pick("Q1", option="a", rev=1)
        self.assertIn("answered rev 1, now rev 2", res["notes"])
        self.assertTrue(any(t.startswith("[answer Q1] (answered rev 1, now rev 2) a: ") for t in self.inbox("lane-a")))
        self.assertEqual(self.rows()[0]["answeredRev"], 1)
        self.assertEqual(self.log_lines()[-1]["warning"], "answered rev 1, now rev 2")

    def test_pick_current_rev_has_no_warning(self):
        self.dec.revise("Q1", question=Q_REWORD, why="jitter", by="lane-a")
        res = self.dec.pick("Q1", option="a", rev=2)
        self.assertNotIn("answered rev 1, now rev 2", res["notes"])
        self.assertEqual(self.rows()[0]["answeredRev"], 2)

    def test_cli_revise(self):
        p = self.run_ok([FLEET, "decisions", "revise", "Q1", "--options-json", json.dumps(["(rec) cap at 3, jittered",
                                                                                           "cap at 5"]),
                         "--why", "jitter", "--by", "lane-a"])
        out = json.loads(p.stdout)
        self.assertEqual((out["rev"], out["changed"]), (2, ["option a"]))
        p = self.sh([FLEET, "decisions", "revise", "Q1", "--question", Q_REWORD])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("reason", p.stdout + p.stderr)

    # ---- LEARN
    def test_learn_counts_revisions_per_lane(self):
        self.tool("decision_revise", {"from": "lane-a", "id": "Q1", "question": Q_REWORD, "why": "one"})
        self.run_ok([DESK, "revise", "Q1", Q_REWORD + " Today?", "two"])
        p = self.run_ok([FLEET, "learn", "revisions", "--json"])
        rep = json.loads(p.stdout)
        lanes = {x["lane"]: x for x in rep["lanes"]}
        self.assertEqual(list(lanes), ["lane-a"], "counted by the asker, not the reviser, and once across stores")
        self.assertEqual(lanes["lane-a"]["total"], 2)
        p = self.run_ok([FLEET, "learn", "revisions"])
        self.assertIn("lane-a: 2 total", p.stdout)


if __name__ == "__main__":
    unittest.main()
