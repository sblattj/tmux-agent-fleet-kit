"""Tests for the WATCH GitHub source (lib/watch_sources/github.py) against recorded responses in
test/fixtures/watch/github/ through an injected HTTP layer. Every positive check has a control that must fail.
Run: python3 -m unittest test/test_watch_github.py
Live read-only run (network, unauthenticated, any public repo):
  FLEET_WATCH_LIVE=1 FLEET_WATCH_LIVE_REPO=owner/repo python3 -m unittest test.test_watch_github.Live
"""
import http.server
import json
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "github")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as W  # noqa: E402

GH = W.load_source("github")
NOW = 1795402800  # 900 s before the reset in events_403_ratelimit.json
TOKEN = "ghp_fixtureTokenValue0000000000000000"
REPO = "acme/widget"


def fx(name):
    with open(os.path.join(FIX, name)) as f:
        d = json.load(f)
    body = d.get("body")
    return d["status"], dict(d.get("headers") or {}), b"" if body is None else json.dumps(body).encode()


class FakeHTTP:
    """Serves queued fixtures per path prefix and records every request."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "timeout": timeout})
        path = url.split("api.github.com", 1)[-1]
        for prefix, queue in self.routes.items():
            if path.startswith(prefix):
                if not queue:
                    raise AssertionError("no fixture left for %s" % path)
                return fx(queue.pop(0))
        raise AssertionError("unexpected request %s" % url)


def make(routes, cfg=None, creds=None, state=None):
    c = {"name": "oss", "kind": "github", "repos": [REPO], "me": {"names": ["Human_Example"], "id": "human-example"}}
    c.update(cfg or {})
    src = GH(c, creds or {}, state if state is not None else {})
    fake = FakeHTTP(routes)
    src.http = fake
    src.clock = lambda: NOW
    src.jitter = lambda: 1.0
    return src, fake


EV = "/repos/%s/events" % REPO


def primed():
    """A cursor after the first (priming) poll of the recorded page."""
    src, _ = make({EV: ["events_recorded_200.json"]})
    _, cur, _ = src.poll(None)
    return cur


class Registry(unittest.TestCase):
    def test_load_source_github(self):
        self.assertEqual(GH.kind, "github")
        self.assertTrue(issubclass(GH, W.Source))
        with self.assertRaises(KeyError):  # control
            W.load_source("not-a-kind")

    def test_constructor_is_contract_shape(self):
        src = GH({"repos": [REPO]}, None, None)  # (cfg, creds, state), all three positional
        self.assertEqual(src.creds, {})
        self.assertEqual(src.state, {})


class Cursor(unittest.TestCase):
    def test_first_poll_primes_and_emits_nothing(self):
        src, fake = make({EV: ["events_recorded_200.json"]})
        evs, cur, nxt = src.poll(None)
        self.assertEqual(evs, [])
        self.assertEqual(cur["repos"][REPO]["etag"], 'W/"etag-page1-recorded"')
        self.assertEqual(len(cur["repos"][REPO]["ids"]), 15)
        self.assertEqual(cur["repos"][REPO]["lastAt"], "2026-11-23T00:27:39Z")
        self.assertNotIn("If-None-Match", fake.calls[0]["headers"])
        # control: backfill emits the whole recorded page
        src2, _ = make({EV: ["events_recorded_200.json"]}, cfg={"backfill": True})
        self.assertEqual(len(src2.poll(None)[0]), 15)

    def test_etag_304_makes_no_events(self):
        cur = primed()
        src, fake = make({EV: ["events_304.json"]})
        evs, cur2, nxt = src.poll(cur)
        self.assertEqual(fake.calls[0]["headers"]["If-None-Match"], 'W/"etag-page1-recorded"')
        self.assertEqual(evs, [])
        self.assertEqual(cur2, cur)
        # control: the same cursor served a changed page makes events
        src, _ = make({EV: ["events_new_200.json"]})
        self.assertEqual(len(src.poll(cur)[0]), 4)

    def test_cursor_advances_by_id_set_not_max_id(self):
        cur = primed()
        src, _ = make({EV: ["events_new_200.json"]})
        evs, cur2, _ = src.poll(cur)
        self.assertEqual([e.type for e in evs],
                         ["pr.opened", "issue.comment", "pr.merged", "push"])  # oldest first
        ids = cur2["repos"][REPO]["ids"]
        # the comment's id (20960000002) is LOWER than ids already seen (27988675934): a max-id cursor drops it
        self.assertIn("20960000002", ids)
        self.assertLess(20960000002, max(int(i) for i in cur["repos"][REPO]["ids"]))
        self.assertEqual(cur2["repos"][REPO]["etag"], 'W/"etag-page2"')
        self.assertEqual(cur2["repos"][REPO]["lastAt"], "2026-11-23T01:41:00Z")
        self.assertNotEqual(cur2, cur)
        # the same page again (no ETag match) makes nothing new; control above made 4
        src, _ = make({EV: ["events_new_200.json"]})
        self.assertEqual(src.poll(cur2)[0], [])

    def test_poll_does_not_mutate_input_cursor(self):
        cur = primed()
        snap = json.dumps(cur, sort_keys=True)
        src, _ = make({EV: ["events_new_200.json"]})
        src.poll(cur)
        self.assertEqual(json.dumps(cur, sort_keys=True), snap)

    def test_max_events_per_poll_carries_the_rest(self):
        cur = primed()
        src, _ = make({EV: ["events_new_200.json"]}, cfg={"maxEventsPerPoll": 2})
        evs, cur2, _ = src.poll(cur)
        self.assertEqual(len(evs), 2)
        self.assertNotIn("etag", cur2["repos"][REPO])  # next poll must refetch, not get a 304
        src, fake = make({EV: ["events_new_200.json"]}, cfg={"maxEventsPerPoll": 2})
        evs2, cur3, _ = src.poll(cur2)
        self.assertNotIn("If-None-Match", fake.calls[0]["headers"])
        self.assertEqual([e.type for e in evs2], ["pr.merged", "push"])
        self.assertEqual({e.eid for e in evs} & {e.eid for e in evs2}, set())
        self.assertIn("etag", cur3["repos"][REPO])  # control: an untruncated poll keeps the ETag


class PollInterval(unittest.TestCase):
    def test_x_poll_interval_raises_next_poll(self):
        cur = primed()
        src, _ = make({EV: ["events_poll_interval_300.json"]})
        _, _, nxt = src.poll(cur)
        self.assertEqual(nxt, 300)
        self.assertEqual(W.effective_wait(60, nxt), 300)  # raises a 60 s pollSec
        self.assertEqual(W.effective_wait(900, nxt), 900)  # a floor, never a shortcut
        # control: the recorded value (60) does not raise a 60 s pollSec above itself; no header -> None
        src, _ = make({EV: ["events_304.json"]})
        self.assertEqual(src.poll(cur)[2], 60)
        src, _ = make({EV: ["events_429.json"]})  # no X-Poll-Interval header there
        with self.assertRaises(W.SourceError):
            src.poll(cur)

    def test_no_header_gives_none(self):
        cur = primed()
        src, fake = make({EV: ["events_304.json"]})
        orig = fake.__call__

        def strip(method, url, headers, timeout):
            s, h, b = orig(method, url, headers, timeout)
            h.pop("X-Poll-Interval", None)
            return s, h, b
        src.http = strip
        self.assertIsNone(src.poll(cur)[2])
        self.assertEqual(W.effective_wait(900, None), 900)


class Backoff(unittest.TestCase):
    def test_429_backs_off_with_retry_after(self):
        cur = primed()
        src, _ = make({EV: ["events_429.json"]})
        with self.assertRaises(W.SourceError) as cm:
            src.poll(cur)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.retry_after, 120)
        self.assertEqual(src.health()["failStreak"], 1)
        self.assertEqual(W.effective_wait(60, cm.exception.retry_after), 120)
        # control: a 200 clears the streak
        src.http = FakeHTTP({EV: ["events_304.json"]})
        src.poll(cur)
        self.assertEqual(src.health()["failStreak"], 0)

    def test_403_rate_limit_spent_waits_for_reset(self):
        src, _ = make({EV: ["events_403_ratelimit.json"]})
        with self.assertRaises(W.SourceError) as cm:
            src.poll(primed())
        self.assertEqual(cm.exception.retry_after, 900)
        self.assertEqual(src.health()["rateRemaining"], 0)

    def test_5xx_exponential_capped(self):
        src, _ = make({EV: ["events_500.json"] * 9})
        waits = []
        for _ in range(8):
            with self.assertRaises(W.SourceError) as cm:
                src.poll(primed())
            waits.append(cm.exception.retry_after)
        self.assertEqual(waits[:4], [60, 120, 240, 480])
        self.assertEqual(max(waits), W.BACKOFF_CAP_SEC)
        src.jitter = lambda: 0.0  # jitter halves the wait at most
        with self.assertRaises(W.SourceError) as cm:
            src.poll(primed())
        self.assertEqual(cm.exception.retry_after, W.BACKOFF_CAP_SEC // 2)

    def test_control_plain_403_is_not_a_rate_limit(self):
        src, _ = make({EV: ["events_403_ratelimit.json"]})

        def forbidden(method, url, headers, timeout):
            return 403, {"X-RateLimit-Remaining": "40"}, b'{"message": "Forbidden"}'
        src.http = forbidden
        with self.assertRaises(W.SourceError) as cm:
            src.poll(primed())
        self.assertIsNone(cm.exception.retry_after)

    def test_partial_failure_keeps_good_repo(self):
        cur = primed()
        cur["repos"]["acme/gadget"] = cur["repos"][REPO]
        src, _ = make({EV: ["events_new_200.json"], "/repos/acme/gadget/events": ["events_429.json"]},
                      cfg={"repos": [REPO, "acme/gadget"]})
        evs, cur2, nxt = src.poll(cur)
        self.assertEqual(len(evs), 4)
        self.assertEqual(nxt, 120)
        self.assertEqual(cur2["repos"]["acme/gadget"], cur["repos"]["acme/gadget"])  # failed repo's cursor held
        self.assertIn("429", src.health()["lastError"])


class Mapping(unittest.TestCase):
    def new_events(self):
        src, _ = make({EV: ["events_new_200.json"]})
        return {e.type: e for e in src.poll(primed())[0]}

    def test_all_events_validate(self):
        src, _ = make({EV: ["events_new_200.json"]}, cfg={"backfill": True})
        evs = src.poll(None)[0]
        self.assertEqual(len(evs), 19)
        for e in evs:
            self.assertEqual(W.validate_event(e.to_dict()), [], e.type)
            self.assertEqual(e.to, W.DECIDE)
            self.assertEqual(e.sourceKind, "github")
        self.assertEqual(len({e.eid for e in evs}), 19)
        bad = evs[0].to_dict()
        bad["to"] = "ROUTE"
        self.assertNotEqual(W.validate_event(bad), [])  # control

    def test_tags_and_scores(self):
        ev = self.new_events()
        op = ev["pr.opened"]
        self.assertEqual(op.ref, "acme/widget#12")
        self.assertEqual(op.url, "https://github.com/acme/widget/pull/12")
        self.assertEqual(op.fields["head"], "fix/retry")
        c = ev["issue.comment"]
        self.assertIn("mention", c.tags)
        self.assertTrue(c.quote.startswith("@human-example can you decide"))
        m = ev["pr.merged"]
        self.assertEqual(W.score_for(W.KIND_FYI, m.type), W.SCORE_STATE_CHANGE)
        self.assertEqual(m.tags, [])  # control: no tag on a plain merge
        self.assertEqual(W.score_for(W.KIND_FYI, ev["push"].type), W.SCORE_FYI)  # control
        self.assertEqual(ev["push"].actor, "acme-ci[bot]")
        src, _ = make({EV: ["events_recorded_200.json"]}, cfg={"backfill": True})
        rel = [e for e in src.poll(None)[0] if e.type == "repo.released"]
        self.assertEqual(len(rel), 2)
        self.assertEqual(W.score_for(W.KIND_FYI, rel[0].type), W.SCORE_STATE_CHANGE)

    def test_me_flag(self):
        src, _ = make({EV: ["events_new_200.json"]}, cfg={"me": {"names": [], "id": "Bob_Example"}})
        evs = src.poll(primed())[0]
        self.assertEqual({e.type for e in evs if e.me}, {"pr.opened", "pr.merged"})
        src, _ = make({EV: ["events_new_200.json"]}, cfg={"me": {"names": [], "id": None}})
        evs = src.poll(primed())[0]
        self.assertEqual([e for e in evs if e.me or e.tags], [])  # control: no identity, no me, no tags

    def test_quote_capped(self):
        src, _ = make({EV: ["events_new_200.json"]}, cfg={"quoteChars": 20})
        for e in src.poll(primed())[0]:
            self.assertLessEqual(len(e.quote), 20)


class Auth(unittest.TestCase):
    def test_unauthenticated_sends_no_auth_and_skips_notifications(self):
        src, fake = make({EV: ["events_recorded_200.json"]})
        src.poll(None)
        self.assertEqual(len(fake.calls), 1)
        h = fake.calls[0]["headers"]
        self.assertEqual(h["User-Agent"], "fleet-watch")
        self.assertNotIn("Authorization", h)
        self.assertEqual(fake.calls[0]["timeout"], W.HTTP_TIMEOUT_SEC)

    def test_token_header_notifications_and_no_leak(self):
        cur = primed()
        src, fake = make({EV: ["events_new_200.json"], "/notifications": ["notifications_200.json", "events_429.json"]},
                         creds={"token": TOKEN})
        evs, cur2, _ = src.poll(cur)
        self.assertEqual(fake.calls[0]["headers"]["Authorization"], "Bearer " + TOKEN)
        n = [e for e in evs if e.type == "notification.review_requested"]
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0].tags, ["review-requested"])
        self.assertEqual(cur2["notifications"]["lastModified"], "Mon, 23 Nov 2026 01:05:00 GMT")
        src.http = FakeHTTP({EV: ["events_429.json"], "/notifications": ["events_429.json"]})
        try:
            src.poll(cur2)
        except W.SourceError as e:
            err = str(e)
        blob = json.dumps([e.to_dict() for e in evs]) + json.dumps(cur2) + json.dumps(src.state) + \
            json.dumps(src.health()) + err
        self.assertNotIn(TOKEN, blob)
        self.assertIn(TOKEN, json.dumps(fake.calls))  # control: the scan does see the token where it is


class Webhook(unittest.TestCase):
    """End to end through the real receiver (watch_sources/webhook.py): it alone verifies the signature, builds this
    adapter with EMPTY creds and spools the result; the engine's webhook source drains the spool."""

    SECRET = "test-webhook-secret"

    def setUp(self):
        import tempfile
        from watch_sources import webhook as WH
        self.WH = WH
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = {"me": {"names": ["Human_Example"], "ids": {"github": "human-example"}},
               "sources": [{"name": "oss", "kind": "github", "repos": [REPO]},
                           {"name": "oss-hook", "kind": "webhook", "for": "oss", "signature": "github",
                            "auth": {"type": "hmac", "secret": "env:OSS_HOOK_SECRET"}, "bind": "127.0.0.1", "port": 0}]}
        table = {"env:OSS_HOOK_SECRET": self.SECRET}

        def resolver(ref):
            if ref not in table:
                raise ValueError("did not resolve")
            return table[ref]
        self.rx = WH.Receiver(cfg, self.tmp.name, resolver=resolver, log=lambda line: None)
        with open(os.path.join(FIX, "webhook_pull_request_merged.json")) as f:
            d = json.load(f)
        self.body = json.dumps(d["body"]).encode()
        self.headers = dict(d["headers"], **{"X-Hub-Signature-256": WH.sign(self.SECRET, self.body)})

    def post(self, headers, body):
        return self.rx.handle("POST", "/hook/oss-hook", headers, body)

    def drained(self):
        return self.WH.spool_drain(self.tmp.name, "oss-hook", 0, 100)[0]

    def test_webhook_and_poll_share_one_eid(self):
        st, obj = self.post(self.headers, self.body)
        self.assertEqual((st, obj), (202, {"accepted": 1}))
        hook = self.drained()
        self.assertEqual(len(hook), 1)
        self.assertEqual(hook[0].type, "pr.merged")
        self.assertEqual(hook[0].eid, W.make_eid("github", "oss", "acme/widget#12", "once", "pr.merged"))
        self.assertEqual(W.validate_event(hook[0].to_dict()), [])
        src, _ = make({EV: ["events_new_200.json"]})
        polled = src.poll(primed())[0]
        merged = [e for e in polled if e.type == "pr.merged"]
        self.assertEqual(hook[0].eid, merged[0].eid)  # webhook + next poll dedupe to one event in seen.db
        opened = [e for e in polled if e.type == "pr.opened"]
        self.assertNotEqual(opened[0].eid, hook[0].eid)  # control: another change on the same PR differs

    def test_bad_signature_refused_at_receiver(self):
        bad = dict(self.headers, **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(self.post(bad, self.body)[0], 401)
        nosig = {k: v for k, v in self.headers.items() if k != "X-Hub-Signature-256"}
        self.assertEqual(self.post(nosig, self.body)[0], 401)
        self.assertEqual(self.drained(), [])  # nothing spooled
        self.assertEqual(self.post(self.headers, self.body)[0], 202)  # control: the signed delivery passes
        self.assertEqual(len(self.drained()), 1)

    def test_adapter_needs_no_creds(self):
        src = GH(W.source_cfg_with_me({"me": {"ids": {"github": "human-example"}}}, {"name": "oss", "repos": [REPO]}),
                 {}, {})
        evs = src.accept_webhook({"X-GitHub-Event": "pull_request"}, self.body)  # no signature header at all
        self.assertEqual([e.type for e in evs], ["pr.merged"])
        self.assertEqual(src.accept_webhook({"X-GitHub-Event": "watch"}, self.body), [])  # control: unmapped event
        with self.assertRaises(W.SourceError):
            src.accept_webhook({"X-GitHub-Event": "pull_request"}, b"not json")


def delivery(event, body_obj):
    return {"X-GitHub-Event": event}, json.dumps(body_obj).encode()


class ReviewFixes(unittest.TestCase):
    def test_type_has_no_login_suffix_but_eid_does(self):
        pr = {"number": 12, "title": "Retry", "updated_at": "2026-11-23T01:05:00Z", "html_url": "https://github.com/acme/widget/pull/12"}

        def rr(who):
            h, b = delivery("pull_request", {"action": "review_requested", "pull_request": pr,
                                                "requested_reviewer": {"login": who},
                                                "repository": {"full_name": REPO}, "sender": {"login": "Bob_Example"}})
            return make({})[0].accept_webhook(h, b)[0]
        a, b = rr("human-example"), rr("Dana_Example")
        self.assertEqual(a.type, "pr.review_requested")
        self.assertEqual(a.tags, ["review-requested"])
        self.assertEqual(a.burst_key(), b.burst_key())  # one type for every reviewer
        self.assertNotEqual(a.eid, b.eid)  # control: the eid still tells the reviewers apart
        self.assertEqual(b.tags, [])

    def test_pagination_follows_fully_new_pages_and_carries_overflow(self):
        cur = primed()
        src, fake = make({EV: ["events_paged_1.json"], "/repositories/1/events": ["events_paged_2.json"]})
        evs, cur2, _ = src.poll(cur)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(evs), 6)  # 4 on page 1, 2 new on page 2 (its third event was seen)
        self.assertEqual(evs[0].fields["head"], "c" * 40)  # oldest first, across pages
        self.assertIn("etag", cur2["repos"][REPO])
        # budget 3: emit the 3 oldest, drop the ETag, the next poll (no If-None-Match) emits the other 3
        src, _ = make({EV: ["events_paged_1.json"], "/repositories/1/events": ["events_paged_2.json"]},
                      cfg={"maxEventsPerPoll": 3})
        evs, cur3, _ = src.poll(cur)
        self.assertEqual(len(evs), 3)
        self.assertNotIn("etag", cur3["repos"][REPO])
        src, fake = make({EV: ["events_paged_1.json"], "/repositories/1/events": ["events_paged_2.json"]},
                         cfg={"maxEventsPerPoll": 3})
        evs2, _, _ = src.poll(cur3)
        self.assertNotIn("If-None-Match", fake.calls[0]["headers"])
        self.assertEqual(len(evs2), 3)
        self.assertEqual({e.eid for e in evs} & {e.eid for e in evs2}, set())
        # control: a page that is not entirely new is not followed
        src, fake = make({EV: ["events_new_200.json"]})
        src.poll(cur)
        self.assertEqual(len(fake.calls), 1)

    def test_bad_bodies_and_transport_errors_are_source_errors(self):
        import http.client as hc
        for resp in [(200, {}, b"<html>oops</html>"), (200, {}, b'{"message": "x"}')]:
            src, _ = make({})
            src.http = lambda m, u, h, t, r=resp: r
            with self.assertRaises(W.SourceError):
                src.poll(primed())

        def broken(m, u, h, t):
            raise hc.RemoteDisconnected("closed")
        src.http = broken
        with self.assertRaises(W.SourceError) as cm:
            src.poll(primed())
        self.assertIsNotNone(cm.exception.retry_after)
        src.http = lambda m, u, h, t: (200, {}, b"[]")  # control
        self.assertEqual(src.poll(primed())[0], [])

    def test_backoff_streak_survives_a_fresh_instance(self):
        state = {}
        waits = []
        for _ in range(3):
            src, _ = make({EV: ["events_500.json"]}, state=state)  # a new Source per poll, one state dict
            with self.assertRaises(W.SourceError) as cm:
                src.poll(primed())
            waits.append(cm.exception.retry_after)
        self.assertEqual(waits, [60, 120, 240])
        self.assertEqual(state["failStreak"], 3)
        src, _ = make({EV: ["events_500.json"]}, state={})  # control: fresh state starts over
        with self.assertRaises(W.SourceError) as cm:
            src.poll(primed())
        self.assertEqual(cm.exception.retry_after, 60)

    def test_loader_identity_from_file_loaded_contract(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fleet_watch_contract_copy",
                                                      os.path.join(LIB, "fleet_watch_contract.py"))
        Wc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(Wc)
        cls = Wc.load_source("github")
        self.assertEqual(cls.kind, "github")
        self.assertTrue(issubclass(cls, W.Source))  # the class subclasses the by-name contract module
        self.assertIs(cls, GH)

    def test_notification_skipped_when_repo_feed_covers_it(self):
        src, _ = make({"/notifications": ["notifications_200.json"]}, cfg={"repos": []}, creds={"token": TOKEN})
        n = {"reason": "mention", "updated_at": "2026-11-23T01:20:00Z", "repository": {"full_name": REPO},
             "subject": {"title": "t", "type": "Issue", "url": "https://api.github.com/repos/acme/widget/issues/7"}}
        body = json.dumps([n]).encode()
        src.http = lambda m, u, h, t: (200, {}, body)
        self.assertEqual(len(src.poll({})[0]), 1)  # control: repo not watched, the mention arrives here
        src.repos = [REPO]
        src.http = lambda m, u, h, t: (304, {}, b"") if "/events" in u else (200, {}, body)
        self.assertEqual(src.poll({"repos": {REPO: {"etag": "x", "ids": []}}})[0], [])

    def test_release_webhook_only_published(self):
        rel = {"id": 5, "tag_name": "v1", "published_at": "2026-11-23T00:27:39Z"}
        src = make({})[0]
        for action, n in (("created", 0), ("released", 0), ("published", 1)):
            h, b = delivery("release", {"action": action, "release": rel, "repository": {"full_name": REPO},
                                           "sender": {"login": "Alice_Example"}})
            self.assertEqual(len(src.accept_webhook(h, b)), n, action)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/repos/acme/limited/"):
            self.send_response(429)
            self.send_header("Retry-After", "77")
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == 'W/"local"':
            self.send_response(304)
            self.send_header("ETag", 'W/"local"')
            self.send_header("X-Poll-Interval", "61")
            self.end_headers()
            return
        _, _, body = fx("events_recorded_200.json")
        self.send_response(200)
        self.send_header("ETag", 'W/"local"')
        self.send_header("X-Poll-Interval", "61")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class RealUrllibLayer(unittest.TestCase):
    """The default HTTP layer (urllib raises HTTPError on 304/429) against a loopback server."""

    def setUp(self):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.api = "http://127.0.0.1:%d" % self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_200_then_304_then_429(self):
        src = GH({"name": "local", "repos": [REPO], "api": self.api}, {}, {})
        evs, cur, nxt = src.poll(None)
        self.assertEqual((len(evs), len(cur["repos"][REPO]["ids"]), nxt), (0, 15, 61))
        evs, cur2, nxt = src.poll(cur)
        self.assertEqual((evs, cur2, nxt), ([], cur, 61))
        lim = GH({"name": "local", "repos": ["acme/limited"], "api": self.api}, {}, {})
        with self.assertRaises(W.SourceError) as cm:
            lim.poll(None)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 77))


@unittest.skipUnless(os.environ.get("FLEET_WATCH_LIVE") == "1" and os.environ.get("FLEET_WATCH_LIVE_REPO"),
                     "live run: set FLEET_WATCH_LIVE=1 and FLEET_WATCH_LIVE_REPO=owner/repo")
class Live(unittest.TestCase):
    def test_public_repo_unauthenticated(self):
        repo = os.environ["FLEET_WATCH_LIVE_REPO"]
        src = GH({"name": "live", "repos": [repo], "backfill": True}, {}, {})
        evs, cur, nxt = src.poll(None)
        print("\nLIVE poll 1: events=%d nextPollSec=%s rateRemaining=%s etag=%s" % (
            len(evs), nxt, src.health()["rateRemaining"], bool(cur["repos"][repo].get("etag"))))
        for e in evs:
            self.assertEqual(W.validate_event(e.to_dict()), [])
        evs2, cur2, nxt2 = src.poll(cur)
        print("LIVE poll 2: events=%d nextPollSec=%s rateRemaining=%s" % (
            len(evs2), nxt2, src.health()["rateRemaining"]))
        self.assertEqual(evs2, [])


if __name__ == "__main__":
    unittest.main()
