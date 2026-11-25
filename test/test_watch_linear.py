"""Tests for lib/watch_sources/linear.py (WATCH source `linear`) and lib/watch_sources/whosaid.py (`whosaid`).

Fixtures only: a fake urlopen plays the recorded GraphQL pages for Linear and refuses any other request; a fake
whosaid CLI (test/fixtures/watch/whosaid/fake-whosaid) prints recorded decisions and refuses anything but the local
engine. No network, no real names. Every positive check has a control.

Run: python3 -m unittest test.test_watch_linear   (from the repo root)
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FXL = os.path.join(ROOT, "test", "fixtures", "watch", "linear")
FXW = os.path.join(ROOT, "test", "fixtures", "watch", "whosaid")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as W  # noqa: E402

LIN = W.load_source("linear")
WHO = W.load_source("whosaid")
FAKE_KEY = "lin_api_FAKE0123456789abcdef"


def fx(name):
    with open(os.path.join(FXL, name)) as f:
        return f.read()


class Resp:
    def __init__(self, body, headers=None):
        self._b = body.encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._b


class FakeLinear:
    """Plays pages in order; checks each request is a POST to the GraphQL URL with the key and the right page."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.reqs = []

    def __call__(self, req, timeout=None):
        assert timeout == W.HTTP_TIMEOUT_SEC
        if not self.pages:
            raise AssertionError("unrecorded request")
        self.reqs.append(req)
        body = self.pages.pop(0)
        if isinstance(body, Exception):
            raise body
        return Resp(body, {"X-RateLimit-Requests-Remaining": "1499"})


def linear(**kw):
    cfg = {"name": "tracker", "kind": "linear", "teams": ["PROJ"], "auth": {"type": "pat", "token": "env:LINEAR_KEY"}}
    cfg.update(kw)
    return LIN(cfg, {"type": "pat", "token": FAKE_KEY}, None)


class TestLinear(unittest.TestCase):
    def poll(self, src, pages, cursor=None):
        fake = FakeLinear(pages)
        with mock.patch("urllib.request.urlopen", fake):
            out = src.poll(cursor)
        return out, fake

    def test_two_pages_map_to_events_with_typed_links(self):
        (evs, cur, _), fake = self.poll(linear(), [fx("issues-page1.json"), fx("issues-page2.json")],
                                        {"updated": "2026-11-20T00:00:00Z"})
        self.assertEqual([e.ref for e in evs], ["PROJ-1", "PROJ-2"])
        self.assertEqual([e.type for e in evs], ["issue.closed", "issue.created"])
        self.assertEqual(cur, {"updated": "2026-11-22T08:00:00.000Z"})
        e = evs[0]
        self.assertEqual((e.sourceKind, e.source, e.at, e.actor), ("linear", "tracker", "2026-11-21T10:02:11Z", "Dev One"))
        self.assertEqual(e.quote, "PROJ-1 Done: Cap upload retries at 3")
        rels = sorted((l["rel"], l["ref"]) for l in e.fields["links"])
        self.assertEqual(rels, [("blocked-by", "PROJ-9"), ("owner", "Dev One"), ("part-of", "PROJ-0"),
                                ("ships-in", "PROJ-cycle-7"), ("ships-in", "Uploads v2")])
        self.assertNotIn(("blocked-by", "PROJ-8"), rels)  # control: a "related" relation is not a block
        self.assertEqual([l["ref"] for l in evs[1].fields["links"]], ["PROJ"])  # no parent: part-of the team
        self.assertTrue(all(l["rel"] in W.LINK_RELS for ev in evs for l in ev.fields["links"]))
        # requests: POST, the key as is (personal key), the cursor as the filter, the second page after c1
        r1, r2 = fake.reqs
        self.assertEqual((r1.get_method(), r1.full_url), ("POST", "https://api.linear.app/graphql"))
        self.assertEqual(r1.get_header("Authorization"), FAKE_KEY)
        v1, v2 = json.loads(r1.data)["variables"], json.loads(r2.data)["variables"]
        self.assertEqual((v1["teams"], v1["since"], v1["after"]), (["PROJ"], "2026-11-20T00:00:00Z", None))
        self.assertEqual(v2["after"], "c1")
        self.assertNotIn("mutation", json.loads(r1.data)["query"])
        self.assertNotIn(FAKE_KEY, json.dumps([ev.to_dict() for ev in evs]) + json.dumps(cur))

    def test_bearer_type_adds_the_prefix(self):
        src = linear(auth={"type": "bearer", "token": "env:X"})
        _, fake = self.poll(src, [fx("issues-page2.json")])
        self.assertEqual(fake.reqs[0].get_header("Authorization"), "Bearer " + FAKE_KEY)

    def test_rate_limit_and_http_errors_raise_source_error(self):
        with self.assertRaises(W.SourceError) as cm:
            self.poll(linear(), [fx("ratelimited.json")])
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 60))
        err = urllib.error.HTTPError("https://api.linear.app/graphql", 401, "no", {}, io.BytesIO(b""))
        with self.assertRaises(W.SourceError) as cm:
            self.poll(linear(), [err])
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_config_needs_teams(self):
        self.assertIn("sources[t] needs teams for kind linear",
                      W.validate_config({"sources": [{"name": "t", "kind": "linear"}]}))
        self.assertEqual(W.validate_config({"sources": [{"name": "t", "kind": "linear", "teams": ["PROJ"],
                                                         "auth": {"type": "pat", "token": "env:LINEAR_KEY"}}]}), [])
        literal = W.validate_config({"sources": [{"name": "t", "kind": "linear", "teams": ["PROJ"],
                                                  "auth": {"type": "pat", "token": FAKE_KEY}}]})
        self.assertTrue(literal)  # control: a literal key is refused
        self.assertNotIn(FAKE_KEY, " ".join(literal))
        with self.assertRaises(W.SourceError):
            self.poll(linear(teams=[]), [])


class TestWhosaid(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="whosaid-ws-")
        self.meeting("2026-11-24 standup", "decisions-standup.json", 1000)
        os.makedirs(os.path.join(self.ws, "_index"))  # a workspace folder that is not a meeting

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def meeting(self, name, fixture, mtime):
        d = os.path.join(self.ws, name)
        os.makedirs(d, exist_ok=True)
        shutil.copy(os.path.join(FXW, fixture), os.path.join(d, "decisions.json"))
        os.utime(d, (mtime, mtime))

    def src(self, **kw):
        cfg = {"name": "calls", "kind": "whosaid", "workspace": self.ws, "bin": os.path.join(FXW, "fake-whosaid")}
        cfg.update(kw)
        return WHO(cfg, {}, None)

    def test_decisions_become_events_with_links_and_the_cursor_moves(self):
        evs, cur, _ = self.src().poll(None)
        self.assertEqual([e.ref for e in evs], ["2026-11-24 standup#D1", "2026-11-24 standup#D2"])  # D3 is blank
        self.assertEqual([e.type for e in evs], ["decision.agreed", "decision.proposed"])
        e = evs[0]
        self.assertEqual((e.sourceKind, e.quote, e.actor, e.title), ("whosaid", "we cap upload retries at 3, jittered",
                                                                     "Dev One", "upload retries"))
        self.assertEqual(sorted((l["rel"], l["ref"]) for l in e.fields["links"]),
                         [("decided-by", "Dev One"), ("part-of", "2026-11-24 standup")])
        self.assertEqual(cur, {"mtime": 1000, "done": ["2026-11-24 standup"]})
        self.assertEqual(self.src().poll(cur)[0], [])  # nothing changed since
        self.meeting("2026-11-25 review", "decisions-standup.json", 2000)
        evs2, cur2, _ = self.src().poll(cur)
        self.assertEqual({e.ref.split("#")[0] for e in evs2}, {"2026-11-25 review"})
        self.assertEqual(cur2["mtime"], 2000)

    def test_local_engine_only_bounded_and_a_bad_meeting_is_skipped(self):
        os.makedirs(os.path.join(self.ws, "2026-11-23 broken"))  # no decisions.json: the fake exits 1
        os.utime(os.path.join(self.ws, "2026-11-23 broken"), (500, 500))
        s = self.src()
        evs, _, _ = s.poll(None)
        self.assertEqual(len(evs), 2)
        self.assertEqual(s.health()["skipped"], 2)  # the broken meeting and the blank D3
        with mock.patch.dict(os.environ, {"FAKE_WHOSAID_SLEEP": "5"}):
            with self.assertRaises(W.SourceError) as cm:
                self.src(timeoutSec=1).poll(None)
        self.assertIn("timeout", str(cm.exception))
        with self.assertRaises(W.SourceError):
            self.src(workspace=os.path.join(self.ws, "nope")).poll(None)
        self.assertIn("sources[c] needs workspace for kind whosaid",
                      W.validate_config({"sources": [{"name": "c", "kind": "whosaid"}]}))


if __name__ == "__main__":
    unittest.main()
