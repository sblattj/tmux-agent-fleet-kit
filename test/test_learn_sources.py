#!/usr/bin/env python3
"""LEARN reads the real sources through WATCH's adapter registry (Q702), keeps typed links and a derived entities
file (Q697), and gets both back from a rebuild that keeps only reviews.jsonl and seen.jsonl (Q703).

Fixtures and fakes only: a `whosaid` source runs the fake CLI in test/fixtures/watch/whosaid, a `command` source
cats a recorded events file. No network, no real names, credentials as env references with fake values. Every case
runs on a temp $FLEET_STATE. Python 3.8+ stdlib only.
"""
import json
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from test.test_learn_lesson import LATER, Base  # noqa: E402

FXW = os.path.join(ROOT, "test", "fixtures", "watch", "whosaid")
FAKE_TOKEN = "fake-tracker-token-0123"

TRACKER_EVENTS = [
    {"type": "issue.closed", "ref": "PROJ-1", "at": "2026-03-01T10:00:00Z", "title": "Cap upload retries",
     "quote": "PROJ-1 Done: cap upload retries at 3", "url": "https://tracker.example/PROJ-1", "actor": "Dev One",
     "fields": {"links": [{"rel": "part-of", "type": "tracker", "ref": "PROJ-0"},
                          {"rel": "ships-in", "type": "release", "ref": "v1.4.0"},
                          {"rel": "blocked-by", "type": "tracker", "ref": "PROJ-9"},
                          {"rel": "owner", "type": "person", "ref": "Dev One"},
                          {"rel": "likes", "type": "person", "ref": "Dev Two"}]}},
    {"type": "issue.comment", "ref": "PROJ-2", "at": "2026-03-01T11:00:00Z", "title": "Bot noise",
     "quote": "PROJ-2 ci bot says rerun", "actor": "ci-bot"},
    {"type": "issue.comment", "ref": "PROJ-3", "at": "2026-03-01T12:00:00Z", "title": "Leaked",
     "quote": "PROJ-3 token ghp_FAKE0123456789abcdefFAKE0123456789ab pasted"},
]


class SourcesBase(Base):
    def setUp(self):
        super().setUp()
        self.ws = os.path.join(self.tmp, "meetings")
        d = os.path.join(self.ws, "2026-03-01 planning")
        os.makedirs(d)
        shutil.copy(os.path.join(FXW, "decisions-standup.json"), os.path.join(d, "decisions.json"))
        os.utime(d, (1000, 1000))
        self.events = os.path.join(self.tmp, "tracker.jsonl")
        self.write_events(TRACKER_EVENTS)
        self.sources()

    def write_events(self, evs):
        with open(self.events, "w") as f:
            for e in evs:
                f.write(json.dumps(e) + "\n")

    def sources(self, auth=None, name="sources.json"):
        cfg = {"me": {"names": ["me"]},
               "sources": [
                   {"name": "calls", "kind": "whosaid", "workspace": self.ws,
                    "bin": os.path.join(FXW, "fake-whosaid")},
                   {"name": "tracker", "kind": "command",
                    "argv": ["sh", "-c", '[ "$FLEET_WATCH_TOKEN" = "%s" ] && cat "$0"' % FAKE_TOKEN, self.events],
                    "auth": auth or {"type": "bearer", "token": "env:FAKE_TRACKER_TOKEN"}},
                   {"name": "hooks", "kind": "webhook", "auth": {"type": "hmac", "secret": "env:FAKE_HOOK"}}],
               "filters": [{"drop": {"actor": ["ci-bot"]}}]}
        with open(os.path.join(self.state, name), "w") as f:
            json.dump(cfg, f)

    def sync(self, now=None):
        kw = {"env": {"FAKE_TRACKER_TOKEN": FAKE_TOKEN}}
        if now:
            kw["now"] = now
        return json.loads(self.ok("sync", "--json", **kw))

    def rows(self):
        return {(r["source"]["type"], r["source"]["ref"]): r for r in json.loads(self.ok("list", "--json"))}


class TestSourcesSync(SourcesBase):
    def test_sources_become_rows_with_typed_links(self):
        res = self.sync()
        rep = res["sources"]
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["polled"], {"calls": 2, "tracker": 1})  # the webhook route is not polled
        self.assertEqual(rep["dropped"], {"drop actor=ci-bot": 1, "secret": 1})
        rows = self.rows()
        d1 = rows[("whosaid", "2026-03-01 planning#D1")]
        d2 = rows[("whosaid", "2026-03-01 planning#D2")]
        self.assertEqual((d1["kind"], d1["status"], d1["origin"]), ("decision", "confirmed", "outside"))
        self.assertEqual(d2["status"], "heard")  # proposed, nobody agreed: not decided yet
        self.assertEqual(d1["quote"], "we cap upload retries at 3, jittered")
        t = rows[("command", "PROJ-1")]
        self.assertEqual(t["kind"], "closed")
        rels = sorted((l.get("rel"), l["ref"]) for l in t["links"] if l.get("rel"))
        self.assertEqual(rels, [("blocked-by", "PROJ-9"), ("owner", "Dev One"), ("part-of", "PROJ-0"),
                                ("ships-in", "v1.4.0")])  # the unknown rel "likes" is left out
        self.assertNotIn(("command", "PROJ-2"), rows)  # drop filter
        self.assertNotIn(("command", "PROJ-3"), rows)  # secret
        self.assertNotIn("ghp_FAKE", json.dumps(list(rows.values())))
        ents = {e["key"]: e for e in self.jsonl("entities.jsonl")}
        self.assertEqual(ents["person:Dev One"]["rows"],
                         sorted([{"lid": d1["lid"], "rel": "decided-by"}, {"lid": t["lid"], "rel": "owner"}],
                                key=lambda x: x["lid"]))
        self.assertIn("meeting:2026-03-01 planning", ents)
        self.assertIn("release:v1.4.0", ents)
        # idempotent: a second sync adds nothing
        self.assertEqual(self.sync()["added"], [])

    def test_a_new_state_supersedes_the_old_row(self):
        self.sync()
        old = self.rows()[("command", "PROJ-1")]
        self.write_events([dict(TRACKER_EVENTS[0], quote="PROJ-1 Reopened: cap upload retries at 5")])
        res = self.sync()
        new = self.rows()[("command", "PROJ-1")]
        self.assertEqual(res["superseded"], [[old["lid"], new["lid"]]])
        self.assertEqual(new["quote"], "PROJ-1 Reopened: cap upload retries at 5")
        self.assertTrue(any(l.get("rel") == "ships-in" for l in new["links"]))

    def test_a_literal_credential_polls_nothing(self):
        self.sources(auth={"type": "bearer", "token": FAKE_TOKEN})
        res = self.sync()
        self.assertTrue(res["sources"]["errors"])
        self.assertEqual(res["sources"]["polled"], {})
        self.assertNotIn(FAKE_TOKEN, json.dumps(res))
        self.assertEqual(self.rows(), {})

    def test_an_unset_reference_fails_that_source_only(self):
        res = json.loads(self.ok("sync", "--json"))  # FAKE_TRACKER_TOKEN not set
        self.assertIn("tracker", res["sources"]["failed"])
        self.assertEqual(res["sources"]["polled"], {"calls": 2})

    def test_watch_json_is_the_fallback_and_watch_reads_the_shared_file(self):
        os.remove(os.path.join(self.state, "sources.json"))
        self.sources(name="watch.json")
        self.assertEqual(self.sync()["sources"]["polled"], {"calls": 2, "tracker": 1})
        os.remove(os.path.join(self.state, "watch.json"))
        self.sources()
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import fleet_watch
        self.assertEqual(fleet_watch._config_path(self.state), os.path.join(self.state, "sources.json"))
        self.sources(name="watch.json")
        self.assertEqual(fleet_watch._config_path(self.state), os.path.join(self.state, "watch.json"))


class TestExampleConfig(unittest.TestCase):
    def test_the_example_sources_config_is_valid_and_refs_only(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import fleet_watch_contract as C
        cfg = C.load_config(os.path.join(ROOT, "docs", "examples", "sources.example.json"))
        self.assertEqual(C.validate_config(cfg), [])
        kinds = {s["kind"] for s in cfg["sources"]}
        self.assertTrue({"github", "linear", "whosaid"} <= kinds)
        for s in cfg["sources"]:
            for k, v in (s.get("auth") or {}).items():
                if k in C.SECRET_KEYS:
                    self.assertTrue(C.is_credential_ref(v), (s["name"], k))


class TestLinksSurviveRebuild(SourcesBase):
    def snap(self):
        rows = sorted((r["source"]["type"], r["source"]["ref"], r["quote"], r["status"],
                       json.dumps(sorted(r["links"], key=json.dumps), sort_keys=True))
                      for r in json.loads(self.ok("list", "--json")))
        return rows, self.jsonl("entities.jsonl")

    def test_links_and_entities_come_back_after_a_rebuild(self):
        self.sync()
        self.ok("lesson", "--yes")
        before = self.snap()
        self.assertTrue(before[1])
        res = json.loads(self.ok("rebuild", "--json", now=LATER, env={"FAKE_TRACKER_TOKEN": FAKE_TOKEN}))
        self.assertEqual(res["seenLeftOut"], 0)
        self.assertEqual(self.snap(), before)

    def test_empty_state_plus_the_two_files_gives_the_links_back(self):
        self.sync()
        self.ok("lesson", "--yes")
        before = self.snap()
        learn = os.path.join(self.state, "learn")
        keep = {}
        for n in ("reviews.jsonl", "seen.jsonl"):
            p = os.path.join(learn, n)
            if os.path.exists(p):
                with open(p) as f:
                    keep[n] = f.read()
        shutil.rmtree(learn)
        os.makedirs(learn)
        self.cfg({"scheduler": "fixed"})
        for n, text in keep.items():
            with open(os.path.join(learn, n), "w") as f:
                f.write(text)
        self.ok("rebuild", "--json", now=LATER, env={"FAKE_TRACKER_TOKEN": FAKE_TOKEN})
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.jsonl("seen.jsonl") != [], "seen.jsonl" in keep)

    def test_a_failing_source_stops_the_rebuild_and_changes_nothing(self):
        self.sync()
        self.ok("lesson", "--yes")
        learn = os.path.join(self.state, "learn")
        names = ("ledger.jsonl", "cards.jsonl", "seen.jsonl", "entities.jsonl", "cursors.json")
        before = {}
        for n in names:
            with open(os.path.join(learn, n)) as f:
                before[n] = f.read()
        p = self.learn("rebuild", "--json", now=LATER)  # FAKE_TRACKER_TOKEN unset: the tracker source fails
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tracker", p.stderr + p.stdout)
        for n in names:
            with open(os.path.join(learn, n)) as f:
                self.assertEqual(f.read(), before[n], n)
        self.assertTrue(os.listdir(os.path.join(learn, "archive")))  # the copy is still made
        # control: with the reference set, the same rebuild runs and keeps every seen row
        res = json.loads(self.ok("rebuild", "--json", now=LATER, env={"FAKE_TRACKER_TOKEN": FAKE_TOKEN}))
        self.assertEqual(res["seenLeftOut"], 0)


class TestDrain(SourcesBase):
    def test_more_meetings_than_one_poll_takes_all_come_in_and_survive_a_rebuild(self):
        for i in range(25):
            d = os.path.join(self.ws, "2026-04-%02d sync" % (i + 1))
            os.makedirs(d)
            shutil.copy(os.path.join(FXW, "decisions-standup.json"), os.path.join(d, "decisions.json"))
            os.utime(d, (2000 + i, 2000 + i))
        res = self.sync()
        self.assertEqual(res["sources"]["polled"]["calls"], 52)  # 26 meetings x 2, over two polls of 20
        self.ok("lesson", "--yes")
        seen = len(self.jsonl("seen.jsonl"))
        out = json.loads(self.ok("rebuild", "--json", now=LATER, env={"FAKE_TRACKER_TOKEN": FAKE_TOKEN}))
        self.assertEqual((out["seenLeftOut"], out["seenKept"]), (0, seen))


if __name__ == "__main__":
    unittest.main()
