"""Bitbucket Cloud WATCH adapter (lib/watch_sources/bitbucket.py) against hand-written recorded responses.

Phase 6 acceptance (docs/design/four-desks.md section 7): the poll query, the cursor advance and the change mapping
into the contract's Event, all through an injected HTTP layer. No network. Every positive check has a control.
Run: python3 -m unittest test/test_watch_bitbucket.py
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "bitbucket")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as W  # noqa: E402  (the same module object the adapter imports)

API = "https://api.acme.example.com/2.0"
REPO_URL = API + "/repositories/acme/widget"
STATES = "state=OPEN&state=MERGED&state=DECLINED&state=SUPERSEDED"
SEED_URL = REPO_URL + "/pullrequests?" + STATES + "&sort=-updated_on&pagelen=1"
Q_0900 = "q=updated_on%20%3E%3D%202026-11-20T09%3A00%3A00%2B00%3A00"
LIST1_URL = REPO_URL + "/pullrequests?" + STATES + "&" + Q_0900 + "&sort=updated_on&pagelen=50"
LIST1_P2 = LIST1_URL + "&page=2"
Q_1022 = "q=updated_on%20%3E%3D%202026-11-20T10%3A22%3A00%2B00%3A00"
LIST2_URL = REPO_URL + "/pullrequests?" + STATES + "&" + Q_1022 + "&sort=updated_on&pagelen=50"
BRANCH_URL = REPO_URL + "/refs/branches/main"
TOKEN = "tok-FIXTURE-0123456789"


def fx(rel):
    with open(os.path.join(FIX, rel), "rb") as f:
        return f.read()


class FakeHTTP:
    """Recorded-response transport: url -> (status, headers, body). An unrecorded URL is a test failure (404)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if url not in self.routes:
            return 404, {}, b'{"type": "error", "error": {"message": "not recorded"}}'
        status, hdrs, body = self.routes[url]
        return status, hdrs, body

    def urls(self):
        return [c[0] for c in self.calls]


SEED_ROUTES = {SEED_URL: (200, {}, fx("seed/list-newest.json")), BRANCH_URL: (200, {}, fx("seed/branch-main.json"))}
POLL1_ROUTES = {
    LIST1_URL: (200, {}, fx("poll1/list-page1.json")),
    LIST1_P2: (200, {}, fx("poll1/list-page2.json")),
    REPO_URL + "/pullrequests/10/activity": (200, {}, fx("poll1/activity-10.json")),
    REPO_URL + "/pullrequests/11": (200, {}, fx("poll1/pr-11.json")),
    REPO_URL + "/pullrequests/11/activity": (200, {}, fx("poll1/activity-11.json")),
    REPO_URL + "/pullrequests/12": (200, {}, fx("poll1/pr-12.json")),
    REPO_URL + "/pullrequests/12/activity": (200, {}, fx("poll1/activity-12.json")),
    BRANCH_URL: (200, {}, fx("poll1/branch-main.json")),
}
POLL2_ROUTES = {
    LIST2_URL: (200, {}, fx("poll2/list.json")),
    REPO_URL + "/pullrequests/10/activity": (200, {}, fx("poll1/activity-10.json")),
    REPO_URL + "/pullrequests/11": (200, {}, fx("poll2/pr-11.json")),
    REPO_URL + "/pullrequests/11/activity": (200, {}, fx("poll1/activity-11.json")),
    BRANCH_URL: (200, {}, fx("poll1/branch-main.json")),
}

SRC_ENTRY = {"name": "acme-bb", "kind": "bitbucket", "repos": ["acme/widget"], "branches": ["main"],
             "api": API, "web": "https://acme.example.com",
             "auth": {"type": "bearer", "token": "env:ACME_BB_TOKEN"}}
WATCH_CFG = {"me": {"names": ["Human_Example"], "ids": {"acme-bb": "acct-me"}}, "sources": [SRC_ENTRY]}

# poll1 expected change mapping: (type, ref, actor, me, tags)
POLL1_EXPECTED = [
    ("pr.approved", "acme/widget#10", "Bob_Example", False, []),
    ("pr.merged", "acme/widget#10", "Bob_Example", False, []),
    ("pr.opened", "acme/widget#11", "Alice_Example", False, []),
    ("pr.comment", "acme/widget#11", "Bob_Example", False, ["mention"]),
    ("pr.review_requested", "acme/widget#11", "Alice_Example", False, ["review-requested"]),
    ("pr.changes_requested", "acme/widget#12", "Bob_Example", False, ["my-pr"]),
    ("pr.comment", "acme/widget#12", "acme-ci[bot]", False, ["my-pr"]),
    ("branch.pushed", "acme/widget@main", "Bob_Example", False, []),
]


def make_source(state=None, creds=None, entry=None):
    cls = W.load_source("bitbucket")
    cfg = W.source_cfg_with_me(WATCH_CFG, entry or SRC_ENTRY)
    src = cls(cfg, creds if creds is not None else {"token": TOKEN}, state if state is not None else {})
    src.now = lambda: "2026-11-20T11:00:00Z"
    return src


def run(routes, cursor, **kw):
    src = make_source(**kw)
    http = FakeHTTP(routes)
    src.http = http
    events, cur, nxt = src.poll(cursor)
    return src, http, events, cur, nxt


def seeded_cursor():
    return run(SEED_ROUTES, None)[3]


def poll1():
    return run(POLL1_ROUTES, seeded_cursor())


class Loader(unittest.TestCase):
    def test_registered_kind_loads_under_the_real_loader(self):
        cls = W.load_source("bitbucket")
        self.assertEqual(cls.kind, "bitbucket")
        self.assertTrue(issubclass(cls, W.Source))
        with self.assertRaises(KeyError):  # control: an unregistered kind does not load
            W.load_source("bitbucket-dc")

    def test_config_entry_validates(self):
        self.assertEqual(W.validate_config(WATCH_CFG), [])
        bad = dict(SRC_ENTRY, auth={"type": "bearer", "token": "literal-token-value"})
        errs = W.validate_config({"sources": [bad]})  # control: a literal token is refused by the contract
        self.assertTrue(any("literal" in e for e in errs), errs)


class PollQuery(unittest.TestCase):
    def test_seed_query_and_no_events(self):
        _, http, events, cur, _ = run(SEED_ROUTES, None)
        self.assertEqual(http.urls(), [SEED_URL, BRANCH_URL])
        self.assertEqual(events, [])
        rc = cur["repos"]["acme/widget"]
        self.assertEqual(rc["since"], "2026-11-20T09:00:00.500000+00:00")
        self.assertEqual(rc["ids"], [10])
        self.assertEqual(rc["branches"], {"main": "0f0f0f0f0f0f"})

    def test_poll_query_pins_states_filter_sort_and_paging(self):
        _, http, _, _, _ = poll1()
        urls = http.urls()
        self.assertEqual(urls[0], LIST1_URL)
        self.assertIn(LIST1_P2, urls)  # the `next` link was followed
        self.assertIn("%2B00%3A00", urls[0])  # '+' in the offset is encoded, not sent as a space
        for s in ("OPEN", "MERGED", "DECLINED", "SUPERSEDED"):
            self.assertIn("state=" + s, urls[0])
        # control: a different cursor changes the query, so the pin above is not vacuous
        src = make_source()
        self.assertNotEqual(src.list_url("acme/widget", "2026-11-20T10:22:00.000000+00:00"), LIST1_URL)
        self.assertEqual(src.list_url("acme/widget", "2026-11-20T10:22:00.000000+00:00"), LIST2_URL)

    def test_only_open_prs_are_read_by_id(self):
        _, http, _, _, _ = poll1()
        urls = http.urls()
        self.assertIn(REPO_URL + "/pullrequests/11", urls)
        self.assertNotIn(REPO_URL + "/pullrequests/10", urls)  # MERGED: the list object is enough
        self.assertNotIn(REPO_URL + "/pullrequests/9", urls)  # skipped by the full-precision check
        self.assertNotIn(REPO_URL + "/pullrequests/9/activity", urls)

    def test_request_headers(self):
        _, http, _, _, _ = poll1()
        for url, headers, timeout in http.calls:
            self.assertEqual(headers.get("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(headers.get("User-Agent"), "fleet-watch")
            self.assertEqual(timeout, W.HTTP_TIMEOUT_SEC)
        _, http2, _, _, _ = run(SEED_ROUTES, None, creds={"user": "human", "token": TOKEN},
                                entry=dict(SRC_ENTRY, auth={"type": "basic", "user": "env:U", "token": "env:T"}))
        import base64
        want = "Basic " + base64.b64encode(("human:" + TOKEN).encode()).decode()
        self.assertEqual(http2.calls[0][1].get("Authorization"), want)
        _, http3, _, _, _ = run(SEED_ROUTES, None, creds={}, entry=dict(SRC_ENTRY, auth={"type": "none"}))
        self.assertNotIn("Authorization", http3.calls[0][1])  # control: no creds, no header

    def test_default_api_host_is_bitbucket_cloud(self):
        entry = {k: v for k, v in SRC_ENTRY.items() if k not in ("api", "web")}
        src = make_source(entry=entry)
        self.assertTrue(src.list_url("acme/widget").startswith("https://api.bitbucket.org/2.0/repositories/acme/widget/"))
        self.assertEqual(src.instance, "bitbucket.org")


class ChangeMapping(unittest.TestCase):
    def test_poll1_maps_every_change(self):
        _, _, events, _, _ = poll1()
        got = [(e.type, e.ref, e.actor, e.me, e.tags) for e in events]
        self.assertEqual(got, POLL1_EXPECTED)

    def test_events_are_valid_contract_events(self):
        _, _, events, _, _ = poll1()
        self.assertTrue(events)
        for e in events:
            self.assertIsInstance(e, W.Event)
            self.assertEqual(W.validate_event(e.to_dict()), [], e.to_dict())
            self.assertEqual(e.sourceKind, "bitbucket")
            self.assertEqual(e.source, "acme-bb")
            self.assertEqual(e.to, "DECIDE")
            self.assertTrue(e.eid.startswith("bitbucket:acme.example.com:"), e.eid)
            self.assertTrue(e.url.startswith("https://acme.example.com/"), e.url)
        bad = events[0].to_dict()
        bad["at"] = "2026-11-20 10:03"
        self.assertNotEqual(W.validate_event(bad), [])  # control: the validator does reject a bad row

    def test_fields_and_quotes(self):
        _, _, events, _, _ = poll1()
        by = {(e.type, e.ref): e for e in events}
        merged = by[("pr.merged", "acme/widget#10")]
        self.assertEqual(merged.fields["mergeCommit"], "5a5a5a5a5a5a")
        self.assertEqual(merged.at, "2026-11-20T10:05:00.250000Z")
        self.assertEqual(W.score_for("fyi", merged.type), W.SCORE_STATE_CHANGE)
        opened = by[("pr.opened", "acme/widget#11")]
        self.assertEqual(opened.quote, "Stops retrying after 5 attempts. Fixes PROJ-1.")
        self.assertEqual(opened.fields["sourceBranch"], "alice/PROJ-1-budget")
        self.assertEqual(opened.title, "PROJ-1: Cap the retry budget")
        mention = by[("pr.comment", "acme/widget#11")]
        self.assertIn("@{acct-me}", mention.quote)
        push = by[("branch.pushed", "acme/widget@main")]
        self.assertEqual(push.fields["to"], "1b1b1b1b1b1b")
        self.assertEqual(push.quote, "Merge PROJ-1 retry fix")
        cr = by[("pr.changes_requested", "acme/widget#12")]
        self.assertEqual(cr.fields["branchOwner"], "Human_Example")
        # control: the deleted comment and the comment older than the cursor make no event
        quotes = [e.quote for e in events]
        self.assertNotIn("Older comment, before the cursor.", quotes)
        self.assertEqual(sum(1 for e in events if e.ref == "acme/widget#12" and e.type == "pr.comment"), 1)

    def test_me_flag_and_tags_follow_the_injected_me(self):
        cur = seeded_cursor()
        src = make_source(entry=dict(SRC_ENTRY, name="other"))  # me.ids has no "other" and no "bitbucket" key
        self.assertEqual(src.cfg["me"]["id"], None)
        src.http = FakeHTTP(POLL1_ROUTES)
        events, _, _ = src.poll(cur)
        tags = [t for e in events for t in e.tags]
        # names still match Human_Example (author of #12, reviewer of #11) but no id: the @{acct-me} mention is missed
        self.assertNotIn("mention", tags)
        self.assertIn("review-requested", tags)
        self.assertIn("my-pr", tags)

    def test_review_request_fires_once_for_a_names_only_me(self):
        entry = dict(SRC_ENTRY, name="other")  # no id: me matches by name only
        _, _, ev1, cur1, _ = run(POLL1_ROUTES, seeded_cursor(), entry=entry)
        self.assertEqual(sum(e.type == "pr.review_requested" for e in ev1), 1)
        _, _, ev2, _, _ = run(POLL2_ROUTES, cur1, entry=entry)
        self.assertEqual(sum(e.type == "pr.review_requested" for e in ev2), 0)  # #11 updated again: not re-fired
        cur_reset = json.loads(json.dumps(cur1))
        cur_reset["repos"]["acme/widget"]["prs"]["11"]["meReviewer"] = False
        _, _, ev3, _, _ = run(POLL2_ROUTES, cur_reset, entry=entry)
        self.assertEqual(sum(e.type == "pr.review_requested" for e in ev3), 1)  # control: unknown before, fires

    def test_declined_pr_maps_to_closed(self):
        page = json.loads(fx("poll2/list.json"))
        pr = dict(page["values"][1], state="DECLINED", reason="Superseded by a smaller change",
                  closed_by={"display_name": "Alice_Example", "nickname": "alice", "account_id": "acct-alice"})
        page["values"] = [pr]
        routes = dict(POLL2_ROUTES)
        routes[LIST2_URL] = (200, {}, json.dumps(page).encode())
        _, _, _, cur1, _ = poll1()
        src, http, ev, cur2, _ = run(routes, cur1)
        self.assertEqual([e.type for e in ev], ["pr.closed"])
        self.assertEqual(ev[0].fields["state"], "DECLINED")
        self.assertEqual(ev[0].quote, "Superseded by a smaller change")
        self.assertEqual(W.score_for("fyi", ev[0].type), W.SCORE_STATE_CHANGE)
        self.assertNotIn(REPO_URL + "/pullrequests/11", http.urls())  # closed: no by-id read
        self.assertNotIn("11", cur2["repos"]["acme/widget"]["prs"])
        body = json.loads(fx("webhook/pullrequest-fulfilled.json"))
        body["pullrequest"] = pr
        hook = src.accept_webhook({"X-Event-Key": "pullrequest:rejected"}, json.dumps(body).encode())
        self.assertEqual([e.eid for e in hook], [ev[0].eid])  # webhook and poll share the eid
        self.assertNotEqual(ev[0].eid, [e.eid for e in poll1()[2] if e.type == "pr.merged"][0])  # control

    def test_my_own_action_sets_me(self):
        src = make_source()
        body = json.loads(fx("webhook/pullrequest-approved.json"))
        body["approval"]["user"] = {"display_name": "Human_Example", "nickname": "human", "account_id": "acct-me"}
        ev = src.accept_webhook({"X-Event-Key": "pullrequest:approved"}, json.dumps(body).encode())[0]
        self.assertTrue(ev.me)
        ev2 = src.accept_webhook({"X-Event-Key": "pullrequest:approved"}, fx("webhook/pullrequest-approved.json"))[0]
        self.assertFalse(ev2.me)  # control: Bob's approval is not me


class CursorAdvance(unittest.TestCase):
    def test_cursor_advances_to_last_pr_and_tracks_open_prs(self):
        _, _, _, cur, _ = poll1()
        rc = cur["repos"]["acme/widget"]
        self.assertEqual(rc["since"], "2026-11-20T10:22:00.000000+00:00")
        self.assertEqual(rc["ids"], [12])
        self.assertEqual(sorted(rc["prs"]), ["11", "12"])  # #10 merged and left the cursor
        self.assertEqual(rc["prs"]["11"], {"state": "OPEN", "head": "b2b2b2b2b2b2", "reviewers": ["acct-me"],
                                           "meReviewer": True})
        self.assertFalse(rc["prs"]["12"]["meReviewer"])
        self.assertEqual(rc["branches"], {"main": "1b1b1b1b1b1b"})
        json.dumps(cur)  # the engine stores it as JSON

    def test_next_poll_uses_new_cursor_and_makes_only_the_new_change(self):
        _, _, ev1, cur1, _ = poll1()
        _, http, ev2, cur2, _ = run(POLL2_ROUTES, cur1)
        self.assertEqual(http.urls()[0], LIST2_URL)
        self.assertNotIn(REPO_URL + "/pullrequests/12/activity", http.urls())  # #12 at since with its id: skipped
        new = {e.eid for e in ev2} - {e.eid for e in ev1}
        self.assertEqual([e.type for e in ev2 if e.eid in new], ["pr.pushed"])
        pushed = [e for e in ev2 if e.type == "pr.pushed"][0]
        self.assertEqual(pushed.fields["from"], "b2b2b2b2b2b2")
        self.assertEqual(pushed.fields["to"], "d4d4d4d4d4d4")
        # the post-merge edit of #10 re-reports the merge under the SAME eid, so the engine's dedupe drops it
        self.assertIn([e.eid for e in ev1 if e.type == "pr.merged"][0], {e.eid for e in ev2})
        self.assertEqual(cur2["repos"]["acme/widget"]["since"], "2026-11-20T10:45:00.000000+00:00")
        # control: replaying the OLD cursor against the poll1 responses yields poll1's events again
        _, _, ev1b, _, _ = run(POLL1_ROUTES, json.loads(json.dumps(seeded_cursor())))
        self.assertEqual([e.eid for e in ev1b], [e.eid for e in ev1])

    def test_max_events_stops_at_a_pr_boundary_and_resumes(self):
        entry = dict(SRC_ENTRY, maxEventsPerPoll=2)
        _, _, ev, cur, _ = run(POLL1_ROUTES, seeded_cursor(), entry=entry)
        self.assertEqual([e.type for e in ev], ["pr.approved", "pr.merged"])
        rc = cur["repos"]["acme/widget"]
        self.assertEqual(rc["since"], "2026-11-20T10:05:00.250000+00:00")
        self.assertEqual(rc["branches"], {"main": "0f0f0f0f0f0f"})  # branch not read yet
        q = "q=updated_on%20%3E%3D%202026-11-20T10%3A05%3A00%2B00%3A00"
        resume_url = REPO_URL + "/pullrequests?" + STATES + "&" + q + "&sort=updated_on&pagelen=50"
        page1 = json.loads(fx("poll1/list-page1.json"))
        page1["values"] = page1["values"][1:]  # the server returns #10 (same second) and #11
        page1["next"] = resume_url + "&page=2"
        routes = dict(POLL1_ROUTES)
        routes[resume_url] = (200, {}, json.dumps(page1).encode())
        routes[resume_url + "&page=2"] = POLL1_ROUTES[LIST1_P2]
        _, _, ev2, _, _ = run(routes, cur, entry=dict(SRC_ENTRY, maxEventsPerPoll=50))
        self.assertEqual([e.type for e in ev2], [t[0] for t in POLL1_EXPECTED[2:]])  # nothing lost, #10 not repeated

    def test_request_budget_stops_without_losing_a_pr(self):
        entry = dict(SRC_ENTRY, maxRequestsPerPoll=3)  # list, #10 activity, #11 by id; #11 activity is over budget
        _, http, ev, cur, _ = run(POLL1_ROUTES, seeded_cursor(), entry=entry)
        self.assertEqual(len(http.calls), 3)
        self.assertEqual([e.type for e in ev], ["pr.approved", "pr.merged"])
        self.assertEqual(cur["repos"]["acme/widget"]["since"], "2026-11-20T10:05:00.250000+00:00")
        self.assertNotIn("11", cur["repos"]["acme/widget"]["prs"])  # control: #11 was not half-recorded


class Failures(unittest.TestCase):
    def test_429_raises_with_retry_after_and_counts_a_failure(self):
        state = {}
        src = make_source(state=state)
        src.http = FakeHTTP({LIST1_URL: (429, {"Retry-After": "120"}, b"{}")})
        cur = seeded_cursor()
        with self.assertRaises(W.SourceError) as cm:
            src.poll(cur)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.retry_after, 120)
        self.assertEqual(src.health()["failStreak"], 1)
        self.assertEqual(W.effective_wait(900, cm.exception.retry_after), 900)
        with self.assertRaises(W.SourceError):
            src.poll(cur)
        self.assertEqual(src.health()["failStreak"], 2)
        src.http = FakeHTTP(POLL1_ROUTES)  # control: a good poll resets the streak
        src.poll(cur)
        self.assertEqual(src.health()["failStreak"], 0)
        self.assertEqual(src.health()["rateRemaining"], None)
        self.assertEqual(state["health"]["failStreak"], 0)  # health lives in the engine-saved state

    def test_server_and_auth_errors(self):
        cur = seeded_cursor()
        for status in (500, 503, 401, 403, 404):
            src = make_source()
            src.http = FakeHTTP({LIST1_URL: (status, {}, b"oops")})
            with self.assertRaises(W.SourceError) as cm:
                src.poll(cur)
            self.assertEqual(cm.exception.status, status)
            self.assertNotIn(TOKEN, str(cm.exception))

    def test_next_link_on_another_origin_is_refused(self):
        page1 = json.loads(fx("poll1/list-page1.json"))
        page1["next"] = "https://evil.example.com/2.0/repositories/acme/widget/pullrequests?page=2"
        routes = dict(POLL1_ROUTES)
        routes[LIST1_URL] = (200, {}, json.dumps(page1).encode())
        src = make_source()
        http = FakeHTTP(routes)
        src.http = http
        with self.assertRaises(W.SourceError):
            src.poll(seeded_cursor())
        self.assertFalse(any("evil.example.com" in u for u in http.urls()))  # the token never went there

    def test_near_limit_raises_the_next_poll_floor(self):
        routes = dict(POLL1_ROUTES)
        st, _, body = routes[LIST1_URL]
        routes[LIST1_URL] = (st, {"X-RateLimit-NearLimit": "true"}, body)
        _, _, _, _, nxt = run(routes, seeded_cursor())
        self.assertEqual(nxt, 2 * 900)
        _, _, _, _, nxt2 = poll1()
        self.assertIsNone(nxt2)  # control: no hint, no floor

    def test_token_never_lands_in_events_cursor_or_health(self):
        src, _, events, cur, _ = poll1()
        blob = json.dumps([e.to_dict() for e in events]) + json.dumps(cur) + json.dumps(src.health()) + json.dumps(src.state)
        self.assertNotIn(TOKEN, blob)
        self.assertIn(TOKEN, json.dumps(src.creds))  # control: the scan would see it if it were there


class Webhook(unittest.TestCase):
    def setUp(self):
        _, _, events, _, _ = poll1()
        self.poll_eids = {e.eid: e for e in events}

    def hook(self, key, name):
        src = make_source()
        return src.accept_webhook({"x-event-key": key}, fx("webhook/" + name))

    def test_webhook_and_poll_share_eids(self):
        cases = [("pullrequest:fulfilled", "pullrequest-fulfilled.json", "pr.merged"),
                 ("pullrequest:approved", "pullrequest-approved.json", "pr.approved"),
                 ("pullrequest:comment_created", "pullrequest-comment_created.json", "pr.comment"),
                 ("pullrequest:changes_request_created", "pullrequest-changes_request_created.json", "pr.changes_requested"),
                 ("repo:push", "repo-push.json", "branch.pushed")]
        for key, name, type_ in cases:
            evs = self.hook(key, name)
            self.assertEqual([e.type for e in evs], [type_], key)
            self.assertIn(evs[0].eid, self.poll_eids, key)
            self.assertEqual(W.validate_event(evs[0].to_dict()), [])
        created = self.hook("pullrequest:created", "pullrequest-created.json")
        self.assertEqual([e.type for e in created], ["pr.opened", "pr.review_requested"])
        self.assertTrue(all(e.eid in self.poll_eids for e in created))

    def test_unwatched_repo_and_unknown_key_make_nothing(self):
        body = json.loads(fx("webhook/pullrequest-fulfilled.json"))
        body["repository"]["full_name"] = "acme/other"
        src = make_source()
        self.assertEqual(src.accept_webhook({"X-Event-Key": "pullrequest:fulfilled"}, json.dumps(body).encode()), [])
        self.assertEqual(self.hook("issue:created", "pullrequest-fulfilled.json"), [])
        self.assertEqual(len(self.hook("pullrequest:fulfilled", "pullrequest-fulfilled.json")), 1)  # control
        with self.assertRaises(W.SourceError):
            src.accept_webhook({"X-Event-Key": "repo:push"}, b"not json")

    def test_verify_signature(self):
        mod = sys.modules[W.load_source("bitbucket").__module__]
        body = fx("webhook/pullrequest-fulfilled.json")
        secret = "whsec-FIXTURE"
        good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(mod.verify_bitbucket(secret, {"X-Hub-Signature": good}, body))
        self.assertTrue(mod.verify_bitbucket(secret, {"x-hub-signature": good.upper().replace("SHA256", "sha256")}, body))
        self.assertFalse(mod.verify_bitbucket("other-secret", {"X-Hub-Signature": good}, body))
        self.assertFalse(mod.verify_bitbucket(secret, {"X-Hub-Signature": good}, body + b" "))
        self.assertFalse(mod.verify_bitbucket(secret, {}, body))
        self.assertFalse(mod.verify_bitbucket(secret, {"X-Hub-Signature": good.replace("sha256", "sha1")}, body))
        self.assertFalse(mod.verify_bitbucket("", {"X-Hub-Signature": good}, body))
        # the vendor's own test vector (manage-webhooks page, "Testing the webhook payload validation")
        vec = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"
        self.assertTrue(mod.verify_bitbucket("It's a Secret to Everybody", {"X-Hub-Signature": vec}, b"Hello World!"))
        self.assertFalse(mod.verify_bitbucket("It's a Secret to Everybody", {"X-Hub-Signature": vec}, b"Hello World?"))


class ThroughTheReceiver(unittest.TestCase):
    """The landed loopback receiver (lib/watch_sources/webhook.py) with route signature "bitbucket"."""

    SECRET = "whsec-FIXTURE-receiver"

    def receiver(self, state):
        from watch_sources import webhook as WH
        hook = {"name": "bb-hook", "kind": "webhook", "for": "acme-bb", "signature": "bitbucket",
                "auth": {"type": "hmac", "secret": "env:BB_HOOK_SECRET"}}
        cfg = dict(WATCH_CFG, sources=[SRC_ENTRY, hook])
        return WH, WH.Receiver(cfg, state=state, resolver=lambda ref: self.SECRET)

    def spooled(self, WH, state):
        p = WH.spool_path(state, "bb-hook")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_signed_delivery_lands_once_with_the_poll_eid(self):
        with tempfile.TemporaryDirectory() as state:
            WH, rx = self.receiver(state)
            self.assertIn("bitbucket", WH.VERIFIERS)  # registered when the receiver preloaded the adapter
            body = fx("webhook/pullrequest-fulfilled.json")
            sig = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
            hdrs = {"X-Event-Key": "pullrequest:fulfilled", "X-Hub-Signature": sig}
            status, resp = rx.handle("POST", "/hook/bb-hook", hdrs, body)
            self.assertEqual((status, resp.get("accepted")), (202, 1))
            rows = self.spooled(WH, state)
            poll_merged = [e for e in poll1()[2] if e.type == "pr.merged"]
            self.assertEqual([r["eid"] for r in rows], [poll_merged[0].eid])
            # webhook + catch-up poll of the same merge: one distinct eid, so seen.db keeps one event
            self.assertEqual(len({r["eid"] for r in rows} | {e.eid for e in poll_merged}), 1)
            # the engine drains the spool through the `webhook` source: exactly one Event comes out
            drained, cur, _ = WH.WebhookSource({"name": "bb-hook", "_state": state}, {}, {}).poll(None)
            self.assertEqual([(e.type, e.eid, e.sourceKind) for e in drained],
                             [("pr.merged", poll_merged[0].eid, "bitbucket")])
            again, _, _ = WH.WebhookSource({"name": "bb-hook", "_state": state}, {}, {}).poll(cur)
            self.assertEqual(again, [])  # control: the cursor moved past it

    def post_updated(self, rx, pr):
        hook = json.loads(fx("webhook/pullrequest-fulfilled.json"))
        body = json.dumps({"actor": pr["author"], "pullrequest": pr, "repository": hook["repository"]}).encode()
        sig = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        return rx.handle("POST", "/hook/bb-hook", {"X-Event-Key": "pullrequest:updated", "X-Hub-Signature": sig}, body)

    def drain(self, WH, state):
        return WH.WebhookSource({"name": "bb-hook", "_state": state}, {}, {}).poll(None)[0]

    def test_push_via_updated_webhook_plus_next_poll_is_one_event(self):
        _, _, ev1, cur1, _ = poll1()
        seen = {e.eid for e in ev1}  # what seen.db holds after poll1
        with tempfile.TemporaryDirectory() as state:
            WH, rx = self.receiver(state)
            # #11 head b2b2 -> d4d4; me dropped from reviewers so the webhook takes the plain-update path
            pr = dict(json.loads(fx("poll2/pr-11.json")), reviewers=[])
            status, _ = self.post_updated(rx, pr)
            self.assertEqual(status, 202)
            hooked = self.drain(WH, state)
        routes = dict(POLL2_ROUTES)
        routes[REPO_URL + "/pullrequests/11"] = (200, {}, json.dumps(pr).encode())
        _, _, ev2, _, _ = run(routes, cur1)
        new = [e for e in hooked + ev2 if e.eid not in seen]
        self.assertEqual(len({e.eid for e in new}), 1)  # webhook + next poll = ONE event
        self.assertEqual({e.type for e in new}, {"pr.pushed"})
        self.assertFalse([e for e in hooked if e.eid not in seen])  # the poll's eid wins

    def test_title_only_edit_is_not_a_push(self):
        _, _, ev1, cur1, _ = poll1()
        seen = {e.eid for e in ev1}
        pr = json.loads(fx("poll1/pr-11.json"))  # same head as poll1
        pr.update(title="PROJ-1 widget sizing (renamed)", updated_on="2026-11-20T10:40:00.000000+00:00", reviewers=[])
        page = json.loads(fx("poll2/list.json"))
        page["values"] = [dict(v, title=pr["title"], source=pr["source"]) if v["id"] == 11 else v
                          for v in page["values"]]
        routes = dict(POLL2_ROUTES)
        routes[LIST2_URL] = (200, {}, json.dumps(page).encode())
        routes[REPO_URL + "/pullrequests/11"] = (200, {}, json.dumps(pr).encode())
        with tempfile.TemporaryDirectory() as state:
            WH, rx = self.receiver(state)
            self.assertEqual(self.post_updated(rx, pr)[0], 202)
            hooked = self.drain(WH, state)
        _, _, ev2, _, _ = run(routes, cur1)
        new = [e for e in hooked + ev2 if e.eid not in seen]
        self.assertEqual([e.type for e in new], ["pr.updated"])  # control: an edit is pr.updated, not pr.pushed

    def test_adapter_gets_empty_creds_from_the_receiver(self):
        mod = sys.modules[W.load_source("bitbucket").__module__]
        seen = []
        real = mod.BitbucketSource.__init__

        def spy(inst, cfg, creds, state):
            seen.append(creds)
            real(inst, cfg, creds, state)
        mod.BitbucketSource.__init__ = spy
        try:
            with tempfile.TemporaryDirectory() as state:
                WH, rx = self.receiver(state)
                body = fx("webhook/pullrequest-fulfilled.json")
                sig = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
                status, _ = rx.handle("POST", "/hook/bb-hook",
                                      {"X-Event-Key": "pullrequest:fulfilled", "X-Hub-Signature": sig}, body)
        finally:
            mod.BitbucketSource.__init__ = real
        self.assertEqual(status, 202)
        self.assertEqual(seen, [{}])  # the secret never reaches the adapter

    def test_bad_or_missing_signature_is_refused_and_nothing_spools(self):
        with tempfile.TemporaryDirectory() as state:
            WH, rx = self.receiver(state)
            body = fx("webhook/pullrequest-fulfilled.json")
            bad = "sha256=" + hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
            for hdrs in ({"X-Event-Key": "pullrequest:fulfilled", "X-Hub-Signature": bad},
                         {"X-Event-Key": "pullrequest:fulfilled"},
                         {"X-Event-Key": "pullrequest:fulfilled", "X-Hub-Signature-256": bad}):
                status, _ = rx.handle("POST", "/hook/bb-hook", hdrs, body)
                self.assertEqual(status, 401, hdrs)
            self.assertEqual(self.spooled(WH, state), [])
            good = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
            status, _ = rx.handle("POST", "/hook/bb-hook", {"X-Event-Key": "pullrequest:fulfilled",
                                                             "X-Hub-Signature": good}, body)
            self.assertEqual(status, 202)  # control: the same body with the right secret is accepted


if __name__ == "__main__":
    unittest.main()
