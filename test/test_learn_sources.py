#!/usr/bin/env python3
"""test_learn_sources.py: LEARN reads the real sources (Q702) and rebuilds from them (Q703 b, Q697 a).

The whosaid adapter (a fake `whosaid` script over a fixture), the Linear adapter (an injected http over fixture
pages), the shared sources config (references only), and `rebuild` from sources: links come back, only reviews and
seen survive, a failing source aborts with the old state intact, the guard refuses lane roles. Fixtures and fakes
only: no network, no real names. Run: python3 -m unittest test/test_learn_sources.py -v
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch")
FLEARN = os.path.join(ROOT, "bin", "fleet-learn")
sys.path.insert(0, LIB)

import fleet_watch_contract as C  # noqa: E402
import fleet_learn as fl  # noqa: E402
import fleet_learn_lesson as les  # noqa: E402
import fleet_learn_sources as S  # noqa: E402
from watch_sources.linear import LinearSource  # noqa: E402
from watch_sources.whosaid import WhosaidSource  # noqa: E402

FAKE_WHOSAID = os.path.join(FIX, "whosaid", "fake-whosaid")
MEETING = "2026-01-05-0900"


def page(name):
    with open(os.path.join(FIX, "linear", name), "rb") as f:
        return f.read()


class FakeHttp:
    """Serves the fixture pages in order; records every request. fail=status makes every call answer that."""

    def __init__(self, pages=("issues_page1.json", "issues_page2.json"), fail=None, headers=None, body=None):
        self.pages, self.fail, self.calls, self.hdrs, self.body = list(pages), fail, [], headers or {}, body

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": json.loads(body)})
        if self.fail:
            return self.fail, self.hdrs, self.body or b"{}"
        if self.body is not None:
            return 200, self.hdrs, self.body
        after = self.calls[-1]["body"]["variables"].get("after")  # page by the GraphQL cursor: re-polls restart
        i = 0 if not after else min(int(after[1:]), len(self.pages) - 1)
        return 200, self.hdrs, page(self.pages[i])


def linear_class(fake):
    return type("FakeLinear", (LinearSource,), {"http": staticmethod(fake)})


def workspace(tmp):
    ws = os.path.join(tmp, "meetings")
    os.makedirs(os.path.join(ws, MEETING))
    os.makedirs(os.path.join(ws, "_cache"))
    open(os.path.join(ws, "_search.db-shm"), "w").close()
    return ws


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="learnsrc-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.state, "learn"))
        with open(os.path.join(self.state, "learn", "learn.json"), "w") as f:
            json.dump({"scheduler": "fixed"}, f)
        self._env = {k: os.environ.get(k) for k in ("FLEET_STATE", "FLEET_ROLE", "FLEET_LEARN_NOW", "LINEAR_KEY")}
        os.environ["FLEET_STATE"] = self.state
        os.environ.pop("FLEET_ROLE", None)
        os.environ["FLEET_LEARN_NOW"] = "2026-01-10T09:00:00Z"
        S.OVERRIDES.clear()

    def tearDown(self):
        S.OVERRIDES.clear()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ whosaid adapter

class TestWhosaid(Tmp):
    def src(self, **kw):
        cfg = {"name": "calls", "kind": "whosaid", "workspace": workspace(self.tmp), "bin": FAKE_WHOSAID}
        cfg.update(kw)
        return WhosaidSource(cfg, {}, {})

    def test_poll_reads_each_meeting_folder_only(self):
        evs, cur, _ = self.src().poll(None)
        self.assertEqual(cur, MEETING)  # _cache and _search.db-shm are not meetings
        self.assertEqual([e.type for e in evs], ["decision.agreed", "decision.proposed"])
        e = evs[0]
        self.assertEqual(e.ref, MEETING + "#d1")
        self.assertEqual(e.at, "2026-01-05T09:05:00Z")
        self.assertEqual(e.quote, "let's cap upload retries at 3")
        self.assertEqual(e.fields["topic"], "upload retries")
        self.assertEqual(e.fields["edges"], {"decided-by": ["call:" + MEETING]})
        self.assertTrue(e.eid.startswith("whosaid:calls:"))

    def test_no_speaker_name_on_any_event(self):
        evs, _, _ = self.src().poll(None)
        blob = json.dumps([e.to_dict() for e in evs])
        self.assertNotIn("Speaker A", blob)
        self.assertNotIn("Speaker B", blob)
        self.assertIn("let's cap", blob)  # control: the quote itself is there

    def test_cursor_skips_read_meetings(self):
        evs, cur, _ = self.src().poll(MEETING)
        self.assertEqual((evs, cur), ([], MEETING))

    def test_failing_cli_and_missing_workspace_raise(self):
        os.makedirs(os.path.join(self.tmp, "meetings2", "2026-02-01-1000"))
        bad = WhosaidSource({"name": "c", "workspace": os.path.join(self.tmp, "meetings2"), "bin": FAKE_WHOSAID}, {}, {})
        with self.assertRaisesRegex(C.SourceError, "exit 3"):
            bad.poll(None)
        gone = WhosaidSource({"name": "c", "workspace": os.path.join(self.tmp, "nope"), "bin": FAKE_WHOSAID}, {}, {})
        with self.assertRaisesRegex(C.SourceError, "not a folder"):
            gone.poll(None)

    def test_timeout_is_bounded(self):
        slow = os.path.join(self.tmp, "slow-whosaid")
        with open(slow, "w") as f:
            f.write("#!/bin/sh\nsleep 30\n")
        os.chmod(slow, os.stat(slow).st_mode | stat.S_IEXEC)
        with self.assertRaisesRegex(C.SourceError, "timeout"):
            self.src(bin=slow, timeoutSec=1).poll(None)

    def test_registered(self):
        self.assertIs(C.load_source("whosaid"), WhosaidSource)
        self.assertEqual(C.validate_config({"sources": [{"name": "c", "kind": "whosaid"}]}) != [], True)


# ------------------------------------------------------------------ Linear adapter

class TestLinear(Tmp):
    def src(self, fake, auth=None):
        cls = linear_class(fake)
        return cls({"name": "tracker", "kind": "linear", "teams": ["ENG"]}, auth or {"type": "pat", "token": "k1"}, {})

    def test_events_edges_and_cursor(self):
        fake = FakeHttp()
        evs, cur, _ = self.src(fake).poll(None)
        self.assertEqual(cur, "2026-01-07T08:00:00.000Z")
        got = sorted((e.ref, e.type) for e in evs)
        self.assertEqual(got, [("ENG-12", "issue.closed"), ("ENG-12", "issue.comment"), ("ENG-12", "issue.created"),
                               ("ENG-9", "issue.created")])
        e12 = [e for e in evs if e.ref == "ENG-12"][0]
        self.assertEqual(e12.fields["edges"], {"part-of": ["Uploads"], "blocked-by": ["ENG-9"], "owner": ["Dev One"],
                                               "ships-in": ["Cycle 4"]})
        e9 = [e for e in evs if e.ref == "ENG-9"][0]
        self.assertEqual(e9.fields["edges"], {"part-of": ["ENG"]})  # no project or parent: the team
        closed = [e for e in evs if e.type == "issue.closed"][0]
        self.assertEqual(closed.quote, "Retry uploads with backoff: In Progress -> Done")
        self.assertEqual(len(fake.calls), 2)  # page 1 says hasNextPage, page 2 ends it
        self.assertEqual(fake.calls[1]["body"]["variables"]["after"], "c1")
        self.assertTrue(all(c["method"] == "POST" and "mutation" not in c["body"]["query"] for c in fake.calls))
        self.assertEqual(fake.calls[0]["body"]["variables"]["filter"], {"team": {"key": {"in": ["ENG"]}}})

    def test_second_poll_filters_on_the_cursor(self):
        fake = FakeHttp(pages=("issues_page2.json",))
        self.src(fake).poll("2026-01-06T10:00:00.000Z")
        self.assertEqual(fake.calls[0]["body"]["variables"]["filter"]["updatedAt"], {"gt": "2026-01-06T10:00:00.000Z"})

    def test_auth_header_forms(self):
        fake = FakeHttp()
        self.src(fake, {"type": "pat", "token": "k1"}).poll(None)
        self.assertEqual(fake.calls[0]["headers"]["Authorization"], "k1")
        fake = FakeHttp()
        self.src(fake, {"type": "bearer", "token": "k2"}).poll(None)
        self.assertEqual(fake.calls[0]["headers"]["Authorization"], "Bearer k2")

    def test_errors_raise_without_the_key(self):
        cases = [(FakeHttp(fail=429, headers={"retry-after": "30"}), "rate limited"),
                 (FakeHttp(fail=500), "HTTP 500"),
                 (FakeHttp(body=page("errors_auth.json")), "query error"),
                 (FakeHttp(body=b"<html>"), "not JSON")]
        for fake, msg in cases:
            with self.assertRaises(C.SourceError) as cm:
                self.src(fake, {"type": "pat", "token": "secret-key-1"}).poll(None)
            self.assertIn(msg, str(cm.exception))
            self.assertNotIn("secret-key-1", str(cm.exception))
        with self.assertRaises(C.SourceError) as cm:
            self.src(FakeHttp(fail=429, headers={"retry-after": "30"})).poll(None)
        self.assertEqual(cm.exception.retry_after, 30)

    def test_registered_and_needs_teams(self):
        self.assertIs(C.load_source("linear"), LinearSource)
        self.assertTrue(C.validate_config({"sources": [{"name": "t", "kind": "linear"}]}))
        self.assertEqual(C.validate_config({"sources": [{"name": "t", "kind": "linear", "teams": ["ENG"]}]}), [])


# ------------------------------------------------------------------ the shared config

class TestConfig(Tmp):
    def write(self, cfg, name="sources.json"):
        with open(os.path.join(self.state, name), "w") as f:
            json.dump(cfg, f)

    def test_no_config_no_polling(self):
        self.assertEqual(S.poll_all()["events"], [])
        self.assertEqual(S.check(), [("config", True, "no sources.json or watch.json: LEARN reads only local files")])

    def test_literal_credential_refused(self):
        self.write({"sources": [{"name": "t", "kind": "linear", "teams": ["ENG"],
                                 "auth": {"type": "pat", "token": "lin_api_literal_value"}}]})
        rows = S.check()
        self.assertTrue(any(not ok for _w, ok, _n in rows), rows)
        self.assertNotIn("lin_api_literal_value", json.dumps(rows))
        with self.assertRaises(S.SourceFailed) as cm:
            S.poll_all()
        self.assertNotIn("lin_api_literal_value", str(cm.exception))
        p = subprocess.run([FLEARN, "sources"], env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("lin_api_literal_value", p.stdout + p.stderr)

    def test_env_reference_resolves_and_watch_json_is_the_fallback(self):
        self.write({"sources": [{"name": "t", "kind": "linear", "teams": ["ENG"],
                                 "auth": {"type": "pat", "token": "env:LINEAR_KEY"}}]}, "watch.json")
        fake = FakeHttp()
        S.OVERRIDES["linear"] = linear_class(fake)
        res = S.poll_all(env={"LINEAR_KEY": "k9"})
        self.assertEqual(S.config_path()[1], "watch.json")
        self.assertEqual(fake.calls[0]["headers"]["Authorization"], "k9")
        self.assertEqual(res["cursors"], {"t": "2026-01-07T08:00:00.000Z"})
        self.assertNotIn("k9", json.dumps(res))

    def test_secret_shaped_event_is_dropped(self):
        body = json.loads(page("issues_page2.json").decode())
        body["data"]["issues"]["nodes"][0]["title"] = "rotate key ghp_abcdefghijklmnopqrstuvwxyz0123"
        S.OVERRIDES["linear"] = linear_class(FakeHttp(body=json.dumps(body).encode()))
        self.write({"sources": [{"name": "t", "kind": "linear", "teams": ["ENG"]}]})
        res = S.poll_all()
        self.assertEqual((res["events"], res["secretDropped"]), ([], 1))

    def test_example_config_is_generic_and_refs_only(self):
        with open(os.path.join(ROOT, "docs", "learn-sources.example.json")) as f:
            cfg = json.load(f)
        self.assertEqual(C.validate_config(cfg), [])
        self.assertTrue(all(ok for _w, ok, _n in S.check(cfg)))
        self.assertEqual({s["kind"] for s in cfg["sources"]}, {"github", "linear", "whosaid"})


# ------------------------------------------------------------------ rebuild from sources

class TestRebuild(Tmp):
    def setUp(self):
        super().setUp()
        self.ws = workspace(self.tmp)
        self.fake = FakeHttp()
        S.OVERRIDES["linear"] = linear_class(self.fake)
        with open(os.path.join(self.state, "sources.json"), "w") as f:
            json.dump({"sources": [{"name": "tracker", "kind": "linear", "teams": ["ENG"]},
                                   {"name": "calls", "kind": "whosaid", "workspace": self.ws, "bin": FAKE_WHOSAID}]}, f)

    def links(self):
        return {k: e.get("edges") or {} for k, e in fl.entities().items()}

    def test_sync_brings_rows_links_and_heard_calls(self):
        fl.sync()
        rows = fl.items()
        calls = [r for r in rows if (r["source"].get("sourceKind")) == "whosaid"]
        self.assertEqual(sorted(r["status"] for r in calls), ["heard", "heard"])
        self.assertEqual(sorted(fl.topic_of(r) for r in calls), ["cdn choice", "upload retries"])
        es = self.links()
        self.assertEqual(es["ENG-12"]["part-of"], ["Uploads"])
        self.assertEqual(es["ENG-12"]["blocked-by"], ["ENG-9"])
        self.assertEqual(es["ENG-12"]["owner"], ["Dev One"])
        self.assertEqual(es["ENG-12"]["ships-in"], ["Cycle 4"])
        self.assertEqual(es["upload retries"]["decided-by"], ["call:" + MEETING])
        self.assertEqual(fl.entities()["call:" + MEETING]["type"], "call")
        self.assertIn("ENG-12", fl.children("Uploads"))
        # a second sync reads from the cursors: nothing new
        n = len(fl.items())
        fl.sync()
        self.assertEqual(len(fl.items()), n)

    def test_heard_rows_teach_as_not_decided_and_get_no_cards(self):
        fl.sync()
        fl.make_cards()
        heard = {r["lid"] for r in fl.items() if r["status"] == "heard"}
        self.assertTrue(heard)
        self.assertFalse([c for c in fl._cards() if c["lid"] in heard and not c.get("retired")])
        out = []

        class W:
            def write(self, s):
                out.append(s)
        les.lesson("upload retries", out=W())
        text = "".join(out)
        self.assertIn("heard, not decided yet", text)
        self.assertIn('"let\'s cap upload retries at 3" [watch:whosaid:calls:', text)

    def test_links_and_results_come_back_after_a_rebuild(self):
        fl.sync()
        fl.make_cards()
        lid = [r["lid"] for r in fl.items() if r["source"].get("itemRef") == "ENG-12"][0]
        fl.mark_seen([lid])
        c = [c for c in fl.due() if c["lid"] == lid and c["format"] == "source"][0]
        fl.review(c["cid"], c["answer"])
        before = {"links": self.links(), "proof": fl.rebuild_proof(),
                  "seen": sorted(fl.seen_lids()), "reviews": [(r["lid"], r["ckey"]) for r in fl.reviews()]}
        self.assertTrue(before["links"]["ENG-12"])
        # wipe every derived file, as if LEARN state were lost; a rebuild re-reads the sources
        for f in fl.DERIVED_FILES:
            p = os.path.join(self.state, "learn", f)
            if os.path.exists(p):
                os.remove(p)
        self.assertEqual(fl.entities(), {})
        res = fl.rebuild()
        self.assertEqual(res["counts"]["orphanedReviews"], 0)
        after = {"links": self.links(), "proof": fl.rebuild_proof(),
                 "seen": sorted(fl.seen_lids()), "reviews": [(r["lid"], r["ckey"]) for r in fl.reviews()]}
        self.assertEqual(after, before)
        p = subprocess.run([FLEARN, "proof", "--check"], env=dict(os.environ), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "learn", "inputs.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "learn", "journal.jsonl")))

    def test_curate_is_not_kept_through_a_rebuild(self):
        fl.sync()
        lid = fl.items()[0]["lid"]
        fl.curate(lid, part_of="Elsewhere")
        self.assertIn("Elsewhere", fl.entities())
        fl.rebuild()
        self.assertNotIn("Elsewhere", fl.entities())  # derived state only: the sources win

    def test_failing_source_aborts_with_state_intact(self):
        fl.sync()
        fl.make_cards()
        d = os.path.join(self.state, "learn")

        def snap():
            out = {}
            for f in sorted(os.listdir(d)):
                if os.path.isfile(os.path.join(d, f)):
                    with open(os.path.join(d, f), "rb") as fh:
                        out[f] = fh.read()
            return out
        before = snap()
        S.OVERRIDES["linear"] = linear_class(FakeHttp(fail=500))
        with self.assertRaisesRegex(S.SourceFailed, "tracker.*HTTP 500"):
            fl.rebuild()
        self.assertEqual(snap(), before)
        self.assertFalse(os.path.exists(os.path.join(d, "rebuild-prev")))

    def test_guard(self):
        fl.sync()
        lid = fl.items()[0]["lid"]
        os.environ["FLEET_ROLE"] = "worker"
        with self.assertRaises(fl.NotHuman):
            fl.mark_seen([lid])
        with self.assertRaises(fl.NotHuman):
            fl.rebuild()
        with self.assertRaises(fl.NotHuman):
            fl.quiz(inp=iter(()), out=io.StringIO())
        os.environ["FLEET_ROLE"] = "learn"
        fl.rebuild()  # LEARN may rebuild
        with self.assertRaises(fl.NotHuman):
            fl.mark_seen([lid])  # but never marks a lesson seen
        os.environ.pop("FLEET_ROLE")
        self.assertEqual([r["lid"] for r in fl.mark_seen([lid])], [lid])


# ------------------------------------------------------------------ ported from the raw arm (Q704 69a)

def many_meetings(tmp, n, per=1):
    """A workspace of n meeting folders, each with its own decisions.json of `per` distinct decisions."""
    ws = os.path.join(tmp, "many")
    for i in range(n):
        m = "2026-03-%02d-%02d00" % (1 + i // 24, i % 24)
        os.makedirs(os.path.join(ws, m))
        with open(os.path.join(ws, m, "decisions.json"), "w") as f:
            json.dump({"decisions": [{"id": "d%d" % k, "topicLabel": "topic %d" % i, "t": 60.0 * k,
                                      "status": "agreed", "text": "decision %d of meeting %d" % (k, i)}
                                     for k in range(per)]}, f)
    return ws


class TestPorted(Tmp):
    def write(self, cfg, name="sources.json"):
        with open(os.path.join(self.state, name), "w") as f:
            json.dump(cfg, f)

    def test_whosaid_always_runs_the_local_engine(self):
        """The fake refuses any other engine (exit 5), so dropping --engine local from the adapter turns this red."""
        p = subprocess.run([FAKE_WHOSAID, "decisions", workspace(self.tmp), MEETING, "--json"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 5)
        p = subprocess.run([FAKE_WHOSAID, "decisions", os.path.join(self.tmp, "meetings"), MEETING, "--json",
                            "--engine", "claude"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 5)
        src = WhosaidSource({"name": "calls", "workspace": os.path.join(self.tmp, "meetings"),
                             "bin": FAKE_WHOSAID}, {}, {})
        evs, _cur, _ = src.poll(None)
        self.assertEqual(len(evs), 2)

    def test_rebuild_drains_a_workspace_bigger_than_one_poll(self):
        """60 meetings, 50 per poll: a rebuild (and a fresh sync) brings all 60, not 50 now and 10 next sync."""
        ws = many_meetings(self.tmp, 60)
        self.write({"sources": [{"name": "calls", "kind": "whosaid", "workspace": ws, "bin": FAKE_WHOSAID}]})
        res = fl.rebuild()
        calls = [r for r in fl.items() if r["source"].get("sourceKind") == "whosaid"]
        self.assertEqual(len(calls), 60)
        self.assertEqual(res["sources"]["polled"][0]["events"], 60)
        self.assertGreaterEqual(res["sources"]["polled"][0]["polls"], 2)
        self.assertEqual(res["sources"]["drainCapped"], [])
        fl.sync()  # nothing left behind for the next sync
        self.assertEqual(len([r for r in fl.items() if r["source"].get("sourceKind") == "whosaid"]), 60)

    def test_event_cap_never_loses_a_decision(self):
        """maxEventsPerPoll stops a poll after a whole meeting; the cursor stays there and the drain reads on."""
        ws = many_meetings(self.tmp, 26, per=3)
        self.write({"sources": [{"name": "calls", "kind": "whosaid", "workspace": ws, "bin": FAKE_WHOSAID,
                                 "maxMeetingsPerPoll": 20, "maxEventsPerPoll": 10}]})
        res = S.poll_all(full=True)
        self.assertEqual(len(res["events"]), 78)
        self.assertEqual(len({e["eid"] for e in res["events"]}), 78)
        self.assertEqual(res["cursors"]["calls"], sorted(os.listdir(ws))[-1])

    def test_rebuild_fails_closed_at_the_drain_bound(self):
        ws = many_meetings(self.tmp, 6)
        self.write({"sources": [{"name": "calls", "kind": "whosaid", "workspace": ws, "bin": FAKE_WHOSAID,
                                 "maxMeetingsPerPoll": 1}]})
        old = S.DRAIN_ROUNDS
        S.DRAIN_ROUNDS = 3
        try:
            with self.assertRaisesRegex(S.SourceFailed, "still more after 3 polls"):
                S.poll_all(full=True)
            res = S.poll_all()  # a sync reports it and resumes from the cursor next time
            self.assertEqual((len(res["events"]), res["drainCapped"]), (3, ["calls"]))
            res2 = S.poll_all(cursors=res["cursors"])
            self.assertEqual(len(res2["events"]), 3)
        finally:
            S.DRAIN_ROUNDS = old

    def test_watch_reads_sources_json_when_it_has_no_watch_json(self):
        import fleet_watch as FW
        self.assertEqual(FW._config_path(self.state), C.state_paths(self.state)["config"])  # neither file yet
        self.write({"sources": [{"name": "calls", "kind": "whosaid", "workspace": workspace(self.tmp)}]})
        self.assertEqual(FW._config_path(self.state), os.path.join(self.state, "sources.json"))
        own = C.state_paths(self.state)["config"]
        os.makedirs(os.path.dirname(own), exist_ok=True)
        with open(own, "w") as f:
            json.dump({"sources": []}, f)
        self.assertEqual(FW._config_path(self.state), own)  # WATCH's own file wins

    def test_learn_events_go_through_watch_normalize_secret_and_drop(self):
        ws = workspace(self.tmp)
        self.write({"sources": [{"name": "calls", "kind": "whosaid", "workspace": ws, "bin": FAKE_WHOSAID}],
                    "filters": [{"drop": {"type": "decision.proposed"}}]})
        res = S.poll_all()
        self.assertEqual([e["type"] for e in res["events"]], ["decision.agreed"])
        self.assertEqual(res["dropped"], {"drop type=decision.proposed": 1})
        self.assertEqual(res["events"][0]["origin"], "outside")  # stamped by WATCH's normalize

        class Odd(C.Source):
            kind = "whosaid"

            def poll(self, cursor):
                good = C.Event(eid="x:1", source="elsewhere", sourceKind="github", at="2026-01-05T09:00:00Z",
                               type="decision.agreed", ref="r1", quote="plain words")
                bad = C.Event(eid="x:2", source="calls", sourceKind="whosaid", at="2026-01-05T09:00:00Z",
                              type="decision.agreed", ref="r2", quote="key ghp_abcdefghijklmnopqrstuvwxyz0123")
                return [good, bad], "c1", None
        S.OVERRIDES["whosaid"] = Odd
        res = S.poll_all()
        self.assertEqual(res["secretDropped"], 1)
        self.assertEqual([(e["source"], e["sourceKind"]) for e in res["events"]], [("calls", "whosaid")])


# ------------------------------------------------------------------ drain bounds: wall clock and WATCH's rate gate

class FakeClock:
    """One clock for the drain deadline (S._clock), the rate gate (S._wall) and its waits (S._sleep)."""

    def __init__(self):
        self.t, self.sleeps = 1_000_000.0, []

    def __call__(self):
        return self.t

    def sleep(self, sec):
        self.sleeps.append(round(sec, 3))
        self.t += sec


class FakeResp:
    headers = None

    def read(self):
        return b"[]"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Stands in for urllib.request.urlopen; records every request that would have gone out."""

    def __init__(self):
        self.sent = []

    def __call__(self, req, *a, **kw):
        self.sent.append(getattr(req, "full_url", req))
        return FakeResp()


def http_source(requests_per_poll, polls, clock=None, sec_per_poll=0):
    """A source of `polls` pages; each poll makes n urlopen calls (as every HTTP adapter does), moves the fake clock
    by sec_per_poll and returns one new event. polls=None never runs dry."""
    class Http(C.Source):
        kind = "command"
        calls = 0

        def poll(self, cursor):
            i = int(cursor or 0)
            for k in range(requests_per_poll):
                with urllib.request.urlopen("https://acme.example/api/%d/%d" % (i, k), timeout=20) as r:
                    r.read()
            Http.calls += 1
            if clock is not None:
                clock.t += sec_per_poll
            self.more = polls is None or i + 1 < polls
            ev = C.Event(eid="api:%d" % i, source="api", sourceKind="github", at="2026-01-05T09:00:00Z",
                         type="decision.agreed", ref="r%d" % i, quote="decision %d" % i)
            return [ev], str(i + 1), None
    return Http


class TestDrainBounds(Tmp):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self._saved = (S._clock, S._wall, S._sleep, urllib.request.urlopen)
        S._clock = S._wall = self.clock
        S._sleep = self.clock.sleep
        self.net = FakeNet()
        urllib.request.urlopen = self.net

    def tearDown(self):
        S._clock, S._wall, S._sleep, urllib.request.urlopen = self._saved
        super().tearDown()

    def write(self, per_min=None, **src):
        s = {"name": "api", "kind": "command", "argv": ["true"]}
        s.update(src)
        cfg = {"me": {"names": ["Human_Example"]}, "sources": [s]}
        if per_min is not None:
            cfg["defaults"] = {"ratePerMin": per_min}
        with open(os.path.join(self.state, "sources.json"), "w") as f:
            json.dump(cfg, f)
        return cfg

    def snap(self):
        d = os.path.join(self.state, "learn")
        out = {}
        for f in sorted(os.listdir(d)):
            if os.path.isfile(os.path.join(d, f)):
                with open(os.path.join(d, f), "rb") as fh:
                    out[f] = fh.read()
        return out

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        so, se = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = fl.main(list(argv))
        finally:
            sys.stdout, sys.stderr = so, se
        return rc, out.getvalue(), err.getvalue()

    def test_rebuild_stops_at_the_time_bound_fails_closed_and_exits_nonzero(self):
        """An endless source, 40 s a poll: the drain stops at 300 s (8 polls), the rebuild changes nothing, rc 1."""
        fl.sync()
        fl.make_cards()
        before = self.snap()
        self.write()
        S.OVERRIDES["command"] = http_source(0, None, self.clock, sec_per_poll=40)
        rc, out, err = self.run_cli("rebuild")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL source api failed: stopped at the 300s bound after 8 polls (8 event(s) read, partial); "
                      "nothing changed\n", err)
        self.assertEqual(self.snap(), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "learn", "rebuild-prev")))
        self.assertLess(S._clock() - 1_000_000.0, 300 + 40 + 1)  # never ran past the bound by more than one poll

    def test_sync_keeps_the_partial_result_and_names_the_bound(self):
        self.write()
        S.OVERRIDES["command"] = http_source(0, None, self.clock, sec_per_poll=40)
        rc, out, err = self.run_cli("sync")
        self.assertEqual(rc, 0)
        self.assertIn("sources: PARTIAL api: stopped at the 300s bound after 8 polls; the next sync reads on", out)
        self.assertEqual(len([r for r in fl.items() if r["source"].get("source") == "api"]), 8)
        with open(os.path.join(self.state, "learn", "cursors.json")) as f:
            cur = json.load(f)
        self.assertEqual(cur["sources"]["api"], "8")  # the next sync reads on from here

    def test_rate_gate_spaces_the_calls_the_way_watch_does(self):
        """The same source and ratePerMin through WATCH's Engine and through LEARN's drain: the same waits."""
        import datetime
        import fleet_watch as W
        cfg = self.write(per_min=20)  # 20 a minute: 20 at once, then one every 3 s (under RATE_MAX_WAIT_SEC)

        class WClock:
            t = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
            sleeps = []

            def __call__(self):
                return self.t

            def sleep(self, sec):
                self.sleeps.append(round(sec, 3))
                self.t += datetime.timedelta(seconds=sec)
        wc = WClock()
        wstate = os.path.join(self.tmp, "wstate")
        os.makedirs(wstate)
        W.Engine(wstate, dict(cfg, filters=[{"kind": "fyi", "when": {"any": True}}]), now=wc,
                 sources={"command": http_source(25, 1)}, sleep=wc.sleep).run_once(force=True)
        self.assertEqual(len(self.net.sent), 25)
        self.assertEqual(wc.sleeps, [3.0] * 5)
        self.net.sent.clear()
        S.OVERRIDES["command"] = http_source(25, 1)
        res = S.poll_all(full=True)
        self.assertEqual(len(self.net.sent), 25)
        self.assertEqual(self.clock.sleeps, wc.sleeps)
        self.assertEqual(res["sources"][0]["rate"], {"calls": 25, "waitedSec": 15.0, "perMin": 20.0})

    def test_rate_gate_spans_the_whole_drain_not_each_poll(self):
        """Three polls of 10 requests at 20 a minute: one bucket per source per drain, so polls 3 waits 3 s a call."""
        self.write(per_min=20)
        S.OVERRIDES["command"] = http_source(10, 3)
        res = S.poll_all(full=True)
        self.assertEqual((len(res["events"]), len(self.net.sent)), (3, 30))
        self.assertEqual(self.clock.sleeps, [3.0] * 10)

    def test_rate_deferral_inside_the_bound_waits_and_polls_the_same_cursor_again(self):
        """1 a minute: each later poll defers 60 s (nothing sent), the drain waits it out and reads every page."""
        self.write(per_min=1)
        S.OVERRIDES["command"] = http_source(1, 3)
        res = S.poll_all(full=True)
        self.assertEqual(len(res["events"]), 3)
        self.assertEqual(len(self.net.sent), 3)  # a deferred poll sends nothing
        self.assertEqual(self.clock.sleeps, [60.0, 60.0])
        self.assertEqual(res["cursors"]["api"], "3")

    def test_rate_deferral_past_the_bound_stops_and_fails_the_rebuild_closed(self):
        fl.sync()
        before = self.snap()
        self.write(per_min=0.1)  # a bucket of 0.1 token: even the first request is 540 s away, past 300 s
        S.OVERRIDES["command"] = http_source(1, 3)
        rc, out, err = self.run_cli("rebuild")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL source api failed: rate limit: next call in 540s, past the 300s bound, after 1 polls "
                      "(0 event(s) read, partial); nothing changed\n", err)
        self.assertEqual(len(self.net.sent), 0)  # the gate deferred before anything went out
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.snap(), before)

    def test_310_of_310_with_engine_local_through_the_cli(self):
        """60 small meetings plus one of 250 decisions, real bounds and defaults, the fake whosaid that refuses any
        engine but local: the rebuild brings all 310, and the gate sends no wait (whosaid is a subprocess)."""
        S._clock, S._wall, S._sleep = self._saved[:3]
        ws = many_meetings(self.tmp, 60)
        big = os.path.join(ws, "2026-04-01-0900")
        os.makedirs(big)
        with open(os.path.join(big, "decisions.json"), "w") as f:
            json.dump({"decisions": [{"id": "d%d" % k, "topicLabel": "big", "t": 1.0 * k, "status": "agreed",
                                      "text": "big decision %d" % k} for k in range(250)]}, f)
        with open(os.path.join(self.state, "sources.json"), "w") as f:
            json.dump({"sources": [{"name": "calls", "kind": "whosaid", "workspace": ws, "bin": FAKE_WHOSAID}]}, f)
        env = dict(os.environ)
        for k in ("TMUX", "TMUX_PANE", "FLEET_ROLE"):
            env.pop(k, None)
        p = subprocess.run([FLEARN, "rebuild", "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                           universal_newlines=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        polled = res["sources"]["polled"][0]
        self.assertEqual(polled["events"], 310)
        self.assertEqual(polled["rate"]["waitedSec"], 0)
        self.assertEqual(res["sources"]["drainCapped"], [])
        self.assertEqual(len([r for r in fl.items() if r["source"].get("sourceKind") == "whosaid"]), 310)


class TestReviewGuard(Tmp):
    def test_review_alone_refuses_under_any_role(self):
        """Only the guard inside review() stands between a lane and reviews.jsonl: removing it turns this red."""
        with open(os.path.join(self.state, "sources.json"), "w") as f:
            json.dump({"sources": [{"name": "tracker", "kind": "linear", "teams": ["ENG"]}]}, f)
        S.OVERRIDES["linear"] = linear_class(FakeHttp())
        fl.sync()
        fl.make_cards()
        fl.mark_seen([r["lid"] for r in fl.items()])
        cs = [c for c in fl._cards() if not c.get("retired")]
        self.assertTrue(cs)
        rpath = os.path.join(self.state, "learn", "reviews.jsonl")
        for role in ("worker", "learn", "route"):
            os.environ["FLEET_ROLE"] = role
            with self.assertRaises(fl.NotHuman):
                fl.review(cs[0]["cid"], cs[0]["answer"], rating="good")
            self.assertFalse(os.path.exists(rpath) and os.path.getsize(rpath))
        os.environ.pop("FLEET_ROLE")
        fl.review(cs[0]["cid"], cs[0]["answer"], rating="good")  # control: the human may
        self.assertTrue(os.path.getsize(rpath))


if __name__ == "__main__":
    unittest.main()
