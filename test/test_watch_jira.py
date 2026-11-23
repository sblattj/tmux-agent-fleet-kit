"""Phase 6 Jira adapter (lib/watch_sources/jira.py) against recorded fixtures in test/fixtures/watch/jira/.
Acceptance (four-desks.md section 7, phase 6): a recorded-fixture test of the poll query, the cursor advance and the
change mapping into the contract's Event. No live calls: every GET goes to a fake transport that serves fixture
files. Every positive check has a control that must fail.
Run: python3 -m unittest test/test_watch_jira.py
"""
import base64
import copy
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "jira")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as W  # noqa: E402
from watch_sources import jira as J  # noqa: E402

TOKEN = "fixture-token-not-a-secret"  # what the creds resolver would hand over; must never reach an Event
CLOUD_CFG = {"name": "acme-tracker", "kind": "jira", "base": "https://acme.example.com", "deployment": "cloud",
             "auth": {"type": "basic", "user": "env:ACME_TRACKER_USER", "token": "env:ACME_TRACKER_TOKEN"},
             "query": "project = PROJ AND (assignee = currentUser() OR watcher = currentUser())"}
SERVER_CFG = {"name": "acme-dc", "kind": "jira", "base": "https://acme.example.com",
              "auth": {"type": "bearer", "token": "keychain:acme-dc-token"}, "query": "project = PROJ"}
WATCH = {"me": {"names": ["Human_Example"], "ids": {"acme-tracker": "acct-123"}}}
CURSOR0 = {"updated": "2026-11-20T10:00:00.000Z", "ids": []}
NOW = J.parse_time("2026-11-20T13:00:00Z")  # this host's clock in every test unless a test sets its own
SECRET = "fixture-webhook-secret"  # what the receiver's resolver hands back for the route's secret reference


def load_receiver():
    """fd-p6-webhook's receiver: lib/watch_sources/webhook.py once landed; before that, the lane's branch blob."""
    path = os.path.join(LIB, "watch_sources", "webhook.py")
    if not os.path.exists(path):
        try:
            blob = subprocess.run(["git", "-C", ROOT, "show", "fd/fd-p6-webhook:lib/watch_sources/webhook.py"],
                                  capture_output=True, check=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        path = os.path.join(tempfile.mkdtemp(prefix="jira-wh-"), "webhook.py")
        with open(path, "wb") as f:
            f.write(blob)
    spec = importlib.util.spec_from_file_location("watch_sources.webhook", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["watch_sources.webhook"] = mod
    spec.loader.exec_module(mod)
    return mod


class Recorded:
    """Fake transport: serves routes.json entries, records every request (url, headers)."""

    def __init__(self, folder, override=None, server=None):
        self.server = server  # {"now": aware dt, "tz": zone name}: apply the JQL `updated >=` the way Jira would
        self.dir = os.path.join(FIX, folder)
        with open(os.path.join(self.dir, "routes.json")) as f:
            self.routes = json.load(f)["routes"]
        self.override = override or {}  # path -> (status, headers, body)
        self.requests = []

    def __call__(self, url, headers, timeout):
        self.requests.append((url, dict(headers), timeout))
        u = urllib.parse.urlsplit(url)
        assert u.hostname == "acme.example.com", url
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path in self.override:
            return self.override[u.path]
        for r in self.routes:
            if r["path"] == u.path and all((q.get(k) == v) if v is not None else (k not in q)
                                           for k, v in r["query"].items()):
                with open(os.path.join(self.dir, r["file"]), "rb") as f:
                    body = f.read()
                if self.server and u.path.endswith(("/search", "/search/jql")):
                    body = self.server_filter(q["jql"], body)
                return r.get("status", 200), r.get("headers", {}), body
        return 404, {}, b'{"errorMessages": ["no fixture route"]}'

    def server_filter(self, jql, body):
        """Keep only issues the server would match: a relative "-Nm" against the server's clock, or an absolute
        "yyyy-MM-dd HH:mm" read in the server's configured zone."""
        from zoneinfo import ZoneInfo
        lit = re.search(r'updated >= "([^"]+)"', jql).group(1)
        if lit.startswith("-"):
            floor = self.server["now"] - J._dt.timedelta(minutes=int(lit[1:-1]))
        else:
            floor = J._dt.datetime.strptime(lit, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(self.server["tz"]))
        page = json.loads(body)
        page["issues"] = [i for i in page["issues"] if J.parse_time(i["fields"]["updated"]) >= floor]
        return json.dumps(page).encode()

    def queries(self, path):
        out = []
        for url, _, _ in self.requests:
            u = urllib.parse.urlsplit(url)
            if u.path == path:
                out.append(dict(urllib.parse.parse_qsl(u.query)))
        return out


def make(cfg=CLOUD_CFG, folder="cloud", creds=None, state=None, override=None, server=None):
    cls = W.load_source("jira")
    src = cls(W.source_cfg_with_me(WATCH, cfg), creds if creds is not None else {"user": "human@acme.example.com",
                                                                                   "token": TOKEN}, state if state is not None else {})
    src.http = Recorded(folder, override, server)
    src.now = lambda: NOW
    return src


def summary(events):
    return [(e.ref, e.type, e.at, sorted(e.tags), e.me) for e in events]


EXPECTED_CLOUD = [
    ("PROJ-1", "issue.transition", "2026-11-20T10:02:11Z", [], False),
    ("PROJ-1", "issue.comment", "2026-11-20T10:04:00Z", ["mention"], False),
    ("PROJ-1", "issue.assignee", "2026-11-20T10:05:00Z", ["assigned-to-me"], False),
    ("PROJ-2", "issue.created", "2026-11-20T10:10:00Z", [], False),
    ("PROJ-2", "issue.field:priority", "2026-11-20T10:12:00Z", [], False),
    ("PROJ-2", "issue.field:labels", "2026-11-20T10:20:00Z", [], False),
    ("PROJ-2", "issue.transition", "2026-11-20T10:30:00Z", [], False),
    ("PROJ-3", "issue.field:summary", "2026-11-20T10:30:00Z", [], True),
]


class Registry(unittest.TestCase):
    def test_load_source_returns_jira(self):
        cls = W.load_source("jira")
        self.assertIs(cls, J.JiraSource)
        self.assertTrue(issubclass(cls, W.Source))
        self.assertEqual(cls.kind, "jira")
        with self.assertRaises(KeyError):  # control: an unknown kind is not loaded
            W.load_source("jirax")

    def test_config_validates_with_reference_auth(self):
        cfg = {"sources": [CLOUD_CFG, SERVER_CFG]}
        self.assertEqual(W.validate_config(cfg), [])
        bad = copy.deepcopy(cfg)
        bad["sources"][0]["auth"]["token"] = "literal-value"  # control: a literal token is refused
        self.assertTrue(any("literal" in e for e in W.validate_config(bad)))


class PollQuery(unittest.TestCase):
    def test_cloud_query_is_relative_jql_updated_since_cursor(self):
        src = make()
        src.poll(CURSOR0)
        qs = src.http.queries("/rest/api/3/search/jql")
        self.assertEqual(len(qs), 2, "two pages followed via nextPageToken")
        want = ('(project = PROJ AND (assignee = currentUser() OR watcher = currentUser())) AND '
                'updated >= "-195m" ORDER BY updated ASC')  # 180 min since the cursor + the 15 min overlap
        self.assertEqual(qs[0]["jql"], want)
        self.assertEqual(qs[0]["expand"], "changelog")
        self.assertIn("comment", qs[0]["fields"].split(","))
        self.assertNotIn("nextPageToken", qs[0])
        self.assertEqual(qs[1]["nextPageToken"], "tok-2")
        self.assertEqual(qs[1]["jql"], want)
        self.assertNotIn("2026-11-20", qs[0]["jql"])  # no absolute date, so no time zone in play

    def test_relative_window_math(self):
        t = J.parse_time("2026-11-20T10:00:00Z")
        self.assertEqual(J.jql_since(t, t + J._dt.timedelta(hours=1)), "-75m")
        self.assertEqual(J.jql_since(t, t + J._dt.timedelta(seconds=30)), "-16m")  # rounded up, never short
        self.assertEqual(J.jql_since(t, t - J._dt.timedelta(minutes=5)), "-15m")  # this host behind the cursor
        self.assertNotEqual(J.jql_since(t, t + J._dt.timedelta(hours=1)), "-60m")  # control: the overlap is there

    def test_mismatched_zone_still_emits_each_change_exactly_once(self):
        # /myself says UTC+14 while the server reads absolute JQL dates in Los Angeles, and its clock runs 10 min fast
        server = {"now": NOW + J._dt.timedelta(minutes=10), "tz": "America/Los_Angeles"}
        state = {"myself": {"ids": ["acct-123"], "timeZone": "Pacific/Kiritimati"}}
        a, cur, _ = make(state=state, server=server).poll(CURSOR0)
        b, cur2, _ = make(state=state, server=server).poll(cur)
        self.assertEqual(summary(a), EXPECTED_CLOUD)
        self.assertEqual(b, [])
        self.assertEqual(len({e.eid for e in a + b}), len(EXPECTED_CLOUD))
        # control: the old absolute literal in the /myself zone, read by this server, misses every change
        ctl = make(state=state, server=server)
        from zoneinfo import ZoneInfo
        ctl.poll_jql = lambda since: 'updated >= "%s" ORDER BY updated ASC' % since.astimezone(
            ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(ctl.poll(CURSOR0)[0], [])

    def test_read_only_get_with_auth_header_and_timeout(self):
        src = make()
        src.poll(CURSOR0)
        paths = [urllib.parse.urlsplit(u).path for u, _, _ in src.http.requests]
        self.assertEqual(paths, ["/rest/api/3/myself", "/rest/api/3/search/jql", "/rest/api/3/issue/PROJ-2/changelog",
                                 "/rest/api/3/search/jql"])
        want = "Basic " + base64.b64encode(("human@acme.example.com:" + TOKEN).encode()).decode()
        for _, h, timeout in src.http.requests:
            self.assertEqual(h["Authorization"], want)
            self.assertEqual(timeout, W.HTTP_TIMEOUT_SEC)
        bearer = make(SERVER_CFG, "server", creds={"token": TOKEN})
        bearer.poll(CURSOR0)
        self.assertEqual(bearer.http.requests[0][1]["Authorization"], "Bearer " + TOKEN)  # control: auth.type wins

    def test_myself_cached_in_state(self):
        state = {}
        src = make(state=state)
        src.poll(CURSOR0)
        self.assertEqual(state["myself"], {"ids": ["acct-123"]})
        again = make(state=state)
        again.poll(CURSOR0)
        self.assertNotIn("/rest/api/3/myself", [urllib.parse.urlsplit(u).path for u, _, _ in again.http.requests])

    def test_order_by_in_query_refused(self):
        with self.assertRaises(W.SourceError):
            make(dict(CLOUD_CFG, query="project = PROJ ORDER BY created"))
        make(dict(CLOUD_CFG, query="project = PROJ AND summary ~ 'order'"))  # control: the word alone is fine

    def test_server_query_and_paging(self):
        src = make(SERVER_CFG, "server", creds={"token": TOKEN})
        events, cur, _ = src.poll({"updated": "2026-11-20T12:00:00.000Z", "ids": []})
        qs = src.http.queries("/rest/api/2/search")
        self.assertEqual([q["startAt"] for q in qs], ["0", "2"])
        self.assertEqual(qs[0]["jql"], '(project = PROJ) AND updated >= "-75m" ORDER BY updated ASC')
        self.assertEqual(src.http.queries("/rest/api/3/search/jql"), [])  # control: no Cloud endpoint on DC


class Mapping(unittest.TestCase):
    def test_cloud_changes_map_to_events(self):
        events, cur, nxt = make().poll(CURSOR0)
        self.assertEqual(summary(events), EXPECTED_CLOUD)
        self.assertIsNone(nxt)
        t = events[0]
        self.assertEqual(t.eid, "jira:acme.example.com:PROJ-1:2026-11-20T10:02:11Z:status")
        self.assertEqual(t.eid, W.make_eid("jira", "acme.example.com", "PROJ-1", "2026-11-20T10:02:11Z", "status"))
        self.assertEqual((t.source, t.sourceKind, t.actor, t.title), ("acme-tracker", "jira", "Alice_Example",
                                                                     "Retry the upload on 503"))
        self.assertEqual(t.url, "https://acme.example.com/browse/PROJ-1")
        self.assertEqual(t.quote, "Status: In Review -> Done")
        self.assertEqual((t.fields["from"], t.fields["to"]), ("In Review", "Done"))
        self.assertEqual(W.score_for(W.KIND_FYI, t.type), W.SCORE_STATE_CHANGE)
        c = events[1]
        self.assertEqual(c.quote, "@Human_Example can you check the retry backoff?")
        self.assertEqual(c.eid, "jira:acme.example.com:PROJ-1:2026-11-20T10:04:00Z:comment:30002")
        self.assertEqual(events[5].actor, "acme-ci[bot]")  # bots reach the pipeline's drop filter, not dropped here
        self.assertNotIn("An old comment", json.dumps([e.to_dict() for e in events]))  # control: pre-cursor comment

    def test_every_event_is_contract_valid_and_carries_no_secret(self):
        events, _, _ = make().poll(CURSOR0)
        events += make(SERVER_CFG, "server", creds={"token": TOKEN}).poll(
            {"updated": "2026-11-20T12:00:00.000Z", "ids": []})[0]
        self.assertTrue(events)
        for e in events:
            d = json.loads(e.to_json())
            self.assertEqual(W.validate_event(d), [], d)
            self.assertEqual((d["to"], d["kind"], d["origin"]), (W.DECIDE, W.KIND_FYI, "outside"))
            self.assertEqual(len(set(e.eid for e in events)), len(events))
        blob = json.dumps([e.to_dict() for e in events])
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn("Authorization", blob)
        bad = events[0].to_dict()
        bad["sourceKind"] = "jiraz"  # control: validate_event does reject a bad row
        self.assertTrue(W.validate_event(bad))

    def test_me_mention_and_assignment_follow_injected_me(self):
        cfg = dict(CLOUD_CFG, name="other")  # me.ids has no entry for "other"; /myself still identifies the user
        events, _, _ = make(cfg).poll(CURSOR0)
        self.assertEqual(summary(events), EXPECTED_CLOUD)
        src = make()
        src.state["myself"] = {"ids": ["acct-999"], "timeZone": "Europe/Berlin"}
        src.cfg = dict(src.cfg, me={"names": [], "id": None})
        src.me_ids, src.me_names = set(), set()
        events, _, _ = src.poll(CURSOR0)  # control: a different me gets no mention, no assignment, no me flag
        self.assertEqual([e.tags for e in events if e.tags], [])
        self.assertFalse(any(e.me for e in events))

    def test_server_wiki_mention_and_username_assignment(self):
        events, cur, _ = make(SERVER_CFG, "server", creds={"token": TOKEN}).poll(
            {"updated": "2026-11-20T12:00:00.000Z", "ids": []})
        self.assertEqual(summary(events), [
            ("PROJ-4", "issue.comment", "2026-11-20T12:15:00Z", ["mention"], False),
            ("PROJ-5", "issue.assignee", "2026-11-20T12:20:00Z", ["assigned-to-me"], False),
            ("PROJ-6", "issue.transition", "2026-11-20T12:40:00Z", [], False),
        ])
        self.assertEqual(events[2].fields["to"], "Closed")
        self.assertEqual(cur, {"updated": "2026-11-20T12:40:00.000Z", "ids": ["10006"]})

    def test_quote_capped(self):
        src = make(dict(CLOUD_CFG, quoteChars=20))
        events, _, _ = src.poll(CURSOR0)
        self.assertTrue(all(len(e.quote) <= 20 for e in events))
        self.assertTrue(events[1].quote.endswith("..."))
        full, _, _ = make().poll(CURSOR0)  # control: the default cap leaves the same quote whole
        self.assertEqual(full[1].quote, "@Human_Example can you check the retry backoff?")


class Cursor(unittest.TestCase):
    def test_cursor_advances_to_last_updated_with_ids_at_that_instant(self):
        _, cur, _ = make().poll(CURSOR0)
        self.assertEqual(cur, {"updated": "2026-11-20T10:30:00.000Z", "ids": ["10002", "10003"]})
        json.dumps(cur)  # cursors.json must hold it

    def test_second_poll_from_new_cursor_yields_nothing(self):
        _, cur, _ = make().poll(CURSOR0)
        src = make()
        events, cur2, _ = src.poll(cur)
        self.assertEqual(events, [])
        self.assertEqual(cur2, cur)
        self.assertEqual(src.http.queries("/rest/api/3/search/jql")[0]["jql"].split(" AND updated")[1],
                         ' >= "-165m" ORDER BY updated ASC')
        # control: same instant without the ids re-emits the 10:30 changes
        ctl, _, _ = make().poll({"updated": "2026-11-20T10:30:00.000Z", "ids": []})
        self.assertEqual(summary(ctl), [EXPECTED_CLOUD[6], EXPECTED_CLOUD[7]])

    def test_max_events_cap_splits_without_loss_or_dupes(self):
        src = make(dict(CLOUD_CFG, maxEventsPerPoll=3))
        a, cur, _ = src.poll(CURSOR0)
        self.assertEqual(summary(a), EXPECTED_CLOUD[:3])
        self.assertEqual(cur, {"updated": "2026-11-20T10:05:00.000Z", "ids": ["10001"]})
        self.assertEqual(len(src.http.queries("/rest/api/3/search/jql")), 1, "stopped before page 2")
        b, cur2, _ = make().poll(cur)
        self.assertEqual(summary(a + b), EXPECTED_CLOUD)
        self.assertEqual(cur2["updated"], "2026-11-20T10:30:00.000Z")
        c, _, _ = make(dict(CLOUD_CFG, maxEventsPerPoll=7)).poll(CURSOR0)  # control: a cap that fits PROJ-2 takes it too
        self.assertNotEqual(summary(c), summary(a))

    def test_cap_never_splits_an_issue(self):
        a, cur, _ = make(dict(CLOUD_CFG, maxEventsPerPoll=4)).poll(CURSOR0)
        self.assertEqual(summary(a), EXPECTED_CLOUD[:3])  # PROJ-2's 4 events do not fit after PROJ-1's 3
        self.assertEqual(cur["ids"], ["10001"])
        b, _, _ = make(dict(CLOUD_CFG, maxEventsPerPoll=7)).poll(CURSOR0)  # control: cap 7 fits PROJ-2
        self.assertEqual(summary(b), EXPECTED_CLOUD[:7])

    def test_issue_at_cursor_skipped_without_refetch(self):
        src = make()
        src.poll({"updated": "2026-11-20T10:30:00.000Z", "ids": ["10002", "10003"]})
        paths = [urllib.parse.urlsplit(u).path for u, _, _ in src.http.requests]
        self.assertNotIn("/rest/api/3/issue/PROJ-2/changelog", paths)
        ctl = make()  # control: PROJ-2 not yet processed at that instant, so its 10:30 change is emitted
        got, _, _ = ctl.poll({"updated": "2026-11-20T10:30:00.000Z", "ids": ["10003"]})
        self.assertEqual(summary(got), [EXPECTED_CLOUD[6]])
        # the embedded changelog already reaches the cursor (oldest 10:30 <= 10:30): no refetch either way
        self.assertNotIn("/rest/api/3/issue/PROJ-2/changelog",
                         [urllib.parse.urlsplit(u).path for u, _, _ in ctl.http.requests])

    def test_truncated_changelog_refetched_only_when_needed_and_unioned(self):
        src = make()
        src.poll(CURSOR0)  # embedded oldest 10:30 > cursor 10:00: refetch
        self.assertIn("/rest/api/3/issue/PROJ-2/changelog", [urllib.parse.urlsplit(u).path for u, _, _ in src.http.requests])
        with open(os.path.join(FIX, "cloud", "changelog-PROJ-2.json")) as f:
            short = json.load(f)
        short["values"] = short["values"][:2]  # the refetch page bound cut off the newest entry
        src = make(override={"/rest/api/3/issue/PROJ-2/changelog": (200, {}, json.dumps(short).encode())})
        events, _, _ = src.poll(CURSOR0)
        self.assertEqual(summary(events), EXPECTED_CLOUD)  # the embedded newest entry survives the union
        ctl = make(override={"/rest/api/3/issue/PROJ-2/changelog": (200, {}, json.dumps(short).encode())})
        ctl._histories = lambda issue, since=None: (  # control: refetch replaces the embedded copy, no union
            ctl._fetch_histories("PROJ-2") if issue.get("key") == "PROJ-2" else issue["changelog"]["histories"])
        self.assertNotIn(EXPECTED_CLOUD[6], summary(ctl.poll(CURSOR0)[0]))

    def test_single_issue_over_cap_is_truncated_and_counted(self):
        src = make(dict(CLOUD_CFG, maxEventsPerPoll=2))
        events, cur, _ = src.poll(CURSOR0)
        self.assertEqual(summary(events), EXPECTED_CLOUD[:2])
        self.assertEqual(src.health()["truncated"], 1)
        self.assertEqual(cur["ids"], ["10001"])  # the cursor moves on rather than stalling on PROJ-1
        ctl = make(dict(CLOUD_CFG, maxEventsPerPoll=3))  # control: a cap that fits counts nothing
        ctl.poll(CURSOR0)
        self.assertEqual(ctl.health()["truncated"], 0)

    def test_issue_updated_again_after_cursor_emits_only_the_new_change(self):
        with open(os.path.join(FIX, "cloud", "search-page2.json")) as f:
            page = json.load(f)
        issue = page["issues"][0]
        issue["fields"]["updated"] = "2026-11-20T10:45:00.000+0000"
        issue["changelog"]["histories"].insert(0, {
            "id": "22002", "author": {"accountId": "acct-555", "displayName": "Alice_Example"},
            "created": "2026-11-20T10:45:00.000+0000",
            "items": [{"field": "status", "fieldId": "status", "fromString": "To Do", "toString": "In Progress"}]})
        issue["changelog"]["total"] = 2
        page = dict(page, issues=[issue])
        src = make(override={"/rest/api/3/search/jql": (200, {}, json.dumps(page).encode())})
        events, cur, _ = src.poll({"updated": "2026-11-20T10:30:00.000Z", "ids": ["10003"]})
        self.assertEqual(summary(events), [("PROJ-3", "issue.transition", "2026-11-20T10:45:00Z", [], False)])
        self.assertEqual(cur, {"updated": "2026-11-20T10:45:00.000Z", "ids": ["10003"]})
        ctl = make(override={"/rest/api/3/search/jql": (200, {}, json.dumps(page).encode())})
        both, _, _ = ctl.poll({"updated": "2026-11-20T10:30:00.000Z", "ids": []})  # control: not seen at 10:30
        self.assertEqual([e.type for e in both], ["issue.field:summary", "issue.transition"])

    def test_first_poll_starts_now_not_history(self):
        src = make()
        src.now = lambda: J.parse_time("2026-11-20T10:20:00Z")
        events, cur, _ = src.poll(None)
        self.assertEqual(summary(events), [EXPECTED_CLOUD[5], EXPECTED_CLOUD[6], EXPECTED_CLOUD[7]])
        self.assertEqual(src.http.queries("/rest/api/3/search/jql")[0]["jql"].split("updated >= ")[1][:6],
                         '"-15m"')
        back = make(dict(CLOUD_CFG, sinceSec=1800))  # control: sinceSec reaches back
        back.now = src.now
        self.assertEqual(len(back.poll(None)[0]), 8)


class Failures(unittest.TestCase):
    def test_429_raises_with_retry_after_and_health_tracks(self):
        with open(os.path.join(FIX, "cloud", "rate-limited.json"), "rb") as f:
            body429 = f.read()
        src = make(override={"/rest/api/3/search/jql": (429, {"Retry-After": "120"}, body429)})
        with self.assertRaises(W.SourceError) as cm:
            src.poll(CURSOR0)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 120))
        self.assertEqual(src.health()["failStreak"], 1)
        with self.assertRaises(W.SourceError):
            src.poll(CURSOR0)
        self.assertEqual(src.health()["failStreak"], 2)
        self.assertNotIn(TOKEN, src.health()["lastError"])
        src.http.override = {}  # control: recovery resets the streak and records lastOk and rate headroom
        src.poll(CURSOR0)
        h = src.health()
        self.assertEqual((h["failStreak"], h["lastError"], h["rateRemaining"]), (0, None, 96))
        self.assertTrue(h["lastOk"])

    def test_401_and_5xx(self):
        src = make(override={"/rest/api/3/myself": (401, {}, b"{}")})
        with self.assertRaises(W.SourceError) as cm:
            src.poll(CURSOR0)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (401, None))
        src = make(override={"/rest/api/3/search/jql": (503, {}, b"oops")})
        with self.assertRaises(W.SourceError) as cm:
            src.poll(CURSOR0)
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(len(make().poll(CURSOR0)[0]), 8)  # control: the same poll with 200s succeeds

    def test_network_error_becomes_source_error(self):
        src = make()

        def boom(url, headers, timeout):
            raise TimeoutError("deadline")
        src.http = boom
        with self.assertRaises(W.SourceError):
            src.poll(CURSOR0)
        self.assertEqual(src.health()["failStreak"], 1)
        self.assertNotIn("deadline", src.health()["lastError"] or "x")  # only the error type, never the payload
        src.http = Recorded("cloud")  # control: a working transport recovers
        self.assertEqual(len(src.poll(CURSOR0)[0]), 8)


class Webhook(unittest.TestCase):
    """The receiver (fd-p6-webhook) alone verifies X-Hub-Signature with its generic "hmac" verifier; jira's
    accept_webhook only normalizes. These tests go through Receiver.handle."""

    def setUp(self):
        self.wh = load_receiver()
        if self.wh is None:
            self.skipTest("webhook receiver not available (lib/watch_sources/webhook.py or fd/fd-p6-webhook)")
        self.state = tempfile.mkdtemp(prefix="jira-wh-state-")
        self.cfg = {"me": WATCH["me"], "sources": [CLOUD_CFG, {
            "name": "jira-hook", "kind": "webhook", "for": "acme-tracker", "signature": "hmac",
            "header": "X-Hub-Signature", "auth": {"type": "hmac", "secret": "env:ACME_JIRA_HOOK_SECRET"}}]}
        self.assertEqual(W.validate_config(self.cfg), [])
        self.rx = self.wh.Receiver(self.cfg, state=self.state, resolver=lambda ref: SECRET)

    def body(self):
        with open(os.path.join(FIX, "webhook", "issue-updated-status.json"), "rb") as f:
            return f.read()

    def sign(self, body, secret=SECRET):
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def spooled(self):
        path = self.wh.spool_path(self.state, "jira-hook")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_good_signature_plus_poll_is_one_event(self):
        body = self.body()
        code, resp = self.rx.handle("POST", "/hook/jira-hook", {"X-Hub-Signature": self.sign(body)}, body)
        self.assertEqual((code, resp.get("accepted")), (202, 1))
        rows = self.spooled()
        self.assertEqual([(r["ref"], r["type"], r["sourceKind"], r["source"]) for r in rows],
                         [("PROJ-1", "issue.transition", "jira", "acme-tracker")])
        self.assertEqual(W.validate_event(rows[0]), [])
        polled, _, _ = make().poll(CURSOR0)
        same = [e for e in polled if e.eid == rows[0]["eid"]]
        self.assertEqual(len(same), 1)  # the catch-up poll's copy has the same dedupe key: one event after seen.db
        self.assertEqual(len({rows[0]["eid"]} | {e.eid for e in polled}), len(polled))
        with open(self.wh.spool_path(self.state, "jira-hook")) as f:
            self.assertNotIn(SECRET, f.read())

    def test_bad_or_missing_signature_refused_by_receiver(self):
        body = self.body()
        for headers in ({"X-Hub-Signature": self.sign(body, "wrong")}, {},
                        {"X-Hub-Signature": self.sign(body + b" ")},
                        {"X-Signature-256": self.sign(body)}):  # right HMAC, wrong header for this route
            code, _ = self.rx.handle("POST", "/hook/jira-hook", headers, body)
            self.assertEqual(code, 401, headers)
        self.assertEqual(self.spooled(), [])  # nothing reached the adapter or the spool

    def test_adapter_does_not_verify(self):
        body = self.body()
        got = make(creds={}).accept_webhook({}, body)  # no secret, no signature: normalization only
        self.assertEqual(summary(got), [EXPECTED_CLOUD[0]])
        with open(J.__file__) as f:
            self.assertNotIn("hmac", f.read().split("def accept_webhook", 1)[1].split("def ", 1)[0])
        with self.assertRaises(W.SourceError):  # control: a non-JSON body is still refused
            make().accept_webhook({}, b"not json")

    def test_eid_is_second_precision_so_ms_drift_still_dedupes(self):
        body = self.body().replace(b'"updated": "2026-11-20T10:02:11.000+0000"', b'"updated": "2026-11-20T10:02:11.417+0000"')
        self.assertIn(b"10:02:11.417", body)
        got = make().accept_webhook({}, body)
        polled, _, _ = make().poll(CURSOR0)
        self.assertEqual(got[0].at, "2026-11-20T10:02:11.417Z")  # at keeps the precision
        self.assertNotEqual(got[0].at, polled[0].at)  # control: the two times do differ
        self.assertEqual(got[0].eid, polled[0].eid)  # the dedupe key does not


class FixtureHygiene(unittest.TestCase):
    def test_fixtures_use_only_acme_example_hosts(self):
        hosts, n = set(), 0
        for dp, _, fs in os.walk(FIX):
            for f in fs:
                with open(os.path.join(dp, f)) as fh:
                    text = fh.read()
                n += 1
                if f.endswith(".json"):
                    hosts |= set(re.findall(r"https?://([^/\"\s?]+)", text))
                    hosts |= set(h.split("@", 1)[1] for h in re.findall(r"[\w.+-]+@[\w.-]+", text))
        self.assertGreaterEqual(n, 10)
        self.assertEqual(hosts, {"acme.example.com"})
        self.assertNotEqual(set(re.findall(r"https?://([^/\"\s?]+)", "see https://jira.realcorp.net/x")),
                            {"acme.example.com"})  # control: the scan does see other hosts


if __name__ == "__main__":
    unittest.main()
