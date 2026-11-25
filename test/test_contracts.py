"""Tests for the four-desks contracts: lib/fleet_watch_contract.py and lib/fleet_probe_contract.py, plus the
`fleet watch|learn|probe` dispatch. Every positive check has a control that must fail.
Run: python3 -m unittest test/test_contracts.py
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _load("fleet_watch_contract", "lib/fleet_watch_contract.py")
P = _load("fleet_probe_contract", "lib/fleet_probe_contract.py")

SAMPLE_CONFIG = """{
  // comment lines are allowed
  "me": {"names": ["Human_Example"], "ids": {"acme-tracker": "acct-123", "github": "human-example"}},
  "defaults": {"pollSec": 900, "maxEventsPerPoll": 200, "quoteChars": 600, "batchSec": 30},
  "sources": [
    {"name": "acme-tracker", "kind": "jira", "base": "https://acme.example",
     "auth": {"type": "basic", "user": "env:ACME_TRACKER_USER", "token": "keychain:acme-tracker-token"},
     "query": "project = PROJ", "pollSec": 1800},
    {"name": "acme-wiki", "kind": "confluence", "base": "https://acme.example/wiki",
     "auth": {"type": "bearer", "token": "cmd:my-secret-tool get acme-wiki"}, "spaces": ["ENG"]},
    {"name": "boards", "kind": "ado", "org": "acme", "project": "PROJ", "auth": {"type": "pat", "token": "env:ADO_PAT"}},
    {"name": "oss", "kind": "github", "repos": ["acme/widget"], "auth": {"type": "bearer", "token": "cmd:gh auth token"}},
    {"name": "replay", "kind": "command", "argv": ["cat", "events.jsonl"], "timeoutSec": 10},
    {"name": "call", "kind": "call", "listener": "$FLEET_STATE/listener.json"}
  ],
  "filters": [
    {"drop": {"actor": "me"}},
    {"drop": {"actorMatches": "\\\\[bot\\\\]$"}},
    {"kind": "ask", "when": {"tags": ["assigned-to-me", "review-requested", "mention"]}},
    {"kind": "task-candidate", "when": {"type": ["build.failed"], "branchOwner": "me"}},
    {"kind": "fyi", "when": {"any": true}}
  ],
  "quiet": {"hours": "19:00-08:00", "decideHolds": true},
  "learn": {"minScore": 1},
  "probe": {"auth": {"type": "bearer", "token": "env:PROBE_READ_TOKEN"}}
}"""


def sample_event(**kw):
    d = {"eid": W.make_eid("jira", "ACME", "PROJ-1", "2026-11-20T10:02:11Z", "status"), "source": "acme-tracker",
         "sourceKind": "jira", "at": "2026-11-20T10:02:11Z", "type": "issue.transition", "ref": "PROJ-1",
         "url": "https://acme.example/browse/PROJ-1", "actor": "Alice_Example", "title": "Retry the upload on 503",
         "quote": "Status: In Review -> Done", "fields": {"from": "In Review", "to": "Done"}, "tags": ["watcher"],
         "kind": "fyi", "to": "DECIDE", "origin": "outside", "score": 2}
    d.update(kw)
    return d


class WatchContract(unittest.TestCase):
    def test_registry_names(self):
        self.assertEqual(set(W.SOURCES), {"call", "command", "github", "jira", "confluence", "ado", "bitbucket",
                                          "gitlab", "webhook", "whosaid", "linear"})
        self.assertTrue(all(v.startswith("watch_sources.") for v in W.SOURCES.values()))
        self.assertTrue(os.path.isdir(os.path.join(ROOT, "lib", "watch_sources")))
        for k in W.SOURCES:
            self.assertTrue(os.path.isdir(os.path.join(ROOT, "test", "fixtures", "watch", k)), k)
        with self.assertRaises(KeyError):  # control
            W.load_source("nope")

    def test_webhook_sources_are_registered(self):
        self.assertTrue(set(W.WEBHOOK_SOURCES) <= set(W.SOURCES))
        self.assertIn("ado", W.WEBHOOK_SOURCES)
        for k in W.WEBHOOK_SOURCES:  # each listed kind overrides accept_webhook
            self.assertIsNot(W.load_source(k).accept_webhook, W.Source.accept_webhook, k)

    def test_routing_classes(self):
        self.assertEqual(W.ROUTING_CLASSES, ("ask", "task-candidate", "fyi", "learn-item"))
        self.assertEqual(W.DECIDE, "DECIDE")

    def test_eid_and_hash(self):
        self.assertEqual(W.make_eid("jira", "ACME", "PROJ-1", "2026-11-20T10:02:11Z", "status"),
                         "jira:ACME:PROJ-1:2026-11-20T10:02:11Z:status")
        long = W.make_eid("github", "x" * 250, "acme/widget#1", "2026-11-20T10:02:11Z", "pr")
        self.assertLessEqual(len(long), W.EID_MAX)
        self.assertTrue(long.startswith("github:h:"))
        self.assertEqual(long, W.make_eid("github", "x" * 250, "acme/widget#1", "2026-11-20T10:02:11Z", "pr"))

    def test_quote_cap(self):
        self.assertEqual(len(W.cap_quote("a" * 1000)), 600)
        self.assertEqual(W.cap_quote("short"), "short")

    def test_event_roundtrip_validates(self):
        d = sample_event()
        self.assertEqual(W.validate_event(d), [])
        e = W.Event.from_dict(d)
        self.assertEqual(W.Event.from_dict(json.loads(e.to_json())), e)
        self.assertEqual(W.validate_event(e.to_dict()), [])
        self.assertEqual(e.burst_key(), ("jira", "PROJ-1", "issue.transition"))
        self.assertTrue(e.is_learn_item(1))
        self.assertFalse(e.is_learn_item(3))

    def test_event_controls_fail(self):
        self.assertTrue(W.validate_event(sample_event(to="ROUTE")))
        self.assertTrue(W.validate_event(sample_event(kind="learn-item")))
        self.assertTrue(W.validate_event(sample_event(sourceKind="slack")))
        self.assertTrue(W.validate_event(sample_event(quote="q" * 601)))
        self.assertTrue(W.validate_event(sample_event(at="yesterday")))
        self.assertTrue(W.validate_event(sample_event(token="env:X")))
        d = sample_event()
        del d["ref"]
        self.assertTrue(W.validate_event(d))

    def test_scores(self):
        self.assertEqual(W.score_for("ask", "pr.review_requested"), 3)
        self.assertEqual(W.score_for("fyi", "pr.merged"), 2)
        self.assertEqual(W.score_for("fyi", "pr.comment"), 1)
        self.assertEqual(W.score_for("drop", "pr.merged"), 0)

    def test_config_sample_validates(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(SAMPLE_CONFIG)
        try:
            cfg = W.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(W.validate_config(cfg), [])
        s = W.source_settings(cfg, cfg["sources"][0])
        self.assertEqual(s["pollSec"], 1800)
        self.assertEqual(W.source_settings(cfg, cfg["sources"][1])["pollSec"], 900)

    def _cfg(self):
        return json.loads(W.strip_jsonc(SAMPLE_CONFIG))

    def test_literal_token_refused_and_not_echoed(self):
        cfg = self._cfg()
        cfg["sources"][3]["auth"]["token"] = "ghp_LITERALVALUE123"
        errs = W.validate_config(cfg)
        self.assertTrue(any("literal" in e for e in errs))
        self.assertFalse(any("LITERALVALUE" in e for e in errs))
        cfg = self._cfg()
        cfg["probe"]["auth"]["token"] = "plain"
        self.assertTrue(W.validate_config(cfg))
        cfg = self._cfg()
        cfg["sources"][0]["token"] = "env:X"  # outside auth
        self.assertTrue(W.validate_config(cfg))

    def test_credential_refs(self):
        for ok in ("env:ADO_PAT", "keychain:acme-tracker-token", "cmd:gh auth token"):
            self.assertTrue(W.is_credential_ref(ok), ok)
            self.assertIn(W.parse_ref(ok)[0], W.CRED_SCHEMES)
        for bad in ("ghp_abc", "env:", "env:1BAD", "file:/x", "", None, 12):
            self.assertFalse(W.is_credential_ref(bad), bad)

    def test_config_controls_fail(self):
        for mut in (lambda c: c["sources"][0].update(kind="slack"),
                    lambda c: c["sources"][3].pop("repos"),
                    lambda c: c["sources"][1].update(name="acme-tracker"),
                    lambda c: c["sources"][0].update(pollSec=10),
                    lambda c: c["filters"].append({"kind": "urgent", "when": {"any": True}}),
                    lambda c: c["filters"].append({"drop": {"colour": "red"}}),
                    lambda c: c.update(quiet={"hours": "7pm-8am"}),
                    lambda c: c.update(learn={"minScore": 9}),
                    lambda c: c.update(sources=[])):
            c = self._cfg()
            mut(c)
            self.assertTrue(W.validate_config(c), mut)

    def test_source_base(self):
        s = W.Source({"name": "x"}, None, None)
        self.assertEqual(set(s.health()), {"lastOk", "lastError", "failStreak", "rateRemaining"})
        with self.assertRaises(NotImplementedError):
            s.poll(None)
        with self.assertRaises(NotImplementedError):
            s.accept_webhook({}, b"")
        err = W.SourceError("429", retry_after=60, status=429)
        self.assertEqual((err.retry_after, err.status), (60, 429))

    def test_snapshot_shape(self):
        snap = W.empty_snapshot("2026-11-20T10:02:11Z")
        self.assertEqual(W.validate_snapshot(snap), [])
        snap["sources"].append({k: None for k in W.SNAPSHOT_SOURCE_KEYS})
        snap["recent"].append(sample_event())
        self.assertEqual(W.validate_snapshot(snap), [])
        bad = W.empty_snapshot()
        bad["sources"].append({"name": "x", "auth": {}})
        self.assertTrue(W.validate_snapshot(bad))
        bad = W.empty_snapshot()
        bad["recent"].append(sample_event(to="ROUTE"))
        self.assertTrue(W.validate_snapshot(bad))

    def test_next_poll_is_a_floor(self):
        self.assertEqual(W.effective_wait(900, None), 900)
        self.assertEqual(W.effective_wait(900, 1800), 1800)
        self.assertEqual(W.effective_wait(900, 30), 900)  # control: a smaller hint never shortens the wait
        self.assertEqual(W.effective_wait(10, None), W.POLL_FLOOR_SEC)

    def test_me_injection(self):
        cfg = self._cfg()
        jira = W.source_cfg_with_me(cfg, cfg["sources"][0])
        self.assertEqual(jira["me"], {"names": ["Human_Example"], "id": "acct-123"})  # by source name
        oss = W.source_cfg_with_me(cfg, cfg["sources"][3])
        self.assertEqual(oss["me"]["id"], "human-example")  # by kind (github)
        self.assertIsNone(W.source_cfg_with_me(cfg, cfg["sources"][2])["me"]["id"])  # control: no id for ado
        self.assertNotIn("me", cfg["sources"][0])  # the config itself is not mutated

    def test_state_paths(self):
        p = W.state_paths("/tmp/fs")
        self.assertEqual(p["events"], "/tmp/fs/watch/events.jsonl")
        self.assertEqual(p["seen"], "/tmp/fs/watch/seen.db")
        self.assertEqual(p["config"], "/tmp/fs/watch.json")
        self.assertEqual(p["webhook"], "/tmp/fs/watch/webhook")


class ProbeContract(unittest.TestCase):
    def test_targets(self):
        doc = {"targets": [{"target": "acme/widget", "what": "open issues labelled good-first-issue",
                            "source": "oss", "everySec": 3600},
                           {"target": "acme/gadget", "what": "CI on main"}]}
        self.assertEqual(P.validate_targets(doc), [])
        self.assertTrue(P.validate_targets({"targets": [{"target": "acme/widget"}]}))
        self.assertTrue(P.validate_targets({"targets": [doc["targets"][0], doc["targets"][0]]}))
        self.assertTrue(P.validate_targets({"targets": [dict(doc["targets"][1], token="env:X")]}))
        self.assertTrue(P.validate_targets([]))

    def test_rows(self):
        r = P.new_row("p-1", "acme/widget", "PROBE-1", "claude", "2026-11-20T10:00:00Z")
        self.assertEqual(P.validate_row(r), [])
        r.update(endedAt="2026-11-20T10:05:00Z", tokensIn=1200, tokensOut=300, found=True, callId="Q12",
                 status="done")
        self.assertEqual(P.validate_row(r), [])
        self.assertTrue(set(P.ROW_REQUIRED) <= set(r))
        for mut in (dict(cli="vim"), dict(tokensIn=-1), dict(found="yes"), dict(found=False, callId="Q1"),
                    dict(startedAt="now"), dict(status="exploded"), dict(id="../x")):
            self.assertTrue(P.validate_row(dict(r, **mut)), mut)
        d = dict(r)
        del d["tokensOut"]
        self.assertTrue(P.validate_row(d))

    def test_log_rows(self):
        a = P.log_row("fyi-forward", "2026-11-20T10:00:00Z", "acme/widget", relayed_to="ROUTE")
        self.assertEqual(P.validate_log_row(a), [])
        self.assertEqual((a["decidedBy"], a["human"]), ("decide:probe", False))
        n = P.log_row("nuke", "2026-11-20T10:00:00Z", "acme/widget", verbatim="nuke", call_id="Q12")
        self.assertEqual(P.validate_log_row(n), [])
        self.assertTrue(n["human"])
        self.assertTrue(P.validate_log_row(dict(a, human=True)))
        self.assertTrue(P.validate_log_row(dict(n, human=False)))
        self.assertTrue(P.validate_log_row(dict(a, action="merge")))

    def test_nuked_and_paths(self):
        self.assertEqual(P.validate_nuked({"acme/widget": {"nukedAt": "2026-11-20T10:00:00Z", "callId": "Q1"}}), [])
        self.assertTrue(P.validate_nuked({"acme/widget": {}}))
        p = P.state_paths("/tmp/fs")
        self.assertEqual(p["targets"], "/tmp/fs/decide/probes.json")
        self.assertEqual(p["rows"], "/tmp/fs/decide/probes.jsonl")
        self.assertEqual(P.STALL_SEC, 900)
        self.assertEqual(P.CHOICES, ("yes", "no", "nuke"))


class Dispatch(unittest.TestCase):
    def run_fleet(self, *args):
        env = dict(os.environ, FLEET_STATE=tempfile.mkdtemp(prefix="fleet-contracts-"))
        env.pop("FLEET_ROLE", None)
        return subprocess.run([FLEET] + list(args), capture_output=True, text=True, env=env, timeout=30)

    def test_watch_learn_probe_dispatch(self):
        for sub in ("watch", "learn", "probe"):
            h = self.run_fleet(sub, "--help")
            self.assertIn("fleet " + sub, h.stdout, sub)
            self.assertIn("fleet " + sub, self.run_fleet("--help").stdout, sub)
            r = self.run_fleet(sub, "run")
            with open(os.path.join(ROOT, "bin", "fleet-" + sub)) as f:
                placeholder = "not built yet" in f.read()
            if placeholder:  # still the placeholder
                self.assertEqual(r.returncode, 2, sub)
                self.assertIn("not built yet", r.stderr, sub)
        bogus = self.run_fleet("watchx")  # control: an unknown verb is not dispatched
        self.assertEqual(bogus.returncode, 2)
        self.assertNotIn("not built yet", bogus.stderr)


if __name__ == "__main__":
    unittest.main()
