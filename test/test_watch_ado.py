"""Azure DevOps WATCH source: recorded-fixture tests of the poll query, the cursor advance and the change mapping
into the contract's Event (four-desks.md phase 6). No network: every HTTP exchange is replayed from
test/fixtures/watch/ado/ and the replay fails on any request the fixtures do not expect.
Every positive check has a control that must fail.
Run: python3 -m unittest test/test_watch_ado.py
"""
import base64
import copy
import datetime
import importlib
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "ado")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

W = importlib.import_module("fleet_watch_contract")
ADO = importlib.import_module("watch_sources.ado")
WH = importlib.import_module("watch_sources.webhook")

PAT = "pat-value-never-in-output-0123456789"
HOOK_SECRET = "hook-secret-never-in-output-9876"
HOOK_USER = "ado-hooks"
ME_ID = "11111111-1111-4111-8111-111111111111"
CLOCK = datetime.datetime(2026, 11, 20, 10, 0, 0, tzinfo=datetime.timezone.utc)
WATCH_JSON = {
    "me": {"names": ["Human_Example"], "ids": {"boards": ME_ID}},
    "sources": [{"name": "boards", "kind": "ado", "org": "ACME", "project": "PROJ",
                 "base": "https://acme.example.com/tfs/ACME", "backfillSec": 3600,
                 "auth": {"type": "pat", "token": "env:ADO_PAT"}},
                {"name": "boards-hook", "kind": "webhook", "for": "boards", "signature": "basic", "port": 0,
                 "auth": {"type": "basic", "user": "env:ADO_HOOK_USER", "secret": "env:ADO_HOOK_SECRET"}}],
}
REFS = {"env:ADO_HOOK_USER": HOOK_USER, "env:ADO_HOOK_SECRET": HOOK_SECRET}


def resolver(ref):
    if ref not in REFS:
        raise ValueError("unknown ref")
    return REFS[ref]


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode(),
            "Content-Type": "application/json"}


class NoCreds(dict):
    """Empty creds that fail the test if anything reads them."""

    def _boom(self, *a, **k):
        raise AssertionError("accept_webhook read creds")

    def __bool__(self):  # the contract does `creds or {}`; stay the object under test
        return True

    get = __getitem__ = __contains__ = keys = items = values = _boom


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class Replay:
    """Transport that answers from fixture exchanges in order and checks each request against the recorded one."""

    def __init__(self, *exchanges):
        self.queue = [fixture(e) if isinstance(e, str) else e for e in exchanges]
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(body.decode("utf-8")) if body else None, "timeout": timeout})
        if not self.queue:
            raise AssertionError("unexpected request %s %s" % (method, url))
        ex = self.queue.pop(0)
        req = ex.get("request") or {}
        for k in ("method", "url", "body"):
            got = {"method": method, "url": url, "body": self.calls[-1]["body"]}[k]
            if k in req and req[k] != got:
                raise AssertionError("request %s differs:\n want %r\n got  %r" % (k, req[k], got))
        raw = ex["body_text"].encode("utf-8") if "body_text" in ex else json.dumps(ex.get("body")).encode("utf-8")
        return ex.get("status", 200), {k.lower(): v for k, v in (ex.get("headers") or {}).items()}, raw

    def done(self):
        return not self.queue


def make_source(state=None, transport=None, cfg_extra=None, creds=None):
    src = dict(WATCH_JSON["sources"][0])
    src.update(cfg_extra or {})
    cfg = W.source_cfg_with_me(WATCH_JSON, src)
    cls = W.load_source("ado")
    return cls(cfg, creds if creds is not None else {"token": PAT},
               state if state is not None else {}, transport=transport, clock=lambda: CLOCK)


def run_polls():
    """Polls 1..3 against the recorded fixtures. Returns (source, replay, [(events, cursor, next), ...])."""
    rp = Replay("poll1-wiql.json", "poll1-batch.json", "poll2-wiql.json", "poll2-batch.json", "poll3-wiql.json")
    s = make_source(transport=rp)
    out, cur = [], None
    for _ in range(3):
        ev, cur, nxt = s.poll(cur)
        cur = json.loads(json.dumps(cur))  # the engine persists it as JSON in cursors.json
        out.append((ev, cur, nxt))
    return s, rp, out


class PollQuery(unittest.TestCase):
    def test_first_poll_sends_the_recorded_wiql_then_a_batch_get(self):
        s, rp, _ = run_polls()
        self.assertTrue(rp.done(), "every recorded exchange was used")
        self.assertEqual([c["url"].split("/_apis/wit/")[1].split("?")[0] for c in rp.calls],
                         ["wiql", "workitemsbatch", "wiql", "workitemsbatch", "wiql"])
        q = rp.calls[0]["body"]["query"]
        self.assertIn("[System.ChangedDate] >= '2026-11-20T09:00:00Z'", q)  # explicit UTC, backfillSec 3600
        self.assertIn("timePrecision=true", rp.calls[0]["url"])  # else WIQL drops the time of day
        self.assertNotIn("@today", q)
        self.assertEqual(rp.calls[1]["body"]["ids"], [1, 2, 3])  # step 2 asks for exactly the ids WIQL returned
        self.assertTrue(all(c["method"] == "POST" and c["timeout"] == W.HTTP_TIMEOUT_SEC for c in rp.calls))

    def test_pat_goes_as_basic_base64_colon_pat(self):
        _, rp, _ = run_polls()
        want = "Basic " + base64.b64encode((":" + PAT).encode()).decode()
        self.assertEqual(rp.calls[0]["headers"]["Authorization"], want)
        self.assertNotEqual(rp.calls[0]["headers"]["Authorization"], "Basic " + PAT)  # control: raw PAT is wrong

    def test_replay_refuses_a_different_query(self):
        # control for the checker itself: a WIQL with a local (zone-less) date must not pass the recorded request
        ex = fixture("poll1-wiql.json")
        rp = Replay(ex)
        bad = json.dumps({"query": ex["request"]["body"]["query"].replace("09:00:00Z", "09:00:00")}).encode()
        with self.assertRaises(AssertionError):
            rp("POST", ex["request"]["url"], {}, bad, 20)

    def test_zoned_cursor_becomes_a_utc_literal(self):
        s = make_source(transport=Replay())
        q = s.build_wiql(ADO.parse_iso("2026-11-20T10:00:00+01:00"))
        self.assertIn("'2026-11-20T09:00:00Z'", q)
        self.assertNotIn("'2026-11-20T10:00:00Z'", q)  # control: the local wall time is not what goes out

    def test_top_is_max_events_per_poll(self):
        s = make_source(transport=Replay(), cfg_extra={"maxEventsPerPoll": 7})
        calls = []

        def t(m, u, h, b, to):
            calls.append(u)
            return 200, {"content-type": "application/json"}, b'{"workItems": []}'
        s.transport = t
        s.poll(None)
        self.assertIn("$top=7&", calls[0])
        self.assertNotIn("$top=200", calls[0])

    def test_extra_query_clause_is_anded_in(self):
        s = make_source(transport=Replay(), cfg_extra={"query": "[System.AssignedTo] = @me"})
        q = s.build_wiql(CLOCK)
        self.assertIn(" AND ([System.AssignedTo] = @me) ORDER BY", q)
        self.assertNotIn("@me", make_source(transport=Replay()).build_wiql(CLOCK))


class CursorAdvance(unittest.TestCase):
    def test_cursor_moves_to_newest_changed_date_and_remembers_ids_there(self):
        _, _, out = run_polls()
        self.assertEqual(out[0][1], {"since": "2026-11-20T09:30:00.5Z", "seen": {"3": 3}})
        self.assertEqual(out[1][1], {"since": "2026-11-20T09:50:00Z", "seen": {"1": 2}})
        self.assertEqual(out[2][1], out[1][1])  # nothing changed, cursor stays
        self.assertEqual(out[2][0], [])

    def test_second_poll_queries_from_the_cursor_second(self):
        _, rp, _ = run_polls()
        self.assertIn(">= '2026-11-20T09:30:00Z'", rp.calls[2]["body"]["query"])
        self.assertIn(">= '2026-11-20T09:50:00Z'", rp.calls[4]["body"]["query"])

    def test_item_at_the_cursor_instant_is_not_reported_twice(self):
        _, _, out = run_polls()
        self.assertIn("PROJ-3", [e.ref for e in out[0][0]])
        self.assertNotIn("PROJ-3", [e.ref for e in out[1][0]])  # same rev at the cursor instant: skipped

    def test_new_rev_at_the_cursor_instant_is_reported(self):
        # control for the skip: same instant, higher rev, must come through
        s = make_source(transport=Replay())
        item = fixture("poll2-batch.json")["body"]["value"][0]
        bumped = copy.deepcopy(item)
        bumped["rev"] = 4
        fresh = s._fresh([item, bumped], ADO.parse_iso("2026-11-20T09:30:00.5Z"), {"3": 3})
        self.assertEqual([w["rev"] for w in fresh], [4])

    def test_full_page_of_seen_items_pages_past_by_id(self):
        top = 2
        page = {"status": 200, "headers": {"content-type": "application/json"},
                "body": {"workItems": [{"id": 1}, {"id": 2}]}}
        same = {"status": 200, "headers": {"content-type": "application/json"}, "body": {"count": 2, "value": [
            {"id": 1, "rev": 1, "fields": {"System.ChangedDate": "2026-11-20T09:00:00Z", "System.State": "New"}},
            {"id": 2, "rev": 1, "fields": {"System.ChangedDate": "2026-11-20T09:00:00Z", "System.State": "New"}}]}}
        nxt = {"status": 200, "headers": {"content-type": "application/json"}, "body": {"workItems": [{"id": 9}]}}
        nine = {"status": 200, "headers": {"content-type": "application/json"}, "body": {"count": 1, "value": [
            {"id": 9, "rev": 1, "fields": {"System.ChangedDate": "2026-11-20T09:00:00Z", "System.State": "New",
                                           "System.Title": "t"}}]}}
        rp = Replay(page, same, nxt, nine)
        s = make_source(transport=rp, cfg_extra={"maxEventsPerPoll": top})
        ev, cur, _ = s.poll({"since": "2026-11-20T09:00:00Z", "seen": {"1": 1, "2": 1}})
        self.assertIn("AND [System.Id] > 2", rp.calls[2]["body"]["query"])
        self.assertNotIn("[System.Id] >", rp.calls[0]["body"]["query"])  # control: the first query has no id clause
        self.assertEqual([e.ref for e in ev], ["PROJ-9"])
        self.assertEqual(cur["seen"], {"1": 1, "2": 1, "9": 1})

    def test_max_events_cap_holds_the_cursor_at_the_last_mapped_item(self):
        rp = Replay("poll1-wiql.json", "poll1-batch.json")
        rp.queue[0]["request"]["url"] = rp.queue[0]["request"]["url"].replace("$top=200", "$top=2")
        s = make_source(transport=rp, cfg_extra={"maxEventsPerPoll": 2})
        ev, cur, _ = s.poll(None)
        self.assertEqual(len(ev), 2)
        self.assertEqual(cur["since"], "2026-11-20T09:20:00Z")  # PROJ-3 is left for the next poll
        self.assertNotEqual(cur["since"], "2026-11-20T09:30:00.5Z")


class ChangeMapping(unittest.TestCase):
    def setUp(self):
        _, _, self.out = run_polls()
        self.p1 = {e.ref: e for e in self.out[0][0]}
        self.p2 = [(e.ref, e.type) for e in self.out[1][0]]
        self.p2e = {(e.ref, e.type): e for e in self.out[1][0]}

    def test_every_event_is_valid_under_the_contract(self):
        for ev, _, _ in self.out:
            for e in ev:
                self.assertIsInstance(e, W.Event)
                self.assertEqual(W.validate_event(e.to_dict()), [], e.to_dict())
        bad = self.out[0][0][0].to_dict()
        bad["ref"] = ""
        self.assertNotEqual(W.validate_event(bad), [])  # control

    def test_first_poll_types_and_tags(self):
        self.assertEqual(sorted(self.p1), ["PROJ-1", "PROJ-2", "PROJ-3"])
        e1 = self.p1["PROJ-1"]
        self.assertEqual((e1.type, e1.sourceKind, e1.source, e1.actor, e1.me), (
            "workitem.created", "ado", "boards", "Alice_Example", False))
        self.assertEqual(e1.tags, ["assigned-to-me"])
        self.assertEqual(e1.url, "https://acme.example.com/tfs/ACME/PROJ/_workitems/edit/1")
        self.assertEqual(e1.at, "2026-11-20T09:10:00Z")
        self.assertEqual(self.p1["PROJ-2"].type, "workitem.updated")  # first sight of an old item
        self.assertEqual(self.p1["PROJ-2"].tags, [])  # control: not assigned to me, no tag
        e3 = self.p1["PROJ-3"]
        self.assertEqual(e3.tags, ["mention"])
        self.assertTrue(e3.quote.startswith("@Human_Example can you pick the retry limit?"))
        self.assertNotIn("<", e3.quote)

    def test_second_poll_transition_assignment_and_comment(self):
        self.assertEqual(self.p2, [("PROJ-2", "workitem.transition"), ("PROJ-1", "workitem.assigned"),
                                   ("PROJ-1", "workitem.comment")])
        t = self.p2e[("PROJ-2", "workitem.transition")]
        self.assertEqual((t.fields["from"], t.fields["to"], t.quote), ("Active", "Resolved", "State: Active -> Resolved"))
        self.assertEqual(W.score_for(W.KIND_FYI, t.type), W.SCORE_STATE_CHANGE)
        self.assertEqual(W.score_for(W.KIND_FYI, "workitem.comment"), W.SCORE_FYI)  # control
        a = self.p2e[("PROJ-1", "workitem.assigned")]
        self.assertTrue(a.me)  # Human_Example made this change (filters drop it as "my own")
        self.assertEqual((a.fields["from"], a.fields["to"]), ("Human_Example", "Alice_Example"))
        self.assertEqual(a.tags, [])  # control: assigned away from me is not assigned-to-me
        c = self.p2e[("PROJ-1", "workitem.comment")]
        self.assertEqual(c.quote, "Handing this to Alice, she owns the upload path.")
        self.assertEqual(c.tags, [])

    def test_eid_follows_the_contract_and_is_unique(self):
        t = self.p2e[("PROJ-2", "workitem.transition")]
        self.assertEqual(t.eid, W.make_eid("ado", "ACME", "PROJ-2", "2026-11-20T09:45:00Z", "state"))
        eids = [e.eid for ev, _, _ in self.out for e in ev]
        self.assertEqual(len(eids), len(set(eids)))

    def test_quote_is_capped(self):
        s = make_source(transport=Replay(), cfg_extra={"quoteChars": 40})
        item = copy.deepcopy(fixture("poll1-batch.json")["body"]["value"][2])
        ev = s.map_item(item)
        self.assertEqual(len(ev[0].quote), 40)
        self.assertTrue(ev[0].quote.endswith("..."))

    def test_mention_by_name_without_guid(self):
        s = make_source(transport=Replay())
        item = copy.deepcopy(fixture("poll1-batch.json")["body"]["value"][2])
        item["fields"]["System.History"] = "<p>@Human_Example ping</p>"
        self.assertEqual(s.map_item(item)[0].tags, ["mention"])
        s2 = make_source(transport=Replay())
        item["fields"]["System.History"] = "<p>@Bob_Example ping</p>"
        self.assertEqual(s2.map_item(item)[0].tags, [])  # control

    def test_string_identity_form(self):
        self.assertEqual(ADO.identity("Alice_Example <alice@acme.example.com>"),
                         {"name": "Alice_Example", "unique": "alice@acme.example.com", "id": ""})
        self.assertEqual(ADO.identity(None)["name"], "")

    def test_memory_is_bounded(self):
        s = make_source(transport=Replay())
        old = ADO.ITEM_MEMORY
        try:
            ADO.ITEM_MEMORY = 2
            for i in range(5):
                s.state["items"][str(i)] = {"changed": "2026-11-20T09:0%d:00Z" % i}
            s._trim_memory()
            self.assertEqual(sorted(s.state["items"]), ["3", "4"])
        finally:
            ADO.ITEM_MEMORY = old


class ErrorsAndRate(unittest.TestCase):
    def test_429_raises_with_retry_after_and_counts_the_streak(self):
        s = make_source(transport=Replay("throttled-429.json"))
        with self.assertRaises(W.SourceError) as cm:
            s.poll(None)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 120))
        h = s.health()
        self.assertEqual((h["failStreak"], h["rateRemaining"]), (1, 0))
        self.assertIn("429", h["lastError"])

    def test_retry_after_on_a_200_is_the_next_poll_floor(self):
        s = make_source(transport=Replay("delayed-200-retry-after.json"))
        ev, _, nxt = s.poll(None)
        self.assertEqual((ev, nxt), ([], 45))
        self.assertEqual(W.effective_wait(60, nxt), 60)
        self.assertEqual(W.effective_wait(30, nxt), 60)
        _, _, out = run_polls()
        self.assertIsNone(out[0][2])  # control: no server hint, no floor

    def test_203_sign_in_page_is_an_error_not_empty(self):
        s = make_source(transport=Replay("signin-203.json"))
        with self.assertRaises(W.SourceError) as cm:
            s.poll(None)
        self.assertIn("sign-in", str(cm.exception))

    def test_success_resets_the_streak(self):
        rp = Replay("throttled-429.json", "poll3-wiql.json")
        rp.queue[1]["request"]["body"]["query"] = rp.queue[1]["request"]["body"]["query"].replace(
            "09:50:00Z", "09:00:00Z")
        s = make_source(transport=rp)
        with self.assertRaises(W.SourceError):
            s.poll(None)
        s.poll(None)
        h = s.health()
        self.assertEqual((h["failStreak"], h["lastError"], h["lastOk"]), (0, None, "2026-11-20T10:00:00Z"))

    def test_network_error_is_a_source_error(self):
        def boom(*a):
            raise OSError("timed out")
        s = make_source(transport=boom)
        with self.assertRaises(W.SourceError):
            s.poll(None)


def hook_body(mutate=None):
    doc = fixture("hook-workitem-updated.json")["body"]
    if mutate:
        mutate(doc)
    return json.dumps(doc).encode()


class Webhook(unittest.TestCase):
    """The receiver alone verifies (built-in "basic" verifier); accept_webhook only normalizes, with empty creds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rx = WH.Receiver(copy.deepcopy(WATCH_JSON), self.tmp.name, resolver=resolver, log=lambda line: None)

    def spooled(self):
        src = WH.WebhookSource({"name": "boards-hook", "kind": "webhook", "_state": self.tmp.name}, {}, {})
        events, _, _ = src.poll(0)
        return events

    def after_poll2(self):
        """A source whose memory has seen polls 1 and 2 (PROJ-2 at rev 6, Resolved)."""
        s, _, out = run_polls()
        return s, out[2][1]

    def catch_up(self, s, cur):
        s.transport = Replay(wiql_like("2026-11-20T09:50:00Z", [2]), "poll4-batch.json")
        return s.poll(cur)

    def test_route_config_validates_and_uses_the_builtin_basic_verifier(self):
        routes, errs = WH.routes_from_config(WATCH_JSON)
        self.assertEqual((sorted(routes), errs), (["boards-hook"], []))
        self.assertIn("basic", WH.VERIFIERS)
        cfg = copy.deepcopy(WATCH_JSON)
        cfg["sources"][1]["auth"]["type"] = "hmac"  # control: a basic route needs a basic auth block
        self.assertNotEqual(WH.routes_from_config(cfg)[1], [])

    def test_receiver_refuses_bad_credentials_and_accepts_good(self):
        body = hook_body()
        for hdrs in (basic(HOOK_USER, "wrong"), basic("someone-else", HOOK_SECRET), basic("", HOOK_SECRET), {},
                     {"Authorization": "Bearer " + HOOK_SECRET}):
            st, obj = self.rx.handle("POST", "/hook/boards-hook", hdrs, body)
            self.assertEqual(st, 401, (hdrs, obj))
        self.assertEqual(self.spooled(), [])
        st, obj = self.rx.handle("POST", "/hook/boards-hook", basic(HOOK_USER, HOOK_SECRET), body)  # control
        self.assertEqual((st, obj), (202, {"accepted": 1}))

    def test_end_to_end_receiver_spool_one_event_with_the_poll_eid(self):
        st, _ = self.rx.handle("POST", "/hook/boards-hook", basic(HOOK_USER, HOOK_SECRET), hook_body())
        self.assertEqual(st, 202)
        ev = self.spooled()
        self.assertEqual([(e.source, e.sourceKind, e.ref, e.type, e.fields["from"], e.fields["to"]) for e in ev],
                         [("boards", "ado", "PROJ-2", "workitem.transition", "Resolved", "Closed")])
        self.assertEqual(W.validate_event(ev[0].to_dict()), [])
        # the dedupe guarantee: the catch-up poll, which diffs against its own memory, makes the very same eid
        s, cur = self.after_poll2()
        polled, _, _ = self.catch_up(s, cur)
        self.assertEqual([e.eid for e in polled], [ev[0].eid])
        # control: an earlier change to the same item has a different eid
        _, _, out = run_polls()
        self.assertNotIn(ev[0].eid, [e.eid for e in out[1][0] if e.ref == "PROJ-2"])

    def test_accept_webhook_never_reads_creds(self):
        cls = W.load_source("ado")
        s = cls(W.source_cfg_with_me(WATCH_JSON, WATCH_JSON["sources"][0]), NoCreds(), {})
        self.assertIsInstance(s.creds, NoCreds)
        ev = s.accept_webhook({}, hook_body())  # no Authorization at all: verifying is not its job
        self.assertEqual([e.type for e in ev], ["workitem.transition"])
        with self.assertRaises(AssertionError):  # control: the trap does fire when creds are read
            s._auth_header()

    def test_good_hook_then_catch_up_poll_on_the_same_instance_adds_nothing(self):
        s, cur = self.after_poll2()
        ev = s.accept_webhook({}, hook_body())
        self.assertEqual([(e.ref, e.type) for e in ev], [("PROJ-2", "workitem.transition")])
        ev2, cur2, _ = self.catch_up(s, cur)
        self.assertEqual(ev2, [])  # the memory already holds rev 7
        self.assertEqual(cur2["since"], "2026-11-20T10:05:00Z")

    def test_flat_fields_form_uses_memory_when_there_is_no_old_value(self):
        def flat(doc):
            doc["resource"] = dict(doc["resource"]["revision"])
            doc["resource"]["fields"]["System.ChangedBy"] = "Bob_Example <bob@acme.example.com>"
        s, _ = self.after_poll2()
        self.assertEqual([(e.type, e.actor) for e in s.accept_webhook({}, hook_body(flat))],
                         [("workitem.transition", "Bob_Example")])
        fresh = make_source(transport=Replay())  # control: no memory and no oldValues, so no transition is known
        self.assertEqual([e.type for e in fresh.accept_webhook({}, hook_body(flat))], ["workitem.updated"])

    def test_assignee_and_comment_come_from_old_values(self):
        def reassign(doc):
            doc["resource"]["fields"] = {
                "System.Rev": {"oldValue": 6, "newValue": 7},
                "System.AssignedTo": {"newValue": doc["resource"]["revision"]["fields"]["System.AssignedTo"]},
                "System.CommentCount": {"oldValue": 0, "newValue": 1},
                "System.ChangedDate": {"oldValue": "2026-11-20T09:45:00Z", "newValue": "2026-11-20T10:05:00Z"}}
            doc["resource"]["revision"]["fields"].update({"System.State": "Resolved", "System.CommentCount": 1,
                                                          "System.History": "<div>done</div>"})
        ev = make_source(transport=Replay()).accept_webhook({}, hook_body(reassign))
        self.assertEqual([(e.type, e.fields.get("from"), e.fields.get("to")) for e in ev],
                         [("workitem.assigned", "", "Bob_Example"), ("workitem.comment", None, None)])

    def test_commented_hook_without_pairs_is_a_comment(self):
        def commented(doc):
            doc["eventType"] = "workitem.commented"
            doc["resource"] = dict(doc["resource"]["revision"])
            doc["resource"]["fields"].update({"System.CommentCount": 1, "System.History": "<div>ok</div>"})
        ev = make_source(transport=Replay()).accept_webhook({}, hook_body(commented))
        self.assertEqual([(e.type, e.quote) for e in ev], [("workitem.comment", "ok")])

    def test_other_event_types_are_ignored_and_junk_is_a_400(self):
        s = make_source(transport=Replay())
        self.assertEqual(s.accept_webhook({}, json.dumps({"eventType": "git.push", "resource": {}})), [])
        with self.assertRaises(W.SourceError) as cm:
            s.accept_webhook({}, b"not json")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(s.accept_webhook({}, hook_body())), 1)  # control


class ContractAndPrivacy(unittest.TestCase):
    def test_registry_loads_this_class_and_me_comes_from_the_injected_cfg(self):
        cls = W.load_source("ado")
        self.assertIs(cls, ADO.AdoSource)
        self.assertTrue(issubclass(cls, W.Source))
        s = make_source(transport=Replay())
        self.assertEqual((s.me_ids, s.me_names), ({ME_ID}, {"human_example"}))
        s2 = cls({"name": "boards", "kind": "ado", "org": "ACME", "project": "PROJ"}, {}, {})
        self.assertEqual(s2.me_ids, set())  # control: no injected me, nobody is me
        self.assertEqual(s2.base, "https://dev.azure.com/ACME")

    def test_config_with_refs_validates_and_a_literal_does_not(self):
        cfg = copy.deepcopy(WATCH_JSON)
        self.assertEqual(W.validate_config(cfg), [])
        cfg["sources"][0]["auth"]["token"] = PAT
        self.assertNotEqual(W.validate_config(cfg), [])

    def test_no_secret_in_events_cursor_or_state(self):
        s, _, out = run_polls()
        s.accept_webhook({}, hook_body())
        blob = json.dumps([[e.to_dict() for e in ev] + [cur] for ev, cur, _ in out]) + json.dumps(s.state)
        b64 = base64.b64encode((":" + PAT).encode()).decode()
        for secret in (PAT, HOOK_SECRET, b64):
            self.assertNotIn(secret, blob)
        self.assertIn("PROJ-1", blob)  # control: the blob is not empty

    def test_fixtures_use_only_example_names_and_hosts(self):
        import re
        hosts = set()
        for n in os.listdir(FIX):
            with open(os.path.join(FIX, n)) as f:
                text = f.read()
            if n.endswith(".json"):
                hosts |= set(re.findall(r"https?://([^/\"\s]+)", text))
        self.assertEqual(hosts, {"acme.example.com"})


def wiql_like(since, ids):
    return {"request": {"method": "POST", "body": {"query": (
        "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = 'PROJ' AND [System.ChangedDate] >= '%s' "
        "ORDER BY [System.ChangedDate] ASC, [System.Id] ASC" % since)}},
        "status": 200, "headers": {"content-type": "application/json"},
        "body": {"queryType": "flat", "workItems": [{"id": i, "url": "x"} for i in ids]}}


if __name__ == "__main__":
    unittest.main()
