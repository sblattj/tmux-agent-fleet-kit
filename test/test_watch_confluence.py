"""Recorded-fixture tests for the Confluence WATCH source (lib/watch_sources/confluence.py).

Phase 6 acceptance (docs/design/four-desks.md section 7): the poll query, the cursor advance and the change mapping,
all against hand-written responses in test/fixtures/watch/confluence/. No live calls: urllib's urlopen is patched to
fail, and the source gets a transport that replays fixtures. Every positive check has a control that must fail.
Run: python3 -m unittest test/test_watch_confluence.py
"""
import base64
import datetime as dt
import json
import os
import sys
import unittest
import urllib.parse
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "confluence")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as W  # noqa: E402  (the same module the adapter imports: one Source class)

TOKEN = "tok-FIXTURE-never-in-output-9f3a"
BASE = "https://acme.example.com/wiki"


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class Replay:
    """Transport that serves fixtures in order and records every request."""

    def __init__(self, *names):
        self.queue = [fixture(n) for n in names]
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if not self.queue:
            raise AssertionError("unexpected extra request %s" % url)
        r = self.queue.pop(0)
        return r["status"], r["headers"], json.dumps(r["body"]).encode("utf-8")


def clock(s):
    return lambda: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def make(transport, now="2026-11-20T09:59:00Z", auth=None, creds=None, **extra):
    cls = W.load_source("confluence")
    src = {"name": "acme-wiki", "kind": "confluence", "base": BASE, "spaces": ["ACME"],
           "auth": auth or {"type": "bearer", "token": "env:ACME_WIKI_TOKEN"}}
    src.update(extra)
    top = {"me": {"names": ["Human Example"], "ids": {"acme-wiki": "acct-me-1"}}, "sources": [src]}
    cfg = W.source_cfg_with_me(top, src)
    return cls(cfg, creds if creds is not None else {"token": TOKEN}, {}, transport=transport, clock=clock(now))


def query(url):
    p = urllib.parse.urlparse(url)
    return p, {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}


class NoLiveCalls(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("live call attempted"))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestLoading(NoLiveCalls):
    def test_registry_loads_the_adapter(self):
        cls = W.load_source("confluence")
        self.assertEqual(cls.kind, "confluence")
        self.assertTrue(issubclass(cls, W.Source))
        self.assertIsNot(cls.poll, W.Source.poll)

    def test_control_unknown_kind_refused(self):
        with self.assertRaises(KeyError):
            W.load_source("confluence-nope")

    def test_patched_urlopen_blocks_live_calls(self):
        # control for the no-live-call guard: the default transport really goes through urlopen and fails
        cls = W.load_source("confluence")
        s = cls({"name": "x", "kind": "confluence", "base": BASE, "me": {}}, {"token": TOKEN}, {},
                clock=clock("2026-11-20T09:59:00Z"))
        with self.assertRaises(W.SourceError) as cm:
            s.poll(None)
        self.assertNotIn(TOKEN, str(cm.exception))


class TestPollQuery(NoLiveCalls):
    def test_first_poll_query(self):
        t = Replay("poll1-page1.json", "poll1-page2.json")
        make(t).poll(None)
        p, q = query(t.calls[0]["url"])
        self.assertEqual((p.scheme, p.netloc, p.path), ("https", "acme.example.com", "/wiki/rest/api/content/search"))
        # no cursor: since = clock (09:59Z), CQL bound = since - overlapSec (3600) at minute precision, UTC
        self.assertEqual(q["cql"], 'type in (page, blogpost, comment) AND space in ("ACME") AND '
                                   'lastmodified >= "2026/11/20 08:59" ORDER BY lastmodified ASC')
        self.assertEqual(q["expand"], "version,history,container,space,body.storage")
        self.assertEqual(q["limit"], "50")
        self.assertNotIn("start", q)
        self.assertEqual(t.calls[0]["timeout"], W.HTTP_TIMEOUT_SEC)
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_second_page_follows_next_on_configured_base(self):
        t = Replay("poll1-page1.json", "poll1-page2.json")
        make(t).poll(None)
        p, q = query(t.calls[1]["url"])
        self.assertEqual((p.netloc, p.path), ("acme.example.com", "/wiki/rest/api/content/search"))
        self.assertEqual(q["cursor"], "raNDoMsTRiNg")
        self.assertEqual(len(t.calls), 2)

    def test_next_link_host_ignored(self):
        t = Replay("next-other-host.json", "empty.json")
        make(t).poll(None)
        p, _ = query(t.calls[1]["url"])
        self.assertEqual(p.netloc, "acme.example.com")
        self.assertNotIn("cdn.", t.calls[1]["url"])

    def test_time_zone_shifts_the_cql_bound(self):
        t = Replay("empty.json")
        make(t, timeZone="+02:00").poll({"since": "2026-11-20T10:07:30Z", "ids": []})
        self.assertIn('lastmodified >= "2026/11/20 11:07"', query(t.calls[0]["url"])[1]["cql"])
        # control: the same cursor with the default (UTC) time zone gives a different bound
        t2 = Replay("empty.json")
        make(t2).poll({"since": "2026-11-20T10:07:30Z", "ids": []})
        self.assertIn('lastmodified >= "2026/11/20 09:07"', query(t2.calls[0]["url"])[1]["cql"])

    def test_no_spaces_no_space_clause(self):
        t = Replay("empty.json")
        make(t, spaces=[]).poll(None)
        self.assertNotIn("space in", query(t.calls[0]["url"])[1]["cql"])

    def test_basic_auth_header(self):
        t = Replay("empty.json")
        make(t, auth={"type": "basic", "user": "env:U", "token": "env:T"},
             creds={"user": "bot@acme.example.com", "token": TOKEN}).poll(None)
        h = t.calls[0]["headers"]["Authorization"]
        self.assertEqual(base64.b64decode(h.split(" ", 1)[1]).decode(), "bot@acme.example.com:" + TOKEN)
        self.assertTrue(h.startswith("Basic "))

    def test_control_no_token_no_header(self):
        t = Replay("empty.json")
        make(t, creds={}).poll(None)
        self.assertNotIn("Authorization", t.calls[0]["headers"])


class TestCursor(NoLiveCalls):
    def test_first_poll_cursor(self):
        evs, cur, nxt = make(Replay("poll1-page1.json", "poll1-page2.json")).poll(None)
        self.assertEqual(cur, {"since": "2026-11-20T10:07:30Z", "ids": ["1003:1"]})
        self.assertIsNone(nxt)
        self.assertEqual(json.loads(json.dumps(cur)), cur)  # cursors.json round trip

    def test_second_poll_advances_and_skips_seen(self):
        cur1 = {"since": "2026-11-20T10:07:30Z", "ids": ["1003:1"]}
        t = Replay("poll2.json")
        evs, cur2, _ = make(t).poll(json.loads(json.dumps(cur1)))
        self.assertIn('lastmodified >= "2026/11/20 09:07"', query(t.calls[0]["url"])[1]["cql"])
        # 1002 is before the cursor, 1003:1 was seen at the cursor instant; 1004 (same instant, new) and 2003 are new
        self.assertEqual([e.fields["contentId"] for e in evs], ["1004", "2003"])
        self.assertEqual(cur2, {"since": "2026-11-20T10:20:00Z", "ids": ["2003:1"]})

    def test_control_without_cursor_ids_the_seen_row_comes_back(self):
        evs, _, _ = make(Replay("poll2.json")).poll({"since": "2026-11-20T10:07:30Z", "ids": []})
        self.assertEqual([e.fields["contentId"] for e in evs], ["1003", "1004", "2003"])

    def test_empty_poll_keeps_cursor(self):
        cur = {"since": "2026-11-20T10:07:30Z", "ids": ["1003:1"]}
        evs, new, _ = make(Replay("empty.json")).poll(cur)
        self.assertEqual((evs, new), ([], cur))

    PENDING_NEXT = "/wiki/rest/api/content/search?limit=2&cursor=moReReSuLtS"

    def test_truncated_unordered_does_not_skip(self):
        cur = {"since": "2026-11-20T11:00:00Z", "ids": []}
        t = Replay("truncated-unordered.json")
        evs, new, _ = make(t, maxEventsPerPoll=2).poll(cur)
        self.assertEqual(len(t.calls), 1)  # stopped at maxEventsPerPoll with a next link unread
        self.assertEqual(len(evs), 2)
        # unread pages may hold changes older than 11:30: since stays, the unread link is saved
        self.assertEqual(new, {"since": "2026-11-20T11:00:00Z", "ids": [], "pending": {
            "next": self.PENDING_NEXT, "hi": "2026-11-20T11:30:00Z", "hiIds": ["1005:3"]}})
        self.assertEqual(json.loads(json.dumps(new)), new)

    def test_truncated_ordered_advances(self):
        # control for the case above: the same rows in ascending server order make the newest instant safe
        cur = {"since": "2026-11-20T11:00:00Z", "ids": []}
        evs, new, _ = make(Replay("truncated-ordered.json"), maxEventsPerPoll=2).poll(cur)
        self.assertEqual((new["since"], new["ids"]), ("2026-11-20T11:30:00Z", ["1005:3"]))
        self.assertEqual(new["pending"]["next"], self.PENDING_NEXT)

    def test_resume_from_pending_then_complete(self):
        cur = {"since": "2026-11-20T11:00:00Z", "ids": [], "pending": {
            "next": self.PENDING_NEXT, "hi": "2026-11-20T11:30:00Z", "hiIds": ["1005:3"]}}
        t = Replay("resume-page2.json")
        evs, new, _ = make(t, maxEventsPerPoll=2).poll(cur)
        p, q = query(t.calls[0]["url"])
        self.assertEqual((p.netloc, p.path, q.get("cursor")), ("acme.example.com", "/wiki/rest/api/content/search",
                                                              "moReReSuLtS"))
        self.assertNotIn("cql", q)  # resumed the saved page, did not restart the query
        self.assertEqual([e.fields["contentId"] for e in evs], ["1008"])
        # complete: since jumps to the newest change of the whole run (11:30 from the earlier poll), pending gone
        self.assertEqual(new, {"since": "2026-11-20T11:30:00Z", "ids": ["1005:3"]})

    def test_control_without_pending_the_query_restarts(self):
        t = Replay("truncated-unordered.json")
        make(t, maxEventsPerPoll=2).poll({"since": "2026-11-20T11:00:00Z", "ids": []})
        self.assertIn("cql", query(t.calls[0]["url"])[1])

    def test_stale_pending_cursor_restarts_from_since(self):
        cur = {"since": "2026-11-20T11:00:00Z", "ids": [], "pending": {
            "next": self.PENDING_NEXT, "hi": "2026-11-20T11:30:00Z", "hiIds": ["1005:3"]}}
        t = Replay("stale-cursor.json", "resume-page2.json")
        evs, new, _ = make(t).poll(cur)
        self.assertEqual(len(t.calls), 2)
        self.assertIn('lastmodified >= "2026/11/20 10:00"', query(t.calls[1]["url"])[1]["cql"])
        self.assertEqual(new, {"since": "2026-11-20T11:20:00Z", "ids": ["1008:1"]})

    def test_control_400_without_pending_raises(self):
        with self.assertRaises(W.SourceError) as cm:
            make(Replay("stale-cursor.json")).poll({"since": "2026-11-20T11:00:00Z", "ids": []})
        self.assertEqual(cm.exception.status, 400)

    def test_overlap_rows_do_not_count_toward_the_cap(self):
        # page 1 is all rows older than the cursor (the overlap re-read); with maxEventsPerPoll=2 the poll must
        # still read page 2, emit the new change and advance
        cur = {"since": "2026-11-20T11:00:00Z", "ids": []}
        t = Replay("overlap-page1.json", "overlap-page2.json")
        evs, new, _ = make(t, maxEventsPerPoll=2).poll(cur)
        self.assertEqual(len(t.calls), 2)
        self.assertEqual([e.fields["contentId"] for e in evs], ["1012"])
        self.assertEqual(new, {"since": "2026-11-20T11:05:00Z", "ids": ["1012:1"]})

    def test_max_pages_bounds_requests(self):
        # control for the case above: maxPages=1 stops after page 1 with nothing new and keeps the link
        cur = {"since": "2026-11-20T11:00:00Z", "ids": []}
        t = Replay("overlap-page1.json")
        evs, new, _ = make(t, maxEventsPerPoll=2, maxPages=1).poll(cur)
        self.assertEqual((len(t.calls), evs), (1, []))
        self.assertEqual(new["pending"]["next"], "/wiki/rest/api/content/search?limit=2&cursor=oVeRlApTwO")

    def test_health_persists_in_state(self):
        cls = W.load_source("confluence")
        state = {}
        src = {"name": "acme-wiki", "kind": "confluence", "base": BASE, "me": {"names": [], "id": None}}
        for _ in range(2):  # the engine builds a new instance per poll, restoring state from cursors.json
            s = cls(src, {"token": TOKEN}, state, transport=Replay("server-error.json"), clock=clock("2026-11-20T11:00:00Z"))
            with self.assertRaises(W.SourceError):
                s.poll(None)
        self.assertEqual(s.health()["failStreak"], 2)
        self.assertNotIn(TOKEN, json.dumps(state))
        s = cls(src, {"token": TOKEN}, state, transport=Replay("empty.json"), clock=clock("2026-11-20T11:00:00Z"))
        s.poll(None)
        self.assertEqual((s.health()["failStreak"], state["failStreak"], state["lastOk"]), (0, 0, "2026-11-20T11:00:00Z"))

    def test_failure_raises_with_retry_after(self):
        s = make(Replay("rate-limited.json"))
        with self.assertRaises(W.SourceError) as cm:
            s.poll({"since": "2026-11-20T10:07:30Z", "ids": []})
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 120))
        self.assertNotIn(TOKEN, str(cm.exception))
        h = s.health()
        self.assertEqual((h["failStreak"], h["rateRemaining"]), (1, 0))
        self.assertIn("429", h["lastError"])

    def test_control_503_has_no_retry_after_and_ok_resets_streak(self):
        s = make(Replay("server-error.json", "empty.json"))
        with self.assertRaises(W.SourceError) as cm:
            s.poll(None)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (503, None))
        self.assertEqual(s.health()["failStreak"], 1)
        s.poll(None)
        h = s.health()
        self.assertEqual((h["failStreak"], h["lastError"], h["rateRemaining"]), (0, None, 95))


class TestMapping(NoLiveCalls):
    def setUp(self):
        super().setUp()
        self.evs, _, _ = make(Replay("poll1-page1.json", "poll1-page2.json")).poll(None)
        self.by = {e.fields["contentId"]: e for e in self.evs}

    def test_every_event_valid_and_unrouted(self):
        self.assertEqual(sorted(self.by), ["1001", "1002", "1003", "2001"])  # 2002 has version.when null: skipped
        for e in self.evs:
            self.assertEqual(W.validate_event(e.to_dict()), [], e.eid)
            self.assertEqual((e.kind, e.to, e.score, e.origin), (W.KIND_FYI, W.DECIDE, W.SCORE_FYI, "outside"))
            self.assertNotIn(TOKEN, e.to_json())
        bad = self.evs[0].to_dict()
        bad["to"] = "ROUTE"
        self.assertNotEqual(W.validate_event(bad), [])  # control: the validator does fail a wrong row

    def test_comment_mapping_full(self):
        e = self.by["2001"]
        self.assertEqual(e.to_dict(), {
            "eid": "confluence:acme.example.com:ACME:1002:2001:2026-11-20T10:05:00Z:page.commented:v1",
            "source": "acme-wiki", "sourceKind": "confluence", "at": "2026-11-20T10:05:00Z",
            "type": "page.commented", "ref": "ACME:1002", "url": BASE + "/spaces/ACME/pages/2001",
            "actor": "Alice Example", "me": False, "title": "PROJ-1 rollout plan",
            "quote": 'Can confirm the "staging" date?',
            "fields": {"contentId": "2001", "contentType": "comment", "version": 1, "space": "ACME",
                       "actorId": "acct-alice-2", "pageId": "1002", "mentionedIds": ["acct-me-1"]},
            "tags": ["my-page", "mention"], "kind": "fyi", "to": "DECIDE", "origin": "outside", "score": 1,
            "secretDropped": False, "count": 1})

    def test_created_and_edited(self):
        self.assertEqual(self.by["1001"].type, "page.created")
        self.assertEqual(self.by["1002"].type, "page.edited")
        self.assertEqual(self.by["1002"].quote, "Added the staging step")
        self.assertEqual(self.by["1002"].tags, ["my-page"])  # Bob edited a page I created
        self.assertEqual(self.by["1001"].tags, [])  # control: Alice's own new page carries no tag
        self.assertEqual(self.by["1003"].fields["contentType"], "blogpost")
        self.assertEqual(self.by["1003"].type, "page.created")

    def test_me(self):
        self.assertTrue(self.by["1003"].me)
        self.assertFalse(self.by["1001"].me)  # control

    def test_control_comment_without_mention(self):
        evs, _, _ = make(Replay("poll2.json")).poll({"since": "2026-11-20T10:07:30Z", "ids": ["1003:1"]})
        c = [e for e in evs if e.fields["contentId"] == "2003"][0]
        self.assertEqual((c.type, c.ref, c.tags, c.fields["mentionedIds"]), ("page.commented", "ACME:1001", [], []))

    def test_eid_stable_across_polls(self):
        # the same version re-read in a later poll gets the same eid, so seen.db dedupes the overlap window
        again, _, _ = make(Replay("poll2.json")).poll({"since": "2026-11-20T09:00:00Z", "ids": []})
        self.assertEqual({e.eid for e in again} & {e.eid for e in self.evs},
                         {self.by["1002"].eid, self.by["1003"].eid})
        self.assertEqual(len({e.eid for e in self.evs}), len(self.evs))  # control: distinct changes, distinct eids

    def test_quote_capped(self):
        evs, _, _ = make(Replay("poll1-page1.json", "poll1-page2.json"), quoteChars=10).poll(None)
        q = [e for e in evs if e.fields["contentId"] == "2001"][0].quote
        self.assertEqual(q, "Can con...")
        self.assertEqual(W.validate_event([e for e in evs if e.fields["contentId"] == "2001"][0].to_dict(),
                                          quote_chars=10), [])


if __name__ == "__main__":
    unittest.main()
