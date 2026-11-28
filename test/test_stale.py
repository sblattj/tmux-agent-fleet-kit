"""Tests for stale open calls (lib/fleet_stale.py), issue #63: claim refs vs WATCH events, recheck times.

Pure tests plus ledger tests on a temp dir; nothing reaches a live fleet. Run: python3 -m unittest test/test_stale.py
"""
import datetime
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _load("fleet_stale", os.path.join(ROOT, "lib", "fleet_stale.py"))
NOW = datetime.datetime(2030, 1, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)


def call(cid="Q7", at="2030-01-02T10:00:00Z", refs=("acme/widget#333",), status="open", **kw):
    c = {"id": cid, "at": at, "from": "lane-a", "question": "Merge the widget fix first?", "status": status,
         "claims": [{"id": "c1", "text": "the fix PR is still open", "source": "gh pr view 333", "options": ["a"],
                     "refs": list(refs)}]}
    c.update(kw)
    return c


def ev(ref="acme/widget#333", at="2030-01-02T11:00:00Z", eid="github:oss:pr-333-merged", type_="pr.merged",
       url="https://example.test/acme/widget/pull/333"):
    return {"eid": eid, "ref": ref, "at": at, "type": type_, "url": url, "source": "oss", "sourceKind": "github"}


class Match(unittest.TestCase):
    def test_exact_ref_match_flags_the_claim(self):
        hits = S.match([call()], [ev()])
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h["id"], h["claim"], h["ref"], h["type"]), ("Q7", "c1", "acme/widget#333", "pr.merged"))
        self.assertEqual(S.stale_line(h),
                         "STALE Q7 claim c1: ref acme/widget#333 changed (pr.merged) github:oss:pr-333-merged")

    def test_url_ref_matches_the_event_url(self):
        hits = S.match([call(refs=["https://example.test/acme/widget/pull/333"])], [ev(ref="acme/widget#333")])
        self.assertEqual([h["claim"] for h in hits], ["c1"])

    def test_bare_number_does_not_match_another_repos_ref(self):
        self.assertEqual(S.match([call(refs=["#333"])], [ev(ref="other/repo#333", url="")]), [])
        self.assertEqual(S.match([call(refs=["#333"])], [ev(ref="acme/widget#333", url="")]), [])

    def test_no_free_text_or_substring_matching(self):
        c = call(refs=["acme/widget#33"])
        c["claims"][0]["text"] = "acme/widget#333 is open"
        self.assertEqual(S.match([c], [ev()]), [])

    def test_whitespace_is_trimmed(self):
        self.assertEqual(len(S.match([call(refs=["  acme/widget#333 "])], [ev()])), 1)

    def test_events_before_the_call_opened_are_ignored(self):
        self.assertEqual(S.match([call(at="2030-01-02T12:00:00Z")], [ev(at="2030-01-02T11:00:00Z")]), [])

    def test_closed_calls_and_calls_without_claims_are_skipped(self):
        self.assertEqual(S.match([call(status="answered")], [ev()]), [])
        self.assertEqual(S.match([{"id": "Q1", "status": "open", "question": "x"}], [ev()]), [])
        self.assertEqual(S.match([{"id": "Q1", "status": "open", "claims": "nope"}], [ev()]), [])

    def test_one_hit_per_claim_ref_keeps_the_latest_event(self):
        hits = S.match([call()], [ev(eid="e1", at="2030-01-02T11:00:00Z", type_="pr.review"),
                                  ev(eid="e2", at="2030-01-02T11:30:00Z", type_="pr.merged"),
                                  ev(eid="e0", at="2030-01-02T10:30:00Z", type_="pr.comment")])
        self.assertEqual([(h["eid"], h["type"]) for h in hits], [("e2", "pr.merged")])

    def test_each_claim_numbered_from_one(self):
        c = call()
        c["claims"].append({"id": "c2", "text": "ticket is in review", "source": "tracker", "refs": ["PROJ-9"]})
        hits = S.match([c], [ev(ref="PROJ-9", url="", eid="jira:t:PROJ-9")])
        self.assertEqual([(h["claim"], h["ref"]) for h in hits], [("c2", "PROJ-9")])

    def test_claims_without_ids_number_by_position(self):
        c = call()
        del c["claims"][0]["id"]
        self.assertEqual(S.match([c], [ev()])[0]["claim"], "c1")

    def test_event_objects_work_like_dicts(self):
        class E:
            eid, ref, at, type, url = "e9", "acme/widget#333", "2030-01-02T11:00:00Z", "pr.closed", ""
        self.assertEqual(len(S.match([call()], [E()])), 1)

    def test_stale_line_round_trips(self):
        h = S.match([call()], [ev(type_="pr state (merged)")])[0]
        line = S.stale_line(h)
        p = S.parse_stale_line(line)
        self.assertEqual((p["id"], p["claim"], p["ref"], p["eid"]), ("Q7", "c1", "acme/widget#333", h["eid"]))
        self.assertIsNone(S.parse_stale_line("STALE mesh lane-a pid 4: old code"))


class Recheck(unittest.TestCase):
    def test_parse_durations_and_iso(self):
        self.assertEqual(S.parse_recheck("4h", NOW), "2030-01-02T16:00:00Z")
        self.assertEqual(S.parse_recheck("30m", NOW), "2030-01-02T12:30:00Z")
        self.assertEqual(S.parse_recheck("2d", NOW), "2030-01-04T12:00:00Z")
        self.assertEqual(S.parse_recheck("2030-01-03T08:00:00Z", NOW), "2030-01-03T08:00:00Z")
        self.assertEqual(S.parse_recheck("2030-01-03T09:00:00+01:00", NOW), "2030-01-03T08:00:00Z")
        self.assertIsNone(S.parse_recheck(None, NOW))

    def test_parse_refuses_junk(self):
        for bad in ("tomorrow", "0h", "-3h", 5, "2030-01-03"):
            with self.assertRaises(ValueError):
                S.parse_recheck(bad, NOW)

    def test_due_only_open_and_past(self):
        calls = [call("Q1", recheckAfter="2030-01-02T11:00:00Z"), call("Q2", recheckAfter="2030-01-02T13:00:00Z"),
                 call("Q3", recheckAfter="2030-01-02T11:00:00Z", status="answered"), call("Q4")]
        self.assertEqual([c["id"] for c in S.recheck_due(calls, NOW)], ["Q1"])
        line = S.recheck_line(calls[0])
        self.assertEqual(line, "RECHECK Q1 from lane-a: recheckAfter 2030-01-02T11:00:00Z passed")
        self.assertEqual(S.parse_recheck_line(line)["id"], "Q1")


class Ledgers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="staletest-")
        self.lp = S.ledger_paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_stale_fires_once_per_event(self):
        hits = S.match([call()], [ev()])
        self.assertEqual(len(S.new_stale(self.lp["stale"], hits, NOW)), 1)
        self.assertEqual(S.new_stale(self.lp["stale"], hits, NOW), [])
        later = S.match([call()], [ev(eid="github:oss:pr-333-reopened", type_="pr.reopened",
                                      at="2030-01-02T11:45:00Z")])
        self.assertEqual(len(S.new_stale(self.lp["stale"], later, NOW)), 1)
        rows = S.stale_by_call(self.lp["stale"], {"Q7"})
        self.assertEqual([(r["claim"], r["type"]) for r in rows["Q7"]], [("c1", "pr.reopened")])
        self.assertEqual(S.stale_by_call(self.lp["stale"], {"Q8"}), {})

    def test_new_recheck_fires_once_per_value(self):
        c = call("Q1", recheckAfter="2030-01-02T11:00:00Z")
        self.assertEqual(len(S.new_recheck(self.lp["recheck"], [c], NOW)), 1)
        self.assertEqual(S.new_recheck(self.lp["recheck"], [c], NOW), [])
        c2 = dict(c, recheckAfter="2030-01-02T11:30:00Z")  # the asker set a new time: it fires again
        self.assertEqual(len(S.new_recheck(self.lp["recheck"], [c2], NOW)), 1)

    def test_candidates_are_grounded_in_the_ledger(self):
        S.new_stale(self.lp["stale"], S.match([call()], [ev()]), NOW)
        cands = S.candidates(self.lp["stale"], {"Q7"})
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual((c["id"], c["kind"], c["path"]), ("Q7", "stale", self.lp["stale"]))
        with open(c["path"]) as f:
            raw = f.read()
        self.assertIn(json.dumps(c["quote"])[1:-1], raw)
        self.assertIn("decision_revise", c["text"])
        self.assertEqual(S.candidates(self.lp["stale"], set()), [])


if __name__ == "__main__":
    unittest.main()
