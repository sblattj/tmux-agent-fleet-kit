"""Tests for lib/fleet_fsrs.py (phase 5 FSRS scheduling). Stdlib unittest, temp FLEET_STATE.

Run: python3 -m unittest test.test_fsrs   (from the repo root)

The reference values come from py-fsrs's own test suite; the fixture cites the file URL and commit.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIXTURE = os.path.join(ROOT, "test", "fixtures", "fsrs", "py-fsrs-reference.json")

spec = importlib.util.spec_from_file_location("fleet_fsrs", os.path.join(LIB, "fleet_fsrs.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

with open(FIXTURE) as fh:
    REF = json.load(fh)
CASES = {c["name"]: c for c in REF["cases"]}


def at(s):
    return F.parse_iso(s)


def intervals(fsrs, ratings, start):
    """Review each card at its previous due; interval = (due - lastReview).days, as py-fsrs measures it."""
    now, s, out = at(start), {}, []
    for r in ratings:
        s = fsrs.review(s, r, now)
        due = at(s["due"])
        out.append((due - at(s["lastReview"])).days)
        now = due
    return out


class Reference(unittest.TestCase):
    def test_fixture_cites_a_pinned_published_file(self):
        src = REF["source"]
        self.assertRegex(src["commit"], r"^[0-9a-f]{40}$")
        self.assertIn(src["commit"], src["file"])
        self.assertTrue(src["file"].startswith("https://github.com/open-spaced-repetition/py-fsrs/blob/"))
        self.assertTrue(src["file"].endswith("/tests/test_basic.py"))

    def test_intervals_match_published_reference(self):
        c = CASES["test_review_card"]
        self.assertEqual(intervals(F.FSRS(), c["ratings"], c["start"]), c["expectIntervalDays"])

    def test_hook_schedule_matches_reference(self):
        # the function fleet_learn.use_scheduler() receives, with no learn.json: same published sequence
        c = CASES["test_review_card"]
        tmp = tempfile.mkdtemp(prefix="fsrstest-")
        old = os.environ.get("FLEET_STATE")
        os.environ["FLEET_STATE"] = tmp
        try:
            now, s, got = at(c["start"]), {}, []
            for r in c["ratings"]:
                s = F.schedule(s, r, now)
                got.append((at(s["due"]) - now).days)
                now = at(s["due"])
            self.assertEqual(got, c["expectIntervalDays"])
        finally:
            shutil.rmtree(tmp)
            if old is None:
                os.environ.pop("FLEET_STATE", None)
            else:
                os.environ["FLEET_STATE"] = old

    def test_memory_state_matches_published_reference(self):
        c = CASES["test_memo_state"]
        now, s = at(c["start"]), {}
        for r, gap in zip(c["ratings"], c["gapDays"]):
            now += timedelta(days=gap)
            s = F.FSRS().review(s, r, now)
        self.assertAlmostEqual(s["stability"], c["expectStability"], delta=c["tolerance"])
        self.assertAlmostEqual(s["difficulty"], c["expectDifficulty"], delta=c["tolerance"])

    def test_repeated_easy_floors_difficulty(self):
        c = CASES["test_repeated_correct_reviews"]
        s, start = {}, at(c["start"])
        for i, r in enumerate(c["ratings"]):
            s = F.FSRS().review(s, r, start + timedelta(microseconds=i))
        self.assertEqual(s["difficulty"], c["expectDifficulty"])

    # controls: the reference check must be able to fail
    def test_control_perturbed_weight_breaks_reference(self):
        c = CASES["test_review_card"]
        w = list(F.DEFAULT_WEIGHTS)
        w[8] = 1.9
        self.assertNotEqual(intervals(F.FSRS(weights=w), c["ratings"], c["start"]), c["expectIntervalDays"])

    def test_control_other_retention_breaks_reference(self):
        c = CASES["test_review_card"]
        self.assertNotEqual(intervals(F.FSRS(desired_retention=0.8), c["ratings"], c["start"]),
                            c["expectIntervalDays"])

    def test_control_memo_state_differs_with_other_ratings(self):
        c = CASES["test_memo_state"]
        now, s = at(c["start"]), {}
        for r, gap in zip(["good"] * 6, c["gapDays"]):
            now += timedelta(days=gap)
            s = F.FSRS().review(s, r, now)
        self.assertGreater(abs(s["stability"] - c["expectStability"]), c["tolerance"])


class Behaviour(unittest.TestCase):
    T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    def test_desired_retention_shortens_or_lengthens_intervals(self):
        s90 = F.FSRS(desired_retention=0.9).review({}, "easy", self.T0)
        s95 = F.FSRS(desired_retention=0.95).review({}, "easy", self.T0)
        s80 = F.FSRS(desired_retention=0.8).review({}, "easy", self.T0)
        d = lambda s: (at(s["due"]) - self.T0).days
        self.assertLess(d(s95), d(s90))
        self.assertGreater(d(s80), d(s90))

    def test_four_ratings_order_first_stability(self):
        st = [F.FSRS().review({}, r, self.T0)["stability"] for r in ("again", "hard", "good", "easy")]
        self.assertEqual(st, sorted(st))
        self.assertEqual(len(set(st)), 4)

    def test_retrievability_is_point_nine_at_elapsed_equal_stability(self):
        f = F.FSRS()
        self.assertAlmostEqual(f.retrievability(10.0, 10), 0.9, places=9)
        self.assertLess(f.retrievability(10.0, 20), 0.9)
        self.assertEqual(F.retrievability({"due": "2026-01-02T00:00:00Z"}, self.T0), 0.0)

    def test_foreign_keys_pass_through_untouched(self):
        sched = {"due": "2026-01-01T09:00:00Z", "reps": 4, "lapses": 1, "lastReview": "2025-12-25T09:00:00Z",
                 "fixedStep": 2, "custom": {"x": 1}}
        out = F.FSRS().review(sched, "good", self.T0)
        self.assertEqual(out["fixedStep"], 2)
        self.assertEqual(out["custom"], {"x": 1})
        self.assertEqual(sched["reps"], 4, "input dict must not be mutated")

    def test_phase3_card_without_stability_seeds_as_new(self):
        # contract: no stability -> new card on its next review; reps, lapses carry over
        sched = {"due": "2026-01-01T09:00:00Z", "reps": 4, "lapses": 1, "lastReview": "2025-12-25T09:00:00Z",
                 "fixedStep": 3}
        out = F.FSRS().review(sched, "good", self.T0)
        self.assertEqual(out["stability"], F.DEFAULT_WEIGHTS[2])
        self.assertEqual((out["state"], out["step"]), ("learning", 1))
        self.assertEqual((out["reps"], out["lapses"]), (5, 1))
        # control: a card that already has FSRS state is not reseeded
        again = F.FSRS().review(out, "good", self.T0 + timedelta(days=1))
        self.assertEqual(again["state"], "review")
        self.assertNotEqual(again["stability"], F.DEFAULT_WEIGHTS[2])

    def test_lapse_counts_only_from_review_state(self):
        f = F.FSRS()
        s = f.review({}, "again", self.T0)
        self.assertEqual(s["lapses"], 0)
        s = f.review(s, "easy", self.T0 + timedelta(minutes=1))
        self.assertEqual(s["state"], "review")
        s = f.review(s, "again", at(s["due"]))
        self.assertEqual((s["state"], s["lapses"]), ("relearning", 1))

    def test_stability_never_below_minimum_and_interval_capped(self):
        f = F.FSRS(maximum_interval=100)
        s, now = {}, self.T0
        for _ in range(300):
            s = f.review(s, "again", now)
            self.assertGreaterEqual(s["stability"], F.STABILITY_MIN)
            now = at(s["due"]) + timedelta(days=1)
        for _ in range(8):
            s = f.review(s, "easy", now)
            self.assertLessEqual((at(s["due"]) - now).days, 100)
            now = at(s["due"])

    def test_timestamps_are_whole_seconds_even_with_a_real_clock(self):
        # fleet_learn parses only %Y-%m-%dT%H:%M:%SZ; a microsecond clock must not leak into due/lastReview
        s = F.FSRS().review({}, "good", self.T0 + timedelta(microseconds=123456))
        for k in ("due", "lastReview"):
            self.assertRegex(s[k], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_bad_input_refused(self):
        with self.assertRaises(ValueError):
            F.FSRS(weights=F.DEFAULT_WEIGHTS[:20])
        with self.assertRaises(ValueError):
            F.FSRS(desired_retention=1.0)
        for bad in ("ok", 0, 5, True, None):
            with self.assertRaises(ValueError):
                F.parse_rating(bad)
        self.assertEqual(F.parse_rating(" Good "), 3)


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fsrstest-")
        self.old = os.environ.get("FLEET_STATE")
        os.environ["FLEET_STATE"] = self.tmp
        self.t0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.tmp)
        if self.old is None:
            os.environ.pop("FLEET_STATE", None)
        else:
            os.environ["FLEET_STATE"] = self.old

    def write(self, text):
        os.makedirs(os.path.join(self.tmp, "learn"), exist_ok=True)
        p = os.path.join(self.tmp, "learn", "learn.json")
        with open(p, "w") as f:
            f.write(text)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # make the change visible

    def days(self):
        s = F.schedule({}, "easy", self.t0)
        return (at(s["due"]) - self.t0).days

    def test_learn_json_retention_is_used_by_the_hook(self):
        default = self.days()
        self.assertEqual(default, F.FSRS().interval_days(F.DEFAULT_WEIGHTS[3]))
        self.write(json.dumps({"scheduler": "fsrs", "fsrs": {"desiredRetention": 0.8}}))
        self.assertGreater(self.days(), default)
        self.write(json.dumps({"scheduler": "fsrs"}))
        self.assertEqual(self.days(), default)

    def test_bad_learn_json_raises_instead_of_defaulting(self):
        self.write(json.dumps({"fsrs": {"desiredRetention": 1.5}}))
        with self.assertRaises(ValueError):
            F.schedule({}, "good", self.t0)
        self.write("{not json")
        with self.assertRaises(ValueError):
            F.schedule({}, "good", self.t0)


class LearnHook(unittest.TestCase):
    """Through fleet_learn's API (lib/fleet_learn.py, owned by fleet-learn), when it is present."""

    def test_use_scheduler_accepts_schedule(self):
        path = os.path.join(LIB, "fleet_learn.py")
        if not os.path.exists(path):
            self.skipTest("lib/fleet_learn.py not on this branch yet")
        sys.path.insert(0, LIB)
        try:
            import fleet_learn
        finally:
            sys.path.pop(0)
        fleet_learn.use_scheduler(F.schedule)


if __name__ == "__main__":
    unittest.main()
