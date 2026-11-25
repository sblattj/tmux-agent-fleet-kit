"""Tests for lib/fleet_learn_explain.py (phase 5 explain back and apply). Stdlib unittest, temp FLEET_STATE.

Run: python3 -m unittest test.test_explain   (from the repo root)

Graders run as real subprocesses (test/fixtures/explain/stub_grader.py, `sleep` for the timeout).
"""
import importlib.util
import io
import json
import os

# Card and quiz checks from before the lesson-first gate: cards are due at creation here.
os.environ["FLEET_LEARN_TEACH_FIRST"] = "0"
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
STUB = os.path.join(ROOT, "test", "fixtures", "explain", "stub_grader.py")

spec = importlib.util.spec_from_file_location("fleet_learn_explain", os.path.join(LIB, "fleet_learn_explain.py"))
X = importlib.util.module_from_spec(spec)
spec.loader.exec_module(X)

QUOTE = "a: cap at 3, jittered; a fourth try never succeeded in the logs"
EXPLAIN = {"cid": "C1", "lid": "L1", "format": "explain",
           "prompt": "Explain the ACME upload retry rule (PROJ-1).",
           "answer": "Upload retries stop after 3 tries, jittered.",
           "grounding": {"quote": QUOTE, "source": "Q12"}, "sched": {}}
APPLY = {"cid": "C2", "lid": "L1", "format": "apply",
         "prompt": "A lane wants a 5th upload retry. What do you say?",
         "answer": "The cap keeps the queue short.",
         "grounding": {"quote": QUOTE, "source": "Q12"}, "sched": {}}
GOOD_ANSWER = "Uploads retry at most 3 times with jitter, because a fourth try never worked."
FAKE_TOKEN = "sk-test-FAKE0123456789abcdefFAKE0123"


class FakeLearn:
    """Records review() calls with the contract's signature (lib/fleet_learn_contract.md, Python API)."""

    def __init__(self):
        self.calls = []

    def review(self, cid, *, answer, rating=None, now=None, grade=None, gradedBy=None, detail=None,
               latencyMs=None):
        row = {"cid": cid, "answer": answer, "rating": rating, "grade": grade, "gradedBy": gradedBy,
               "detail": detail, "at": now}
        self.calls.append(row)
        return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="explaintest-")
        self.old = os.environ.get("FLEET_STATE")
        os.environ["FLEET_STATE"] = os.path.join(self.tmp, "state")
        os.makedirs(os.environ["FLEET_STATE"])
        self.capture = os.path.join(self.tmp, "request.json")
        os.environ["GRADER_CAPTURE"] = self.capture
        self.asked = []

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("GRADER_CAPTURE", None)
        if self.old is None:
            os.environ.pop("FLEET_STATE", None)
        else:
            os.environ["FLEET_STATE"] = self.old

    def cfg(self, lines, exit_code=0, timeout=10):
        p = os.path.join(self.tmp, "lines.txt")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        run = "%s %s %s %d" % (shlex.quote(sys.executable), shlex.quote(STUB), shlex.quote(p), exit_code)
        return {"grader": {"kind": "command", "run": run, "timeoutSec": timeout, "maxTokens": 300}}

    def ask(self, rating):
        def f(card, reason):
            self.asked.append(reason)
            return rating
        return f


class Grader(Base):
    COVERED = "COVERED: cap is three tries | quote: cap at 3"

    def test_unquoted_wrong_line_is_dropped(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg([self.COVERED, "WRONG: says five retries"]), ask=self.ask("again"))
        self.assertEqual((r["grade"], r["gradedBy"], r["rating"]), ("pass", "command", "good"))
        self.assertEqual(r["detail"]["wrong"], [])
        self.assertEqual([(d["kind"], d["reason"]) for d in r["detail"]["dropped"]], [("WRONG", "no quote")])
        self.assertEqual(self.asked, [], "a working grader must not fall back to self-grading")

    def test_control_quoted_wrong_line_is_kept_and_fails(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER,
                    self.cfg([self.COVERED, "WRONG: says five retries | quote: cap at 3"]), ask=self.ask("good"))
        self.assertEqual((r["grade"], r["rating"]), ("fail", "again"))
        self.assertEqual([w["text"] for w in r["detail"]["wrong"]], ["says five retries"])
        self.assertEqual(r["detail"]["dropped"], [])

    def test_wrong_line_quoting_text_not_in_source_is_dropped(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER,
                    self.cfg([self.COVERED, "WRONG: no jitter | quote: retries are unlimited"]), ask=self.ask("good"))
        self.assertEqual(r["grade"], "pass")
        self.assertEqual([d["reason"] for d in r["detail"]["dropped"]], ["quote not in source"])

    def test_quote_match_ignores_case_and_spacing(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg(["COVERED: cap | quote: \"CAP  at 3\""]), ask=self.ask("again"))
        self.assertEqual(r["grade"], "pass")

    def test_core_missing_fails_minor_missing_passes(self):
        miss = "MISSING: the reason | quote: a fourth try never succeeded"
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg([self.COVERED, miss]), ask=self.ask("good"))
        self.assertEqual((r["grade"], r["rating"]), ("fail", "again"))
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg([self.COVERED, miss + " | minor"]), ask=self.ask("again"))
        self.assertEqual((r["grade"], r["rating"]), ("pass", "good"))
        self.assertTrue(r["detail"]["missing"][0]["minor"])

    def test_no_grounded_covered_is_not_a_pass(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER,
                    self.cfg(["MISSING: the cap | quote: cap at 3 | minor"]), ask=self.ask("good"))
        self.assertEqual(r["grade"], "fail")

    def test_request_on_stdin_carries_card_answer_quote_and_cap(self):
        X.grade(APPLY, GOOD_ANSWER, self.cfg([self.COVERED]), ask=self.ask("again"))
        with open(self.capture) as f:
            req = json.load(f)
        self.assertEqual(req["answer"], GOOD_ANSWER)
        self.assertEqual(req["quote"], QUOTE)
        self.assertEqual(req["maxTokens"], 300)
        self.assertEqual(req["format"], "apply")
        self.assertEqual(req["rubric"], APPLY["answer"])
        self.assertEqual(req["card"]["cid"], "C2")

    def test_apply_may_quote_the_rubric_explain_may_not(self):
        line = ["COVERED: queue stays short | quote: keeps the queue short"]
        self.assertEqual(X.grade(APPLY, GOOD_ANSWER, self.cfg(line), ask=self.ask("again"))["grade"], "pass")
        # control: the same line on an explain card is not grounded (rubric is not source), so self-grading runs
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg(line), ask=self.ask("hard"))
        self.assertEqual((r["gradedBy"], r["grade"]), ("self", "fail"))

    def test_secret_in_a_grader_line_is_dropped(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER,
                    self.cfg([self.COVERED, "WRONG: leaked %s | quote: cap at 3" % FAKE_TOKEN]), ask=self.ask("good"))
        self.assertEqual(r["grade"], "pass")
        self.assertNotIn(FAKE_TOKEN, json.dumps(r))

    def test_unknown_lines_are_ignored_and_counted(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg([self.COVERED, "VERDICT: great", "", "NOTE: brief answer"]),
                    ask=self.ask("again"))
        self.assertEqual(r["detail"]["ignored"], 1)
        self.assertEqual(r["detail"]["notes"], ["brief answer"])


class Fallback(Base):
    def test_no_grader_falls_back_to_self_grading(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER, {}, ask=self.ask("good"))
        self.assertEqual((r["grade"], r["gradedBy"], r["rating"]), ("pass", "self", "good"))
        self.assertEqual(self.asked, ["no grader configured"])
        r = X.grade(EXPLAIN, GOOD_ANSWER, {}, ask=self.ask("hard"))
        self.assertEqual((r["grade"], r["gradedBy"]), ("fail", "self"))

    def test_control_configured_grader_does_not_ask(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg(["COVERED: cap | quote: cap at 3"]), ask=self.ask("again"))
        self.assertEqual(r["gradedBy"], "command")
        self.assertEqual(self.asked, [])

    def test_grader_kind_other_than_command_is_no_grader(self):
        X.grade(EXPLAIN, GOOD_ANSWER, {"grader": {"kind": "model", "run": "true"}}, ask=self.ask("good"))
        self.assertEqual(self.asked, ["no grader configured"])

    def test_timeout_falls_back_and_is_bounded(self):
        cfg = {"grader": {"kind": "command", "run": "sleep 30", "timeoutSec": 0.5}}
        t0 = time.time()
        r = X.grade(EXPLAIN, GOOD_ANSWER, cfg, ask=self.ask("again"))
        self.assertLess(time.time() - t0, 5)
        self.assertEqual((r["gradedBy"], r["grade"]), ("self", "fail"))
        self.assertIn("timeout", self.asked[0])

    def test_nonzero_exit_falls_back_even_with_a_pass_line(self):
        r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg(["COVERED: cap | quote: cap at 3"], exit_code=3),
                    ask=self.ask("again"))
        self.assertEqual(r["gradedBy"], "self")
        self.assertIn("exit 3", self.asked[0])

    def test_empty_or_all_dropped_output_never_passes_silently(self):
        for lines in ([], ["WRONG: five retries", "COVERED: something | quote: not in the source"]):
            self.asked = []
            r = X.grade(EXPLAIN, GOOD_ANSWER, self.cfg(lines), ask=self.ask("again"))
            self.assertEqual((r["gradedBy"], r["grade"]), ("self", "fail"), lines)
            self.assertIn("no grounded item", self.asked[0])

    def test_self_grading_without_a_way_to_ask_raises(self):
        with self.assertRaises(RuntimeError):
            X.grade(EXPLAIN, GOOD_ANSWER, {})

    def test_refuses_other_formats_and_empty_answers(self):
        with self.assertRaises(ValueError):
            X.grade(dict(EXPLAIN, format="recall"), GOOD_ANSWER, {}, ask=self.ask("good"))
        with self.assertRaises(ValueError):
            X.grade(EXPLAIN, "   ", {}, ask=self.ask("good"))
        with self.assertRaises(ValueError):
            X.self_grade(EXPLAIN, "ok")

    def test_learn_json_is_read_from_fleet_state(self):
        os.makedirs(os.path.join(os.environ["FLEET_STATE"], "learn"))
        with open(os.path.join(os.environ["FLEET_STATE"], "learn", "learn.json"), "w") as f:
            json.dump(self.cfg(["COVERED: cap | quote: cap at 3"]), f)
        r = X.grade(EXPLAIN, GOOD_ANSWER, X.load_config(), ask=self.ask("again"))
        self.assertEqual(r["gradedBy"], "command")


class Submit(Base):
    NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)

    def test_submit_records_through_learn_review(self):
        learn = FakeLearn()
        X.submit(learn, EXPLAIN, GOOD_ANSWER, cfg=self.cfg(["COVERED: cap | quote: cap at 3"]), now=self.NOW)
        c = learn.calls[0]
        self.assertEqual((c["cid"], c["grade"], c["gradedBy"], c["rating"], c["at"]),
                         ("C1", "pass", "command", "good", self.NOW))
        self.assertEqual(set(c["detail"]) >= {"covered", "missing", "wrong"}, True)

    def test_terminal_quiz_self_grades_from_typed_rating(self):
        learn = FakeLearn()
        inp = io.StringIO("Retries stop at three.\nJitter spreads them.\n\nzz\ng\n")
        out = io.StringIO()
        X.quiz_card(learn, EXPLAIN, cfg={}, inp=inp, out=out, now=self.NOW)
        c = learn.calls[0]
        self.assertEqual(c["answer"], "Retries stop at three. Jitter spreads them.")
        self.assertEqual((c["grade"], c["gradedBy"], c["rating"]), ("pass", "self", "good"))
        shown = out.getvalue()
        self.assertIn(QUOTE, shown, "self-grading must show the source quote")
        self.assertIn(EXPLAIN["answer"], shown)

    def test_end_to_end_with_real_fleet_learn(self):
        """Real fleet_learn API: an explain card made by make_cards, graded by the stub command, scheduled by FSRS."""
        if not os.path.exists(os.path.join(LIB, "fleet_learn.py")):
            self.skipTest("lib/fleet_learn.py not on this branch yet")
        sys.path.insert(0, LIB)
        try:
            import fleet_learn
            import fleet_fsrs
        finally:
            sys.path.pop(0)
        learn_dir = os.path.join(os.environ["FLEET_STATE"], "learn")
        os.makedirs(learn_dir, exist_ok=True)
        cfg = self.cfg(["COVERED: cap is three | quote: cap at 3"])
        cfg.update({"scheduler": "fsrs", "cardFormats": ["explain"]})
        with open(os.path.join(learn_dir, "learn.json"), "w") as f:
            json.dump(cfg, f)
        fleet_learn.use_scheduler(fleet_fsrs.schedule)
        try:
            fleet_learn.add_item(kind="decision", origin="fleet", quote=QUOTE,
                                 source={"type": "manual", "ref": "Q12"}, title="ACME upload retry cap",
                                 what="Upload retries stop after 3 tries, jittered.", now=self.NOW)
            cards = [c for c in fleet_learn.make_cards(now=self.NOW) if c.get("format") == "explain"]
            self.assertEqual(len(cards), 1)
            row = X.submit(fleet_learn, cards[0], GOOD_ANSWER, cfg=X.load_config(), now=self.NOW)
            self.assertEqual((row["grade"], row["gradedBy"], row["rating"]), ("pass", "command", "good"))
            with open(os.path.join(learn_dir, "reviews.jsonl")) as f:
                rows = [json.loads(ln) for ln in f if ln.strip()]
            self.assertEqual([r["gradedBy"] for r in rows], ["command"])
            self.assertEqual(rows[0]["detail"]["covered"][0]["quote"], "cap at 3")
            with open(os.path.join(learn_dir, "cards.jsonl")) as f:
                sched = [json.loads(ln) for ln in f if ln.strip()][0]["sched"]
            expect = fleet_fsrs.FSRS().review({}, "good", self.NOW)
            self.assertEqual((sched["state"], sched["step"], sched["stability"], sched["due"]),
                             (expect["state"], expect["step"], expect["stability"], expect["due"]))
        finally:
            fleet_learn.use_scheduler(None)

if __name__ == "__main__":
    unittest.main()
