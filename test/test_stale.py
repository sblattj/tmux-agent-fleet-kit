"""Tests for stale open calls (lib/fleet_stale.py), issue #63: claim refs vs WATCH events, recheck times.

Pure tests plus ledger tests on a temp dir; nothing reaches a live fleet. Run: python3 -m unittest test/test_stale.py
"""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
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


# ---------------------------------------------------------------- end to end on a temp FLEET_STATE

LIB = os.path.join(ROOT, "lib")
FLEET = os.path.join(ROOT, "bin", "fleet")
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fleet(unittest.TestCase):
    """decision_open with claims and recheckAfter, a WATCH run, the card, refresh and intake, on one temp state."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="stale-e2e-")
        self._env = {k: os.environ.get(k) for k in ("FLEET_STATE", "FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN",
                                                     "FLEET_NAME", "TMUX")}
        os.environ["FLEET_STATE"] = self.state
        for k in ("FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN", "FLEET_NAME", "TMUX"):
            os.environ.pop(k, None)
        import fleet_mesh
        import fleet_decisions
        self.mesh, self.dec = fleet_mesh, fleet_decisions

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.state, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.state, *parts)

    def open_call(self, refs=("acme/widget#333",), **kw):
        args = {"from": "lane-a", "question": "Merge the widget fix before the release?", "options": ["yes", "no"],
                "claims": [{"id": "c1", "text": "the widget fix PR is still open", "source": "gh pr view 333",
                            "options": ["a"], "refs": list(refs)}]}
        args.update(kw)
        res = self.mesh.t_decision_open(args)
        return res.split()[1].rstrip(";")

    def watch_run(self, events):
        path = self.p("events-in.jsonl")
        with open(path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        with open(self.p("watch.json"), "w") as f:
            json.dump({"sources": [{"name": "oss", "kind": "command", "argv": ["cat", path], "timeoutSec": 10}],
                       "filters": [{"kind": "fyi", "when": {"any": True}}]}, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        env["FLEET_STATE"] = self.state
        r = subprocess.run([FLEET, "watch", "run", "--once", "--force", "--json"], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def event(self, ref="acme/widget#333", type_="pr.merged", n=1):
        at = _iso(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=n))
        return {"actor": "Bob_Example", "at": at, "eid": "command:oss:%s:%s:%s" % (ref, at, type_), "fields": {},
                "quote": "merged", "ref": ref, "tags": [], "title": "widget fix", "type": type_,
                "url": "https://example.test/acme/widget/pull/333"}

    def desk_lines(self):
        return [ln for m in _jsonl(self.p("inbox", "DECIDE.jsonl")) for ln in (m.get("text") or "").split("\n")]

    def test_recheck_after_is_stored_as_iso_and_junk_refused(self):
        cid = self.open_call(recheckAfter="4h")
        row = self.mesh.get_decision(cid)
        t = datetime.datetime.strptime(row["recheckAfter"], "%Y-%m-%dT%H:%M:%SZ")
        delta = t.replace(tzinfo=datetime.timezone.utc) - datetime.datetime.now(datetime.timezone.utc)
        self.assertTrue(3.9 * 3600 < delta.total_seconds() <= 4 * 3600, delta)
        with self.assertRaises(ValueError):
            self.open_call(recheckAfter="next tuesday")
        self.assertNotIn("recheckAfter", self.mesh.get_decision(self.open_call()))

    def test_watch_flags_a_changed_claim_ref_once_and_refresh_asks_the_asker(self):
        cid = self.open_call()
        other = self.open_call(refs=["other/repo#333"], question="Unrelated call on another repo?")
        merged = self.event()
        res = self.watch_run([merged])
        self.assertEqual(res.get("stale"), 1, res)
        stale_lines = [ln for ln in self.desk_lines() if ln.startswith("STALE")]
        self.assertEqual(len(stale_lines), 1)
        self.assertTrue(stale_lines[0].startswith("STALE %s claim c1: ref acme/widget#333 changed (pr.merged) "
                                                  % cid), stale_lines)
        self.assertFalse(any(other + " " in ln for ln in stale_lines))
        self.assertFalse(os.path.exists(self.p("inbox", "ROUTE.jsonl")))
        # a second run with the same event: dedupe (seen) and the ledger keep it at one line
        self.watch_run([merged])
        self.assertEqual(len([ln for ln in self.desk_lines() if ln.startswith("STALE")]), 1)
        # the card carries it, with the call's age
        cards, _ = self.dec.open_cards()
        card = [c for c in cards if c["id"] == cid][0]
        self.assertEqual([(r["claim"], r["ref"]) for r in card["stale"]], [("c1", "acme/widget#333")])
        self.assertTrue(card["age"])
        self.assertFalse(card["recheck"])
        self.assertIn("stale: claim c1 ref acme/widget#333 changed (pr.merged)",
                      "\n".join(self.dec.stale_card_lines(card)))
        self.assertEqual([c for c in cards if c["id"] == other][0]["stale"], [])
        # refresh offers it as a grounded stale row; B + confirm pings the asker and leaves the call as it was
        out = self.dec.refresh_all({"refreshSources": []})
        rows = {r["id"]: r for r in out["rows"]}
        self.assertEqual(rows[cid]["b"]["kind"], "stale")
        self.assertNotIn(other, rows)
        q_before = self.mesh.get_decision(cid)["question"]
        self.dec.refresh_choose(cid, "B")
        got = self.dec.refresh_confirm(cid)
        self.assertEqual(got["asked"], "lane-a")
        asked = _jsonl(self.p("inbox", "lane-a.jsonl"))
        self.assertTrue(any("decision_revise" in m["text"] and cid in m["text"] for m in asked), asked)
        row = self.mesh.get_decision(cid)
        self.assertEqual((row["status"], row["question"]), ("open", q_before))

    def test_answered_calls_are_not_flagged(self):
        cid = self.open_call()
        self.mesh.mark_decision(cid, "yes")
        res = self.watch_run([self.event()])
        self.assertEqual(res.get("stale"), 0)
        self.assertFalse(any(ln.startswith("STALE") for ln in self.desk_lines()))

    def test_recheck_due_goes_to_decide_once_and_intake_pings_the_asker_once(self):
        past = _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5))
        cid = self.open_call(recheckAfter=past)
        later = self.open_call(recheckAfter="2d", question="A call with a later recheck time?")
        self.watch_run([])
        self.watch_run([])
        rl = [ln for ln in self.desk_lines() if ln.startswith("RECHECK")]
        self.assertEqual(rl, ["RECHECK %s from lane-a: recheckAfter %s passed" % (cid, past)])
        cards, _ = self.dec.open_cards()
        by = {c["id"]: c for c in cards}
        self.assertTrue(by[cid]["recheck"])
        self.assertFalse(by[later]["recheck"])
        out = self.dec.intake(rl[0])
        self.assertEqual([r["id"] for r in out.get("recheck", [])], [cid])
        out2 = self.dec.intake(rl[0])
        self.assertEqual(out2.get("recheck"), None)
        self.assertTrue(any("already pinged" in s for s in out2["skipped"]))
        pings = [m for m in _jsonl(self.p("inbox", "lane-a.jsonl")) if "recheck time" in m["text"]]
        self.assertEqual(len(pings), 1)

    def test_intake_lists_stale_lines_without_skipping_them(self):
        out = self.dec.intake("STALE Q4 claim c1: ref acme/widget#333 changed (pr.merged) command:oss:x")
        self.assertEqual([h["id"] for h in out["stale"]], ["Q4"])
        self.assertEqual(out["skipped"], [])


if __name__ == "__main__":
    unittest.main()
