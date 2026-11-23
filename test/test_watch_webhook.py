"""Tests for the WATCH phase 6 loopback webhook receiver: lib/watch_sources/webhook.py.

Acceptance (docs/design/four-desks.md section 7, phase 6): a webhook with a bad signature is refused; a good one plus
the next poll yields one event (dedupe). Lane scope adds: the receiver never binds a non-loopback address.
Every positive check has a control that must fail. Run: python3 -m unittest test/test_watch_webhook.py
"""
import contextlib
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "webhook")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as C  # noqa: E402
from watch_sources import webhook as WH  # noqa: E402

SECRET = "fixture-secret-not-real"
OTHER = "some-other-secret"


def _fix(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


# --- a github stand-in until fd-p2-github lands: poll and accept_webhook share one mapping, built per the contract's
# webhook eid rule (fields both payloads carry: merged_at / closed_at / updated_at plus the change type) ---------------
def _pr_event(source, repo, pr):
    if pr.get("merged_at"):
        change, at = "merged", pr["merged_at"]
    elif pr.get("closed_at"):
        change, at = "closed", pr["closed_at"]
    else:
        change, at = "updated", pr["updated_at"]
    ref = "%s#%d" % (repo, pr["number"])
    return C.Event(eid=C.make_eid("github", source, ref, at, change), source=source, sourceKind="github", at=at,
                   type="pr." + change, ref=ref, url=pr.get("html_url", ""),
                   actor=(pr.get("merged_by") or pr.get("user") or {}).get("login", ""), title=pr.get("title", ""))


class StubGithub(C.Source):
    kind = "github"
    poll_body = None  # bytes of a recorded pulls response

    def poll(self, cursor):
        repo = self.cfg["repos"][0]
        return [_pr_event(self.cfg["name"], repo, pr) for pr in json.loads(self.poll_body)], cursor, None

    def accept_webhook(self, headers, body):
        assert not self.creds, "the receiver must not hand an adapter the secret"
        doc = json.loads(body)
        return [_pr_event(self.cfg["name"], doc["repository"]["full_name"], doc["pull_request"])]


def _load_source(kind):
    if kind == "github":
        return StubGithub
    return _REAL_LOAD(kind)


_REAL_LOAD = C.load_source


# --- the engine's dedupe, reduced to what 3.6 specifies: seen.db holds eid ------------------------------------------
class SeenDB:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (eid TEXT PRIMARY KEY, at TEXT)")
        self.out = []

    def ingest(self, events):
        for e in events:
            cur = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (e.eid, e.at))
            if cur.rowcount:
                self.out.append(e)


def _config(bind="127.0.0.1", secret_ref="env:OSS_HOOK_SECRET", extra=None):
    cfg = {
        "me": {"names": ["Human_Example"], "ids": {"github": "human-example"}},
        "sources": [
            {"name": "oss", "kind": "github", "repos": ["acme/widget"],
             "auth": {"type": "bearer", "token": "env:OSS_TOKEN"}},
            {"name": "oss-hook", "kind": "webhook", "for": "oss", "signature": "github",
             "auth": {"type": "hmac", "secret": secret_ref}, "bind": bind, "port": 0},
            {"name": "acme-ci", "kind": "webhook", "signature": "hmac", "header": "X-Acme-Signature",
             "auth": {"type": "hmac", "secret": "env:ACME_CI_SECRET"}, "bind": bind, "port": 0},
        ],
    }
    if extra:
        cfg["sources"].extend(extra)
    return cfg


def _resolver(ref):
    table = {"env:OSS_HOOK_SECRET": SECRET, "env:ACME_CI_SECRET": SECRET}
    if ref not in table:
        raise ValueError("did not resolve")
    return table[ref]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        p = mock.patch.object(C, "load_source", side_effect=_load_source)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        StubGithub.poll_body = _fix("github-pulls.poll.json")

    def rx(self, cfg=None, **kw):
        r = WH.Receiver(cfg or _config(), self.state, resolver=kw.pop("resolver", _resolver), log=lambda line: None,
                        **kw)
        return r

    def spool_lines(self, name):
        p = WH.spool_path(self.state, name)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]



class Live(Base):
    """Through a real socket on 127.0.0.1."""

    def setUp(self):
        super().setUp()
        self.r = self.rx()
        self.host, self.port = self.r.start()
        self.addCleanup(self.r.stop)

    def post(self, path, body, headers=None, method="POST"):
        c = http.client.HTTPConnection(self.host, self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        resp = c.getresponse()
        data = resp.read().decode("utf-8")
        c.close()
        return resp.status, data

    def raw_status(self, head):
        import socket
        with socket.create_connection((self.host, self.port), timeout=5) as s:
            s.sendall(head.encode("ascii"))
            return int(s.recv(1024).split(b" ", 2)[1])

    def gh_headers(self, body, secret=SECRET, event="pull_request", name="X-Hub-Signature-256"):
        return {name: WH.sign(secret, body), "X-GitHub-Event": event, "Content-Type": "application/json"}


class TestSignature(Live):
    def test_good_github_signature_accepted(self):
        body = _fix("github-pr-merged.webhook.json")
        st, data = self.post("/hook/oss-hook", body, self.gh_headers(body))
        self.assertEqual(st, 202, data)
        rows = self.spool_lines("oss-hook")
        self.assertEqual(len(rows), 1)
        self.assertEqual(C.validate_event(rows[0]), [])
        self.assertEqual((rows[0]["source"], rows[0]["sourceKind"]), ("oss", "github"))

    def test_bad_signature_refused(self):
        body = _fix("github-pr-merged.webhook.json")
        h = self.gh_headers(body)
        h["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        st, data = self.post("/hook/oss-hook", body, h)
        self.assertEqual(st, 401)
        self.assertEqual(self.spool_lines("oss-hook"), [])

    def test_wrong_secret_refused(self):
        body = _fix("github-pr-merged.webhook.json")
        st, _ = self.post("/hook/oss-hook", body, self.gh_headers(body, secret=OTHER))
        self.assertEqual(st, 401)
        self.assertEqual(self.spool_lines("oss-hook"), [])

    def test_tampered_body_refused(self):
        body = _fix("github-pr-merged.webhook.json")
        h = self.gh_headers(body)  # signed over the original
        tampered = body.replace(b"Alice_Example", b"Mallory_Example")
        self.assertNotEqual(tampered, body)
        st, _ = self.post("/hook/oss-hook", tampered, h)
        self.assertEqual(st, 401)
        # control: the original with the same header passes
        st, _ = self.post("/hook/oss-hook", body, h)
        self.assertEqual(st, 202)

    def test_missing_or_unprefixed_header_refused(self):
        body = _fix("github-pr-merged.webhook.json")
        st, _ = self.post("/hook/oss-hook", body, {"X-GitHub-Event": "pull_request"})
        self.assertEqual(st, 401)
        h = self.gh_headers(body)
        h["X-Hub-Signature-256"] = h["X-Hub-Signature-256"][len("sha256="):]
        st, _ = self.post("/hook/oss-hook", body, h)
        self.assertEqual(st, 401)
        self.assertEqual(self.spool_lines("oss-hook"), [])

    def test_header_name_case_insensitive(self):
        body = _fix("github-pr-merged.webhook.json")
        st, _ = self.post("/hook/oss-hook", body, self.gh_headers(body, name="x-hub-signature-256"))
        self.assertEqual(st, 202)

    def test_refusal_never_echoes_secret_or_digest(self):
        body = _fix("github-pr-merged.webhook.json")
        st, data = self.post("/hook/oss-hook", body, self.gh_headers(body, secret=OTHER))
        self.assertEqual(st, 401)
        self.assertNotIn(SECRET, data)
        self.assertNotIn(WH.sign(SECRET, body)[7:], data)

    def test_generic_hmac_route(self):
        body = _fix("generic-events.jsonl")
        st, data = self.post("/hook/acme-ci", body, {"X-Acme-Signature": WH.sign(SECRET, body)})
        self.assertEqual(st, 202, data)
        rows = self.spool_lines("acme-ci")
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(C.validate_event(r), [])
            self.assertEqual(r["source"], "acme-ci")
        # the sender tried to self-route as an ask to ROUTE with score 3; the pipeline fields were reset
        self.assertEqual((rows[0]["kind"], rows[0]["to"], rows[0]["score"]), (C.KIND_FYI, C.DECIDE, C.SCORE_FYI))

    def test_generic_route_bad_signature_refused(self):
        body = _fix("generic-events.jsonl")
        st, _ = self.post("/hook/acme-ci", body, {"X-Acme-Signature": WH.sign(OTHER, body)})
        self.assertEqual(st, 401)
        # a GitHub-style header does not satisfy a route that names its own header
        st, _ = self.post("/hook/acme-ci", body, {"X-Hub-Signature-256": WH.sign(SECRET, body)})
        self.assertEqual(st, 401)
        self.assertEqual(self.spool_lines("acme-ci"), [])

    def test_ping_accepted_without_events(self):
        body = b'{"zen": "Keep it logically awesome.", "hook_id": 1}'
        st, data = self.post("/hook/oss-hook", body, self.gh_headers(body, event="ping"))
        self.assertEqual(st, 200, data)
        self.assertEqual(self.spool_lines("oss-hook"), [])
        st, _ = self.post("/hook/oss-hook", body, self.gh_headers(body, secret=OTHER, event="ping"))
        self.assertEqual(st, 401)

    def test_http_shape(self):
        body = _fix("github-pr-merged.webhook.json")
        self.assertEqual(self.post("/hook/oss-hook", None, method="GET")[0], 405)
        self.assertEqual(self.post("/hook/nope", body, self.gh_headers(body))[0], 404)
        self.assertEqual(self.post("/hook/oss", body, self.gh_headers(body))[0], 404)  # a polled source is no route
        self.assertEqual(self.raw_status("POST /hook/oss-hook HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n"
                                         % (WH.MAX_BODY + 1)), 413)  # refused on the header, before the body
        self.assertEqual(self.raw_status("POST /hook/oss-hook HTTP/1.1\r\nHost: x\r\n\r\n"), 411)
        # control: the same route answers 202 to a proper request
        self.assertEqual(self.post("/hook/oss-hook", body, self.gh_headers(body))[0], 202)


class TestDedupe(Base):
    def _seen(self, path):
        db = SeenDB(path)
        self.addCleanup(db.db.close)
        return db

    def _deliver(self, r, body):
        st, obj = r.handle("POST", "/hook/oss-hook", {"X-Hub-Signature-256": WH.sign(SECRET, body),
                                                       "X-GitHub-Event": "pull_request"}, body)
        self.assertEqual(st, 202, obj)

    def _engine_cycle(self, seen, cursor=0):
        """What the engine does per tick for these two sources: poll each, ingest through seen.db."""
        cfg = _config()
        hook = C.source_cfg_with_me(cfg, cfg["sources"][1])
        with mock.patch.dict(os.environ, {"FLEET_STATE": self.state}):
            ev, cursor, nxt = C.load_source("webhook")(hook, {}, {}).poll(cursor)
        self.assertIsNone(nxt)
        seen.ingest(ev)
        gh = C.source_cfg_with_me(cfg, cfg["sources"][0])
        ev2, _, _ = C.load_source("github")(gh, {"token": "x"}, {}).poll(None)
        seen.ingest(ev2)
        return ev, ev2, cursor

    def test_webhook_plus_next_poll_is_one_event(self):
        r = self.rx()
        self._deliver(r, _fix("github-pr-merged.webhook.json"))
        seen = self._seen(os.path.join(self.state, "seen.db"))
        from_hook, from_poll, _ = self._engine_cycle(seen)
        self.assertEqual((len(from_hook), len(from_poll)), (1, 1))  # both paths saw the change
        self.assertEqual(from_hook[0].eid, from_poll[0].eid)
        self.assertEqual(len(seen.out), 1)
        self.assertEqual(seen.out[0].type, "pr.merged")

    def test_control_different_change_is_two_events(self):
        r = self.rx()
        doc = json.loads(_fix("github-pr-merged.webhook.json"))
        pr = doc["pull_request"]
        pr.update(merged=False, merged_at=None, closed_at=None, state="open", updated_at="2026-11-19T12:00:00Z")
        self._deliver(r, json.dumps(doc).encode("utf-8"))
        seen = self._seen(os.path.join(self.state, "seen.db"))
        self._engine_cycle(seen)
        self.assertEqual(len(seen.out), 2)

    def test_redelivery_is_one_event(self):
        r = self.rx()
        body = _fix("github-pr-merged.webhook.json")
        self._deliver(r, body)
        self._deliver(r, body)  # GitHub "Redeliver"
        seen = self._seen(os.path.join(self.state, "seen.db"))
        from_hook, _, _ = self._engine_cycle(seen)
        self.assertEqual(len(from_hook), 2)
        self.assertEqual(len(seen.out), 1)

    def test_drain_cursor_does_not_replay(self):
        r = self.rx()
        self._deliver(r, _fix("github-pr-merged.webhook.json"))
        seen = self._seen(":memory:")
        ev, _, cur = self._engine_cycle(seen)
        self.assertEqual(len(ev), 1)
        ev, _, cur2 = self._engine_cycle(seen, cur)
        self.assertEqual((ev, cur2), ([], cur))
        # control: from offset 0 the same line comes back
        ev, _, _ = self._engine_cycle(seen, 0)
        self.assertEqual(len(ev), 1)


class TestBind(Base):
    REFUSED = ("0.0.0.0", "", "::", "192.168.1.10", "10.0.0.1", "example.com", "[::]")

    def test_non_loopback_refused_before_socket(self):
        with mock.patch.object(WH.http.server.ThreadingHTTPServer, "server_bind",
                               side_effect=AssertionError("socket bound")):
            for h in self.REFUSED:
                with self.subTest(bind=h):
                    with self.assertRaises(WH.BindRefused):
                        self.rx(bind=h).start()

    def test_config_with_non_loopback_bind_refused(self):
        for h in self.REFUSED:
            with self.subTest(bind=h):
                routes, errs = WH.routes_from_config(_config(bind=h))
                self.assertTrue(any("loopback" in e for e in errs), errs)
                with self.assertRaises(ValueError):
                    self.rx(_config(bind=h))
        self.assertEqual(WH.routes_from_config(_config())[1], [])  # control

    def test_loopback_binds(self):
        r = self.rx()
        host, port = r.start()
        self.addCleanup(r.stop)
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)
        self.assertEqual(WH.check_bind("localhost"), "127.0.0.1")
        self.assertEqual(WH.check_bind("127.0.0.2"), "127.0.0.2")
        self.assertEqual(WH.check_bind("::1"), "::1")

    def test_cli_refuses_non_loopback(self):
        p = os.path.join(self.state, "watch.json")
        with open(p, "w") as f:
            json.dump(_config(), f)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FLEET_STATE": self.state}), contextlib.redirect_stderr(err):
            rc = WH.main(["serve", "--config", p, "--bind", "0.0.0.0"])
        self.assertEqual(rc, 2)
        self.assertIn("loopback", err.getvalue())


class TestConfig(Base):
    def test_literal_secret_refused(self):
        routes, errs = WH.routes_from_config(_config(secret_ref="hunter2-literal"))
        self.assertTrue(any("oss-hook" in e and "reference" in e for e in errs), errs)
        self.assertFalse(any("hunter2" in e for e in errs))
        self.assertTrue(C.validate_config(_config(secret_ref="hunter2-literal")))  # the contract refuses it too
        self.assertEqual(C.validate_config(_config()), [])  # control

    def test_unresolvable_secret_fails_closed(self):
        r = self.rx(_config(secret_ref="env:UNSET_HOOK_SECRET"))
        body = _fix("github-pr-merged.webhook.json")
        st, _ = r.handle("POST", "/hook/oss-hook", {"X-Hub-Signature-256": WH.sign(SECRET, body)}, body)
        self.assertEqual(st, 503)
        self.assertEqual(self.spool_lines("oss-hook"), [])

    def test_resolve_secret_env(self):
        with mock.patch.dict(os.environ, {"FD_HOOK_TEST": "abc"}):
            self.assertEqual(WH.resolve_secret("env:FD_HOOK_TEST"), "abc")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                WH.resolve_secret("env:FD_HOOK_TEST")
        with self.assertRaises(ValueError):
            WH.resolve_secret("plainvalue")
        self.assertEqual(WH.resolve_secret("cmd:printf 'line1\\nline2'"), "line1")

    def test_bad_for_and_webhook_to_webhook(self):
        extra = [{"name": "x", "kind": "webhook", "for": "missing", "auth": {"type": "hmac", "secret": "env:A"},
                  "port": 0},
                 {"name": "y", "kind": "webhook", "for": "oss-hook", "auth": {"type": "hmac", "secret": "env:A"},
                  "port": 0}]
        _, errs = WH.routes_from_config(_config(extra=extra))
        self.assertTrue(any("names no source" in e for e in errs), errs)
        self.assertTrue(any("polled source" in e for e in errs), errs)

    def test_target_without_mapping_is_501(self):
        extra = [{"name": "acme-wiki", "kind": "confluence", "base": "https://acme.example",
                  "auth": {"type": "bearer", "token": "env:T"}},
                 {"name": "jira-hook", "kind": "webhook", "for": "acme-wiki", "signature": "hmac",
                  "auth": {"type": "hmac", "secret": "env:ACME_CI_SECRET"}, "port": 0}]
        r = self.rx(_config(extra=extra))
        body = b'{"webhookEvent": "jira:issue_updated"}'
        st, _ = r.handle("POST", "/hook/jira-hook", {"X-Signature-256": WH.sign(SECRET, body)}, body)
        self.assertEqual(st, 501)
        self.assertEqual(self.spool_lines("jira-hook"), [])


class TestVerifierRegistry(Base):
    def test_registered_verifier_is_used(self):
        calls = []

        def verify_acme(secret, headers, body, route):
            calls.append(route["name"])
            if WH._header(headers, "X-Acme-Token") != secret:
                raise C.SourceError("token mismatch", status=401)

        with mock.patch.dict(WH.VERIFIERS):
            WH.register_verifier("acme-token", verify_acme)
            extra = [{"name": "acme-tok", "kind": "webhook", "signature": "acme-token",
                      "auth": {"type": "hmac", "secret": "env:ACME_CI_SECRET"}, "port": 0}]
            r = self.rx(_config(extra=extra))
            body = _fix("generic-events.jsonl")
            self.assertEqual(r.handle("POST", "/hook/acme-tok", {"X-Acme-Token": OTHER}, body)[0], 401)
            self.assertEqual(r.handle("POST", "/hook/acme-tok", {"X-Acme-Token": SECRET}, body)[0], 202)
            self.assertEqual(calls, ["acme-tok", "acme-tok"])
            with self.assertRaises(ValueError):
                WH.register_verifier("github", verify_acme)
        # control: once unregistered, the config naming it is refused
        _, errs = WH.routes_from_config(_config(extra=extra))
        self.assertTrue(any("acme-tok" in e and "signature" in e for e in errs), errs)


class TestAdapterRegistersAtImport(Base):
    def test_verifier_registered_by_adapter_import(self):
        def verify_acme(secret, headers, body, route):
            if WH._header(headers, "X-Acme-Token") != secret:
                raise C.SourceError("token mismatch", status=401)

        class AcmeSource(C.Source):
            kind = "bitbucket"

            def accept_webhook(self, headers, body):
                return _generic_from(self.cfg["name"], body)

        def fake_load(kind):  # what importing watch_sources.bitbucket would do
            if kind == "bitbucket":
                WH.register_verifier("acme-token", verify_acme)
                return AcmeSource
            return _load_source(kind)

        route = {"name": "bb-hook", "kind": "webhook", "for": "bb", "signature": "acme-token",
                 "auth": {"type": "hmac", "secret": "env:ACME_CI_SECRET"}, "port": 0}
        target = {"name": "bb", "kind": "bitbucket", "repos": ["acme/widget"]}
        with mock.patch.dict(WH.VERIFIERS), mock.patch.object(C, "load_source", side_effect=fake_load):
            # control first: without `for`, nothing imports the adapter, so the name is unknown
            bare = dict(route)
            del bare["for"]
            _, errs = WH.routes_from_config(_config(extra=[target, bare]))
            self.assertTrue(any("bb-hook" in e and "signature" in e for e in errs), errs)
            r = self.rx(_config(extra=[target, route]))  # no manual register_verifier call
            body = _fix("generic-events.jsonl")
            self.assertEqual(r.handle("POST", "/hook/bb-hook", {"X-Acme-Token": OTHER}, body)[0], 401)
            st, obj = r.handle("POST", "/hook/bb-hook", {"X-Acme-Token": SECRET}, body)
            self.assertEqual(st, 202, obj)


def _generic_from(source, body):
    out = []
    for ln in body.decode().splitlines():
        d = json.loads(ln)
        d = {k: v for k, v in d.items() if k not in WH.PIPELINE_FIELDS}
        d.update(source=source, sourceKind="bitbucket")
        out.append(C.Event.from_dict(d))
    return out


class TestBasicAuth(Base):
    def _cfg(self, user="ado-hooks"):
        extra = [{"name": "boards", "kind": "ado", "org": "acme", "project": "PROJ",
                  "auth": {"type": "pat", "token": "env:ADO_PAT"}},
                 {"name": "ado-hook", "kind": "webhook", "signature": "basic",
                  "auth": {"type": "basic", "user": user, "secret": "env:ACME_CI_SECRET"}, "port": 0}]
        return _config(extra=extra)

    def _auth(self, user, pw):
        import base64
        return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()}

    def test_basic_good_accepted_bad_refused(self):
        r = self.rx(self._cfg())
        body = _fix("generic-events.jsonl")
        self.assertEqual(r.handle("POST", "/hook/ado-hook", self._auth("ado-hooks", OTHER), body)[0], 401)
        self.assertEqual(r.handle("POST", "/hook/ado-hook", self._auth("someone-else", SECRET), body)[0], 401)
        self.assertEqual(r.handle("POST", "/hook/ado-hook", {}, body)[0], 401)  # no Authorization header
        self.assertEqual(r.handle("POST", "/hook/ado-hook", {"Authorization": "Basic !!notb64"}, body)[0], 401)
        self.assertEqual(r.handle("POST", "/hook/ado-hook", {"Authorization": "Bearer " + SECRET}, body)[0], 401)
        self.assertEqual(self.spool_lines("ado-hook"), [])
        # control: the right user and secret pass
        self.assertEqual(r.handle("POST", "/hook/ado-hook", self._auth("ado-hooks", SECRET), body)[0], 202)
        self.assertEqual(len(self.spool_lines("ado-hook")), 2)

    def test_basic_user_by_reference(self):
        cfg = self._cfg(user="env:ADO_HOOK_USER")
        res = lambda ref: "ado-hooks" if ref == "env:ADO_HOOK_USER" else _resolver(ref)  # noqa: E731
        r = self.rx(cfg, resolver=res)
        body = _fix("generic-events.jsonl")
        self.assertEqual(r.handle("POST", "/hook/ado-hook", self._auth("ado-hooks", SECRET), body)[0], 202)
        self.assertEqual(r.handle("POST", "/hook/ado-hook", self._auth("env:ADO_HOOK_USER", SECRET), body)[0], 401)

    def test_basic_needs_basic_auth_block(self):
        cfg = self._cfg()
        cfg["sources"][-1]["auth"]["type"] = "hmac"
        _, errs = WH.routes_from_config(cfg)
        self.assertTrue(any("ado-hook" in e and "basic" in e for e in errs), errs)
        self.assertEqual(WH.routes_from_config(self._cfg())[1], [])  # control


GH_HOOK_FIX = os.path.join(ROOT, "test", "fixtures", "watch", "github", "webhook_pull_request_merged.json")
GH_POLL_FIX = os.path.join(ROOT, "test", "fixtures", "watch", "github", "events_new_200.json")
HAVE_GITHUB = all(os.path.exists(p) for p in (os.path.join(LIB, "watch_sources", "github.py"), GH_HOOK_FIX,
                                              GH_POLL_FIX))


@unittest.skipUnless(HAVE_GITHUB, "needs fd-p2-github's adapter and fixtures (runs once both branches are on integ)")
class TestRealGithubDedupe(unittest.TestCase):
    """The acceptance clause with no stand-in adapter: the real github Source normalizes the webhook, and its own
    poll mapping (map_event over the recorded /events row of the same merge) must produce the same eid."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(GH_HOOK_FIX) as f:
            fx = json.load(f)
        self.secret = fx["secret"]
        self.body = (fx["body"] if isinstance(fx["body"], str) else json.dumps(fx["body"])).encode("utf-8")
        self.headers = dict(fx["headers"])
        self.cfg = {"me": {"names": [], "ids": {}}, "sources": [
            {"name": "oss", "kind": "github", "repos": ["acme/widget"], "auth": {"type": "bearer", "token": "env:T"}},
            {"name": "oss-hook", "kind": "webhook", "for": "oss", "signature": "github",
             "auth": {"type": "hmac", "secret": "env:S"}, "port": 0}]}
        self.rx = WH.Receiver(self.cfg, self.tmp.name, resolver=lambda ref: self.secret, log=lambda line: None)

    def _polled_merge(self):
        with open(GH_POLL_FIX) as f:
            rows = json.load(f)["body"]
        gh = C.load_source("github")(C.source_cfg_with_me(self.cfg, self.cfg["sources"][0]), {}, {})
        out = [gh.map_event(r) for r in rows]
        return [e for e in out if e is not None and e.type == "pr.merged"]

    def _drain(self):
        with mock.patch.dict(os.environ, {"FLEET_STATE": self.tmp.name}):
            return WH.WebhookSource({"name": "oss-hook"}, {}, {}).poll(0)[0]

    def test_real_adapter_webhook_plus_poll_is_one_event(self):
        h = dict(self.headers, **{"X-Hub-Signature-256": WH.sign(self.secret, self.body)})
        st, obj = self.rx.handle("POST", "/hook/oss-hook", h, self.body)
        self.assertEqual(st, 202, obj)
        hook = self._drain()
        polled = self._polled_merge()
        self.assertEqual((len(hook), len(polled)), (1, 1))
        seen = SeenDB(":memory:")
        self.addCleanup(seen.db.close)
        seen.ingest(hook)
        seen.ingest(polled)
        self.assertEqual(len(seen.out), 1)

    def test_real_adapter_bad_signature_refused(self):
        h = dict(self.headers, **{"X-Hub-Signature-256": WH.sign("not-the-secret", self.body)})
        self.assertEqual(self.rx.handle("POST", "/hook/oss-hook", h, self.body)[0], 401)
        self.assertEqual(self._drain(), [])


HAVE_ENGINE = os.path.exists(os.path.join(LIB, "fleet_watch.py"))


@unittest.skipUnless(HAVE_GITHUB and HAVE_ENGINE,
                     "needs fd-p2-github and fd-p2-watch-core (runs once both are on integ)")
class TestEngineEndToEnd(TestRealGithubDedupe):
    """No stand-ins: the real receiver, the real webhook Source drained by the real Engine (its seen.db), then the
    github source's catch-up poll of the same merge, mapped by the real adapter's map_event."""

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("FLEET_STATE", "FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN")}
        os.environ["FLEET_STATE"] = self.tmp.name
        for k in ("FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN"):
            os.environ.pop(k, None)
        self.addCleanup(self._restore)
        import importlib
        self.FW = importlib.import_module("fleet_watch")
        self.cfg["sources"][1]["pollSec"] = 60
        self.cfg["filters"] = [{"kind": "fyi", "when": {"any": True}}]  # without a rule the engine drops everything
        test = self

        class RecordedGithub(C.load_source("github")):
            """The real adapter; only the HTTP fetch is replaced by the recorded /events rows."""
            rows = None

            def poll(self, cursor):
                return [e for e in (self.map_event(r) for r in RecordedGithub.rows) if e is not None], cursor, None

        with open(GH_POLL_FIX) as f:
            RecordedGithub.rows = json.load(f)["body"]
        self.Recorded = RecordedGithub
        self.test = test

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _engine(self, only):
        self.FW.Engine(self.tmp.name, self.cfg, sources={"github": self.Recorded},
                       env={"S": self.secret, "T": "unused-token"}).run_once(force=True, only=only)

    def _rows(self):
        p = C.state_paths(self.tmp.name)["events"]
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def _merge_rows(self):
        return [r for r in self._rows() if r["ref"] == "acme/widget#12" and r["type"] == "pr.merged"]

    def test_engine_webhook_then_poll_is_one_event(self):
        h = dict(self.headers, **{"X-Hub-Signature-256": WH.sign(self.secret, self.body)})
        self.assertEqual(self.rx.handle("POST", "/hook/oss-hook", h, self.body)[0], 202)
        self._engine(["oss-hook"])
        first = self._merge_rows()
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["source"], first[0]["sourceKind"]), ("oss", "github"))
        self._engine(["oss"])  # the catch-up poll of the same change
        self.assertEqual(len(self._merge_rows()), 1)
        self.assertEqual(self._merge_rows()[0]["eid"], first[0]["eid"])

    def test_engine_control_poll_alone_writes_the_merge(self):
        # control: with no webhook, the poll itself writes the merge, so the dedupe above is what held it to one
        self._engine(["oss"])
        self.assertEqual(len(self._merge_rows()), 1)
        self._engine(["oss-hook"])  # an empty spool adds nothing
        self.assertEqual(len(self._merge_rows()), 1)


class TestSpool(Base):
    def _ev(self, i):
        return C.Event(eid="command:x:PROJ-%d:2026-11-20T10:00:00Z:t" % i, source="acme-ci", sourceKind="command",
                       at="2026-11-20T10:00:00Z", type="t", ref="PROJ-%d" % i)

    def test_limit_and_partial_line(self):
        WH.spool_append(self.state, "acme-ci", [self._ev(i) for i in range(5)])
        ev, cur = WH.spool_drain(self.state, "acme-ci", 0, 3)
        self.assertEqual([e.ref for e in ev], ["PROJ-0", "PROJ-1", "PROJ-2"])
        with open(WH.spool_path(self.state, "acme-ci"), "a") as f:
            f.write('{"eid": "half')  # a writer mid-line
        ev, cur = WH.spool_drain(self.state, "acme-ci", cur, 10)
        self.assertEqual([e.ref for e in ev], ["PROJ-3", "PROJ-4"])
        self.assertEqual(WH.spool_drain(self.state, "acme-ci", cur, 10), ([], cur))

    def test_rotate_after_full_drain(self):
        with mock.patch.object(WH, "SPOOL_ROTATE_BYTES", 10):
            WH.spool_append(self.state, "acme-ci", [self._ev(1)])
            ev, cur = WH.spool_drain(self.state, "acme-ci", 0, 10)
            self.assertEqual((len(ev), cur), (1, 0))
            self.assertEqual(os.path.getsize(WH.spool_path(self.state, "acme-ci")), 0)

    def test_spool_mode_0600(self):
        WH.spool_append(self.state, "acme-ci", [self._ev(1)])
        self.assertEqual(os.stat(WH.spool_path(self.state, "acme-ci")).st_mode & 0o777, 0o600)

    def test_route_name_cannot_escape(self):
        for bad in ("../x", ".hidden", "", "a/b"):
            with self.assertRaises(ValueError):
                WH.spool_path(self.state, bad)


if __name__ == "__main__":
    unittest.main()


class TestCLIServe(unittest.TestCase):
    """`fleet watch webhook serve` as a real process: webhook.py runs as __main__, and the verifiers gitlab.py and
    bitbucket.py register at import must reach the CLI's receiver (one VERIFIERS dict, not a __main__ copy)."""

    GL_SECRET = "gl-hook-secret-example"
    BB_SECRET = "bb-hook-secret-example"

    def setUp(self):
        import socket
        import subprocess
        import time
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        cfg = {"me": {"names": ["Human_Example"], "ids": {"acme-forge": "human-example", "acme-bb": "acct-me"}},
               "sources": [
                   {"name": "acme-forge", "kind": "gitlab", "flavor": "gitlab", "base": "https://acme.example.com",
                    "repos": ["acme/widget"], "branches": ["main"],
                    "auth": {"type": "bearer", "token": "env:ACME_FORGE_TOKEN"}},
                   {"name": "forge-hook", "kind": "webhook", "for": "acme-forge", "signature": "gitlab",
                    "auth": {"type": "hmac", "secret": "env:ACME_FORGE_HOOK"}, "pollSec": 60},
                   {"name": "acme-bb", "kind": "bitbucket", "repos": ["acme/widget"], "branches": ["main"],
                    "auth": {"type": "bearer", "token": "env:ACME_BB_TOKEN"}},
                   {"name": "bb-hook", "kind": "webhook", "for": "acme-bb", "signature": "bitbucket",
                    "auth": {"type": "hmac", "secret": "env:ACME_BB_HOOK"}, "pollSec": 60}]}
        self.cfgpath = os.path.join(self.tmp.name, "watch.json")
        with open(self.cfgpath, "w") as f:
            json.dump(cfg, f)
        env = dict(os.environ, FLEET_STATE=self.tmp.name, ACME_FORGE_HOOK=self.GL_SECRET, ACME_BB_HOOK=self.BB_SECRET)
        self.proc = subprocess.Popen(["bash", os.path.join(ROOT, "bin", "fleet"), "watch", "webhook", "serve",
                                      "--config", self.cfgpath, "--port", str(self.port)],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop)
        line = self.proc.stdout.readline()
        if not line:
            self.fail("serve did not start: rc=%s %s" % (self.proc.wait(5), self.proc.stderr.read()))
        self.assertIn("forge-hook", line)
        self.assertIn("bb-hook", line)
        time.sleep(0.05)

    def _stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(5)
        for p in (self.proc.stdout, self.proc.stderr):
            p.close()

    def post(self, name, headers, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request("POST", "/hook/" + name, body=body, headers=dict(headers, **{"Content-Length": str(len(body))}))
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    def test_gitlab_route_wrong_token_401_right_token_202(self):
        with open(os.path.join(ROOT, "test", "fixtures", "watch", "gitlab", "gitlab_hook_merge.json")) as f:
            fx = json.load(f)
        body = json.dumps(fx["body"]).encode()
        self.assertEqual(self.post("forge-hook", dict(fx["headers"], **{"X-Gitlab-Token": "wrong"}), body), 401)
        self.assertEqual(self.post("forge-hook", dict(fx["headers"], **{"X-Gitlab-Token": self.GL_SECRET}), body), 202)

    def test_bitbucket_route_wrong_signature_401_right_signature_202(self):
        import hashlib
        import hmac
        with open(os.path.join(ROOT, "test", "fixtures", "watch", "bitbucket", "webhook",
                               "pullrequest-fulfilled.json"), "rb") as f:
            body = f.read()
        good = "sha256=" + hmac.new(self.BB_SECRET.encode(), body, hashlib.sha256).hexdigest()
        bad = "sha256=" + hmac.new(b"not-the-secret", body, hashlib.sha256).hexdigest()
        h = {"X-Event-Key": "pullrequest:fulfilled", "Content-Type": "application/json"}
        self.assertEqual(self.post("bb-hook", dict(h, **{"X-Hub-Signature": bad}), body), 401)
        self.assertEqual(self.post("bb-hook", dict(h, **{"X-Hub-Signature": good}), body), 202)
