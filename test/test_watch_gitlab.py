"""Tests for lib/watch_sources/gitlab.py (WATCH source `gitlab`, GitLab and Gitea flavors).

Recorded-fixture tests (phase 6 acceptance): the poll query, the cursor advance and the change mapping into the
contract's Event, for both flavors. Fixtures are hand-written from the vendors' public API docs
(test/fixtures/watch/gitlab/README.md). No network: a replayer stands in for HTTP and refuses any request that is
not the next recorded one. Every positive check has a control that must fail.

Run: python3 -m unittest test.test_watch_gitlab   (from the repo root)
"""
import base64
import calendar
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FX = os.path.join(ROOT, "test", "fixtures", "watch", "gitlab")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as W  # noqa: E402

GL = W.load_source("gitlab")
GLMOD = sys.modules[GL.__module__]
from watch_sources import webhook as WH  # noqa: E402
FAKE_TOKEN = "glpat-FAKE0123456789abcdefFAKE"
HOOK_SECRET = "hook-secret-FAKE-0123"


def fixture(name):
    with open(os.path.join(FX, name)) as f:
        return json.load(f)


def epoch(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


class Replayer:
    """Plays recorded exchanges in order. A request whose method or URL differs from the next recording fails."""

    def __init__(self, exchanges):
        self.exchanges = list(exchanges)
        self.seen = []  # (method, url, headers)

    def __call__(self, method, url, headers, timeout):
        assert timeout == W.HTTP_TIMEOUT_SEC, timeout
        if not self.exchanges:
            raise AssertionError("unrecorded request %s %s" % (method, url))
        want = self.exchanges.pop(0)
        if (method, url) != (want["request"]["method"], want["request"]["url"]):
            raise AssertionError("request mismatch:\n got %s %s\nwant %s %s"
                                 % (method, url, want["request"]["method"], want["request"]["url"]))
        self.seen.append((method, url, dict(headers)))
        r = want["response"]
        body = r["body"] if isinstance(r["body"], str) else json.dumps(r["body"])
        return r["status"], {k.lower(): v for k, v in r["headers"].items()}, body.encode("utf-8")


def make(flavor="gitlab", **extra):
    src = {"name": "acme-forge", "kind": "gitlab", "flavor": flavor, "base": "https://acme.example.com",
           "repos": ["acme/widget"], "branches": ["main"],
           "auth": {"type": "bearer", "token": "env:ACME_FORGE_TOKEN"}}
    src.update(extra)
    top = {"me": {"names": ["Human_Example"], "ids": {"acme-forge": "human-example"}}, "sources": [src]}
    cfg = W.source_cfg_with_me(top, src)
    return GL(cfg, {"token": FAKE_TOKEN}, {})


def run(source, fx_name, cursor):
    fx = fixture(fx_name)
    rp = Replayer(fx["exchanges"])
    source.transport = rp
    source.clock = lambda: epoch(fx["clock"])
    out = source.poll(cursor)
    return out, rp


def summary(events):
    return [(e.type, e.ref) for e in events]


class TestConfigAndInterface(unittest.TestCase):
    def test_registry_and_config(self):
        self.assertEqual(GL.kind, "gitlab")
        self.assertTrue(issubclass(GL, W.Source))
        for flavor in ("gitlab", "gitea"):
            cfg = {"sources": [{"name": "f", "kind": "gitlab", "flavor": flavor, "base": "https://acme.example.com",
                                "repos": ["acme/widget"], "auth": {"type": "bearer", "token": "env:T"}}]}
            self.assertEqual(W.validate_config(cfg), [], flavor)
        # control: a literal token is refused by the contract's check
        bad = {"sources": [{"name": "f", "kind": "gitlab", "base": "https://acme.example.com", "repos": ["acme/widget"],
                            "auth": {"type": "bearer", "token": FAKE_TOKEN}}]}
        self.assertTrue(W.validate_config(bad))
        # control: an unknown flavor is refused
        with self.assertRaises(W.SourceError):
            make(flavor="svn")


class TestGitLabPoll(unittest.TestCase):
    def test_poll_query_cursor_and_mapping(self):
        s = make()
        (events, cur, nxt), rp = run(s, "gitlab_poll1.json", None)
        self.assertEqual(rp.exchanges, [], "every recorded request was made")
        # poll query: exact URLs (the replayer refused anything else), GET only, token in Authorization only
        self.assertEqual([m for m, _u, _h in rp.seen], ["GET"] * 3)
        self.assertIn("updated_after=2026-11-20T10%3A00%3A00Z", rp.seen[0][1])
        self.assertIn("page=2", rp.seen[1][1])
        for _m, url, h in rp.seen:
            self.assertEqual(h["Authorization"], "Bearer " + FAKE_TOKEN)
            self.assertNotIn(FAKE_TOKEN, url)
        # change mapping
        self.assertEqual(summary(events), [
            ("pr.review_requested", "acme/widget!11"),
            ("pr.opened", "acme/widget!12"),
            ("pr.merged", "acme/widget!9"),
            ("pr.updated", "acme/widget!13"),
        ])
        rr, opened, merged, upd = events
        self.assertEqual(rr.tags, ["review-requested"])
        self.assertEqual(rr.fields["added"], ["human-example"])
        self.assertEqual(opened.at, "2026-11-20T10:02:11Z")
        self.assertEqual(opened.actor, "alice_example")
        self.assertIn("mention", opened.tags)
        self.assertEqual(opened.quote, "Implements PROJ-1. @human-example can you take a look?")
        self.assertEqual(opened.url, "https://acme.example.com/acme/widget/-/merge_requests/12")
        self.assertEqual(merged.eid, "gitlab:acme.example.com:acme/widget!9:2026-11-20T10:05:00Z:merged")
        self.assertEqual(merged.actor, "bob_example")
        self.assertEqual(merged.fields["to"], "merged")
        self.assertEqual(W.score_for(W.KIND_FYI, merged.type), W.SCORE_STATE_CHANGE)
        self.assertEqual(upd.tags, [])
        for e in events:
            self.assertEqual(W.validate_event(e.to_dict()), [], e.eid)
            self.assertEqual(e.to, W.DECIDE)
            self.assertEqual(e.sourceKind, "gitlab")
            self.assertTrue(e.quote)
            self.assertNotIn(FAKE_TOKEN, e.to_json())
        self.assertEqual(len({e.eid for e in events}), 4)
        # cursor advance: last updatedAt + the ids at that instant; first sight of a branch records it, no event
        rc = cur["repos"]["acme/widget"]
        self.assertEqual(rc["updatedAt"], "2026-11-20T10:05:00Z")
        self.assertEqual(rc["ids"], [9, 13])
        self.assertEqual(rc["branches"], {"main": "a" * 40})
        self.assertIsNone(nxt)
        self.assertEqual(s.health()["rateRemaining"], 1997)
        self.assertEqual(s.health()["failStreak"], 0)
        self.assertNotIn(FAKE_TOKEN, json.dumps(cur) + json.dumps(s.state))

        # poll 2 from that cursor: boundary ids skipped, reviewer/push/close/branch changes found
        (events2, cur2, nxt2), rp2 = run(s, "gitlab_poll2.json", cur)
        self.assertEqual(rp2.exchanges, [])
        self.assertIn("updated_after=2026-11-20T10%3A05%3A00Z", rp2.seen[0][1])
        self.assertEqual(summary(events2), [
            ("pr.review_requested", "acme/widget!12"),
            ("pr.pushed", "acme/widget!12"),
            ("pr.closed", "acme/widget!11"),
            ("branch.pushed", "acme/widget@main"),
        ])
        rr2, pushed, closed, bp = events2
        self.assertEqual(rr2.fields["added"], ["carol_example"])
        self.assertEqual(rr2.tags, ["mention"])  # not mine: no review-requested tag (control for the poll 1 check)
        self.assertEqual(pushed.fields["from"], "1212121212121212121212121212121212121212")
        self.assertEqual(closed.actor, "alice_example")
        self.assertEqual(closed.at, "2026-11-20T10:25:00Z")
        self.assertEqual(bp.at, "2026-11-20T10:22:00Z")  # -08:00 offset normalized to UTC
        self.assertEqual(bp.quote, "Fix PROJ-1 flaky test")
        self.assertEqual(bp.fields["from"], "a" * 40)
        self.assertFalse({e.eid for e in events} & {e.eid for e in events2}, "no event repeats across polls")
        self.assertEqual(cur2["repos"]["acme/widget"]["updatedAt"], "2026-11-20T10:25:00Z")
        self.assertEqual(cur2["repos"]["acme/widget"]["ids"], [11])
        self.assertEqual(cur2["repos"]["acme/widget"]["branches"]["main"], "b" * 40)
        # RateLimit-Remaining 0 with RateLimit-Reset 120 s out: the next poll floor is raised
        self.assertEqual(nxt2, 120)
        self.assertEqual(W.effective_wait(60, nxt2), 120)
        self.assertEqual(cur["repos"]["acme/widget"]["updatedAt"], "2026-11-20T10:05:00Z", "input cursor not mutated")

        # poll 3: 429 with Retry-After -> SourceError carrying it, failStreak counts
        with self.assertRaises(W.SourceError) as cm:
            run(s, "gitlab_poll3_429.json", cur2)
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 60))
        self.assertEqual(s.health()["failStreak"], 1)
        self.assertNotIn(FAKE_TOKEN, str(cm.exception))

    def test_control_without_cursor_boundary_skip_events_repeat(self):
        """Control: polling poll 2's responses with NO memory of poll 1 re-reports the boundary requests, so the
        skip in the cursor is what keeps them out."""
        s = make()
        (events, cur, _n), _rp = run(s, "gitlab_poll1.json", None)
        cur["repos"]["acme/widget"]["ids"] = []
        cur["repos"]["acme/widget"]["known"] = {}
        (events2, _c, _n2), _ = run(s, "gitlab_poll2.json", cur)
        refs = [e.ref for e in events2]
        self.assertIn("acme/widget!9", refs)
        self.assertIn("acme/widget!13", refs)

    def test_control_query_mismatch_fails(self):
        """Control: a source that asked from the wrong cursor would not match the recording."""
        s = make()
        bad = {"v": 1, "repos": {"acme/widget": {"updatedAt": "2026-11-20T09:00:00Z", "ids": [], "known": {}}}}
        with self.assertRaises(AssertionError):
            run(s, "gitlab_poll1.json", bad)

    def test_max_events_per_poll_stops_cursor(self):
        s = make(maxEventsPerPoll=2)
        (events, cur, _n), rp = run(s, "gitlab_poll1.json", None)
        self.assertEqual(summary(events), [("pr.review_requested", "acme/widget!11"), ("pr.opened", "acme/widget!12")])
        rc = cur["repos"]["acme/widget"]
        self.assertEqual((rc["updatedAt"], rc["ids"]), ("2026-11-20T10:02:11Z", [12]))
        self.assertEqual(len(rp.exchanges), 1, "branch not polled once the cap is reached")
        self.assertNotIn("9", rc["known"])  # the rest is picked up by the next poll


class TestGiteaPoll(unittest.TestCase):
    def test_poll_query_cursor_and_mapping(self):
        s = make(flavor="gitea")
        (events, cur, nxt), rp = run(s, "gitea_poll1.json", None)
        self.assertEqual(rp.exchanges, [])
        self.assertEqual(rp.seen[0][1],
                         "https://acme.example.com/api/v1/repos/acme/widget/pulls?state=all&sort=recentupdate&limit=50&page=1")
        for _m, _u, h in rp.seen:
            self.assertEqual(h["Authorization"], "token " + FAKE_TOKEN)
        self.assertEqual(summary(events), [
            ("pr.opened", "acme/widget#5"),
            ("pr.review_requested", "acme/widget#5"),
            ("pr.merged", "acme/widget#7"),
        ])  # PR 2 (updated 09:40, before the 10:00 window) is not reported
        opened, rr, merged = events
        self.assertEqual(opened.at, "2026-11-20T10:03:00Z")  # +00:00 normalized
        self.assertEqual(rr.tags, ["review-requested"])
        self.assertEqual(merged.actor, "bob_example")
        self.assertEqual(merged.eid, "gitlab:acme.example.com:acme/widget#7:2026-11-20T10:06:00Z:merged")
        self.assertEqual(merged.fields["flavor"], "gitea")
        for e in events:
            self.assertEqual(W.validate_event(e.to_dict()), [], e.eid)
        rc = cur["repos"]["acme/widget"]
        self.assertEqual((rc["updatedAt"], rc["ids"]), ("2026-11-20T10:06:00Z", [7]))
        self.assertEqual(rc["branches"], {"main": "d" * 40})
        self.assertIsNone(nxt)

        (events2, cur2, _n), rp2 = run(s, "gitea_poll2.json", cur)
        self.assertEqual(rp2.exchanges, [])
        self.assertEqual(summary(events2), [("pr.closed", "acme/widget#5")])  # 7 skipped, branch unchanged
        self.assertEqual(events2[0].at, "2026-11-20T10:20:00Z")
        self.assertEqual(cur2["repos"]["acme/widget"]["updatedAt"], "2026-11-20T10:20:00Z")

    def test_control_merged_flag_not_state(self):
        """Control: Gitea has no state 'merged'; with merged=false the same closed PR maps to pr.closed."""
        fx = fixture("gitea_poll1.json")
        fx["exchanges"][0]["response"]["body"][0]["merged"] = False
        s = make(flavor="gitea")
        s.transport = Replayer(fx["exchanges"])
        s.clock = lambda: epoch(fx["clock"])
        events, _c, _n = s.poll(None)
        self.assertEqual(events[-1].type, "pr.closed")


class TestWebhooks(unittest.TestCase):
    """ROUTE ruling: the receiver alone verifies (with the verifiers gitlab.py registers at import); accept_webhook
    only normalizes, with EMPTY creds. These go through lib/watch_sources/webhook.py's Receiver."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-gitlab-wh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)

    def cfg(self, flavor, signature=None):
        src = {"name": "acme-forge", "kind": "gitlab", "flavor": flavor, "base": "https://acme.example.com",
               "repos": ["acme/widget"], "branches": ["main"], "auth": {"type": "bearer", "token": "env:ACME_FORGE_TOKEN"}}
        route = {"name": "forge-hook", "kind": "webhook", "for": "acme-forge", "signature": signature or flavor,
                 "auth": {"type": "hmac", "secret": "env:ACME_FORGE_HOOK"}, "port": 0}
        return {"me": {"names": ["Human_Example"], "ids": {"acme-forge": "human-example"}}, "sources": [src, route]}

    def rx(self, cfg, secret=HOOK_SECRET):
        seen = []

        def resolver(ref):
            seen.append(ref)
            return secret

        r = WH.Receiver(cfg, self.state, resolver=resolver, log=lambda line: None)
        r.resolved = seen
        return r

    def spool(self):
        return WH.spool_drain(self.state, "forge-hook", 0, 100)[0]

    def poll_events(self, flavor, fx_name):
        s = make(flavor=flavor)
        (events, _c, _n), _ = run(s, fx_name, None)
        return events

    def test_verifiers_registered_by_import(self):
        self.assertIs(WH.VERIFIERS["gitlab"], GLMOD.verify_gitlab)
        self.assertIs(WH.VERIFIERS["gitea"], GLMOD.verify_gitea)
        _routes, errs = WH.routes_from_config(self.cfg("gitlab"))
        self.assertEqual(errs, [])
        # control: an unregistered signature name is a config error
        _routes, errs = WH.routes_from_config(self.cfg("gitlab", signature="gitlab-nope"))
        self.assertTrue(any("signature" in e for e in errs), errs)

    def test_accept_webhook_never_reads_creds(self):
        """Normalize-only: empty creds, and a creds object that fails on any read, give the same events."""
        fx = fixture("gitlab_hook_merge.json")

        class Exploding(dict):
            def get(self, *a, **k):
                raise AssertionError("accept_webhook read creds")

            def __getitem__(self, k):
                raise AssertionError("accept_webhook read creds")

        s = make()
        s.creds = Exploding()
        evs = s.accept_webhook(fx["headers"], json.dumps(fx["body"]).encode())  # no signature header at all
        self.assertEqual(summary(evs), [("pr.merged", "acme/widget!9")])

    def test_gitlab_token_receiver_end_to_end(self):
        fx = fixture("gitlab_hook_merge.json")
        body = json.dumps(fx["body"]).encode()
        r = self.rx(self.cfg("gitlab"))
        # control first: wrong token and missing token are refused, nothing spooled
        for hdrs in ({"X-Gitlab-Token": "wrong"}, {}):
            st, obj = r.handle("POST", "/hook/forge-hook", dict(fx["headers"], **hdrs), body)
            self.assertEqual(st, 401, obj)
        self.assertEqual(self.spool(), [])
        st, obj = r.handle("POST", "/hook/forge-hook", dict(fx["headers"], **{"X-Gitlab-Token": HOOK_SECRET}), body)
        self.assertEqual((st, obj), (202, {"accepted": 1}))
        self.assertEqual(r.resolved, ["env:ACME_FORGE_HOOK"] * 3)  # the receiver resolved the secret, per request
        spooled = self.spool()
        self.assertEqual(summary(spooled), [("pr.merged", "acme/widget!9")])
        self.assertEqual(W.validate_event(spooled[0].to_dict()), [])
        # receiver -> spool -> engine: the next poll of the same change dedupes to one event
        polled = [e for e in self.poll_events("gitlab", "gitlab_poll1.json") if e.ref == "acme/widget!9"]
        self.assertEqual(len(polled), 1)
        self.assertEqual(spooled[0].eid, polled[0].eid)
        self.assertEqual(len({e.eid for e in spooled + polled}), 1)
        # legacy '2026-11-20 10:05:00 UTC' timestamps give the same eid
        lfx = fixture("gitlab_hook_merge_legacy_ts.json")
        st, _ = r.handle("POST", "/hook/forge-hook", dict(lfx["headers"], **{"X-Gitlab-Token": HOOK_SECRET}),
                         json.dumps(lfx["body"]).encode())
        self.assertEqual(st, 202)
        self.assertEqual({e.eid for e in self.spool()}, {polled[0].eid})
        # the secret never lands in the spool
        for name in os.listdir(os.path.join(self.state, "watch", "webhook")):
            with open(os.path.join(self.state, "watch", "webhook", name)) as f:
                self.assertNotIn(HOOK_SECRET, f.read())

    def test_gitlab_signed_hook_through_receiver(self):
        fx = fixture("gitlab_hook_merge.json")
        body = json.dumps(fx["body"]).encode()
        key = b"0123456789abcdef0123456789abcdef"
        whsec = "whsec_" + base64.b64encode(key).decode()
        r = self.rx(self.cfg("gitlab"), secret=whsec)
        ts = str(epoch("2026-11-20T10:05:05Z"))
        sig = base64.b64encode(hmac.new(key, b"msg_1." + ts.encode() + b"." + body, hashlib.sha256).digest()).decode()
        hdrs = dict(fx["headers"], **{"webhook-id": "msg_1", "webhook-timestamp": ts, "webhook-signature": "v1," + sig})
        old = GLMOD._clock
        self.addCleanup(setattr, GLMOD, "_clock", old)
        GLMOD._clock = lambda: epoch("2026-11-20T10:05:30Z")
        self.assertEqual(r.handle("POST", "/hook/forge-hook", hdrs, body)[0], 202)
        self.assertEqual(r.handle("POST", "/hook/forge-hook", hdrs, body.replace(b"Bump", b"Dump"))[0], 401)  # tampered
        no_sig = {k: v for k, v in hdrs.items() if k != "webhook-signature"}
        self.assertEqual(r.handle("POST", "/hook/forge-hook", dict(no_sig, **{"X-Gitlab-Token": whsec}), body)[0], 401)
        GLMOD._clock = lambda: epoch("2026-11-20T11:00:00Z")
        self.assertEqual(r.handle("POST", "/hook/forge-hook", hdrs, body)[0], 401)  # replay outside the window
        self.assertEqual(len(self.spool()), 1)

    def test_gitea_receiver_end_to_end(self):
        fx = fixture("gitea_hook_merged.json")
        body = json.dumps(fx["body"]).encode()
        sig = hmac.new(HOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = self.rx(self.cfg("gitea"))
        # controls: signature over a different body; empty digest (Gitea sends one when no secret is set);
        # the right digest checked under the gitlab scheme
        self.assertEqual(r.handle("POST", "/hook/forge-hook", dict(fx["headers"], **{"X-Gitea-Signature": sig}),
                                  body + b" ")[0], 401)
        self.assertEqual(r.handle("POST", "/hook/forge-hook", dict(fx["headers"], **{"X-Gitea-Signature": ""}),
                                  body)[0], 401)
        wrong = self.rx(self.cfg("gitea", signature="gitlab"))
        self.assertEqual(wrong.handle("POST", "/hook/forge-hook", dict(fx["headers"], **{"X-Gitea-Signature": sig}),
                                      body)[0], 401)
        self.assertEqual(self.spool(), [])
        st, obj = r.handle("POST", "/hook/forge-hook", dict(fx["headers"], **{"X-Gitea-Signature": sig}), body)
        self.assertEqual((st, obj), (202, {"accepted": 1}))
        spooled = self.spool()
        polled = [e for e in self.poll_events("gitea", "gitea_poll1.json") if e.ref == "acme/widget#7"]
        self.assertEqual(summary(spooled), [("pr.merged", "acme/widget#7")])
        self.assertEqual(spooled[0].eid, polled[0].eid)

    def test_non_mr_hook_ignored(self):
        r = self.rx(self.cfg("gitlab"))
        body = json.dumps({"object_kind": "push", "ref": "refs/heads/main"}).encode()
        self.assertEqual(r.handle("POST", "/hook/forge-hook", {"X-Gitlab-Token": HOOK_SECRET}, body),
                         (202, {"accepted": 0}))


class TestFixtureHygiene(unittest.TestCase):
    def test_fixtures_use_example_names_only(self):
        import re
        for name in sorted(os.listdir(FX)):
            with open(os.path.join(FX, name)) as f:
                text = f.read()
            hosts = set(re.findall(r"https?://([^/\"\s)]+)", text)) - {"acme.example.com"}
            if name == "README.md":
                hosts = {h for h in hosts if not h.endswith(("gitlab.com", "gitea.com"))}
            self.assertEqual(hosts, set(), name)
            emails = set(re.findall(r"[\w.+-]+@([\w.-]+\.\w+)", text)) - {"acme.example.com"}
            self.assertEqual(emails, set(), name)
            self.assertNotIn("glpat-", text, name)


if __name__ == "__main__":
    unittest.main()
