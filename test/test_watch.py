"""Tests for the WATCH core (docs/design/four-desks.md 3.1, 3.3 to 3.7, 6; phase 2 acceptance in section 7):
bin/fleet-watch, lib/fleet_watch.py, lib/fleet_watch_creds.py, lib/watch_sources/command.py and call.py.
Every positive check has a control that must fail. Every test uses a temp FLEET_STATE.
Run: python3 -m unittest test/test_watch.py
"""
import datetime
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FLEET = os.path.join(ROOT, "bin", "fleet")
FIX = os.path.join(ROOT, "test", "fixtures", "watch", "command")
EVENTS = os.path.join(FIX, "events.jsonl")
EXPECTED = json.load(open(os.path.join(FIX, "expected.json")))
SECRET = EXPECTED["secret"]
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(LIB, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import fleet_watch_contract as C  # noqa: E402  (the same module object the engine and sources import)
import fleet_watch as W  # noqa: E402
import fleet_watch_creds as CR  # noqa: E402

FILTERS = [
    {"drop": {"actor": "me"}},
    {"drop": {"actorMatches": "\\[bot\\]$"}},
    {"drop": {"type": ["issue.field:labels"]}},
    {"kind": "ask", "when": {"tags": ["assigned-to-me", "review-requested", "mention"]}},
    {"kind": "task-candidate", "when": {"type": ["build.failed"], "branchOwner": "me"}},
    {"kind": "fyi", "when": {"any": True}},
]


def replay_cfg(**kw):
    cfg = {"me": {"names": ["Human_Example"], "ids": {"replay": "human-example"}},
           "defaults": {"pollSec": 900},
           "sources": [{"name": "replay", "kind": "command", "argv": ["cat", EVENTS], "timeoutSec": 10}],
           "filters": FILTERS, "learn": {"minScore": 1}}
    cfg.update(kw)
    return cfg


def env_for(state, **kw):
    e = dict(os.environ)
    for k in ("FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN", "TMUX", "FLEET_NAME"):
        e.pop(k, None)
    e["FLEET_STATE"] = state
    e.update(kw)
    return e


def fleet(state, *args, **envkw):
    return subprocess.run([FLEET, "watch"] + list(args), env=env_for(state, **envkw), capture_output=True, text=True,
                          timeout=60)


def jsonl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(ln) for ln in open(path) if ln.strip()]


def walk_bytes(root):
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                yield p, fh.read()


class StateCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="watch-test-")
        self._env = {k: os.environ.get(k) for k in ("FLEET_STATE", "FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN")}
        os.environ["FLEET_STATE"] = self.state
        for k in ("FLEET_DECIDE", "FLEET_ROUTE", "FLEET_LEARN"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.state, ignore_errors=True)

    def write_cfg(self, cfg, raw=None):
        with open(os.path.join(self.state, "watch.json"), "w") as f:
            f.write(raw if raw is not None else "// test config\n" + json.dumps(cfg, indent=1))

    def p(self, *parts):
        return os.path.join(self.state, *parts)


# ---------------------------------------------------------------- acceptance: the 50-event replay

class TestReplay(StateCase):
    def run_replay(self):
        self.write_cfg(replay_cfg())
        r = fleet(self.state, "run", "--once", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_fixture_is_50_events(self):
        self.assertEqual(len(jsonl(EVENTS)), 50)

    def test_exact_counts_all_to_decide_none_to_route(self):
        res = self.run_replay()
        evs = jsonl(self.p("watch", "events.jsonl"))
        by = {k: sum(1 for e in evs if e["kind"] == k) for k in C.KINDS}
        self.assertEqual(by, {"ask": EXPECTED["ask"], "task-candidate": EXPECTED["task-candidate"],
                              "fyi": EXPECTED["fyi"]})
        self.assertEqual(res["dropped"], EXPECTED["dropped"])
        # every line accounted for: routed + dropped == 50
        self.assertEqual(len(evs) + sum(res["dropped"].values()), EXPECTED["lines"])
        self.assertTrue(all(e["to"] == "DECIDE" for e in evs))
        for e in evs:
            self.assertEqual(C.validate_event(e), [], e["eid"])
        # asks became decisions from WATCH, delivered to DECIDE, each carrying the watch meta
        decs = jsonl(self.p("decisions.jsonl"))
        self.assertEqual(len(decs), EXPECTED["ask"])
        self.assertTrue(all(d["from"] == "WATCH" for d in decs))
        decide = jsonl(self.p("inbox", "DECIDE.jsonl"))
        self.assertTrue(decide and all(m["to"] == "DECIDE" and m["from"] == "WATCH" for m in decide))
        tc = [m for m in decide if m["text"].startswith("TASK-CANDIDATE")]
        self.assertEqual(len(tc), 1)  # batched: one message
        self.assertEqual(len(tc[0]["text"].split("\n")), EXPECTED["task-candidate"])
        fyi = [m for m in decide if m["text"].startswith("FYI %d events:" % EXPECTED["fyi"])]
        self.assertEqual(len(fyi), 1)
        learn = jsonl(self.p("inbox", "LEARN.jsonl"))
        items = [ln for m in learn for ln in m["text"].split("\n") if ln.startswith("LEARN-ITEM ")]
        self.assertEqual(len(items), EXPECTED["learn-item"])
        # none to ROUTE: no inbox file, no message addressed to it anywhere
        self.assertFalse(os.path.exists(self.p("inbox", "ROUTE.jsonl")))
        for name in os.listdir(self.p("inbox")):
            for m in jsonl(self.p("inbox", name)):
                self.assertNotEqual(m["to"], "ROUTE")
        # control: the same check catches a ROUTE address when one exists
        bad = dict(evs[0], to="ROUTE")
        self.assertIn("to must be DECIDE (got 'ROUTE')", C.validate_event(bad))

    def test_burst_collapses_with_count(self):
        self.run_replay()
        b = [e for e in jsonl(self.p("watch", "events.jsonl")) if e["ref"] == EXPECTED["burstRef"]]
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["count"], EXPECTED["burstCount"])
        # control: with batchSec 1 the five edits (5 s apart) stay five events
        shutil.rmtree(self.p("watch"))
        self.write_cfg(replay_cfg(defaults={"pollSec": 900, "batchSec": 1}))
        self.assertEqual(fleet(self.state, "run", "--once").returncode, 0)
        b = [e for e in jsonl(self.p("watch", "events.jsonl")) if e["ref"] == EXPECTED["burstRef"]]
        self.assertEqual(len(b), 5)

    def test_mention_tagged_by_normalize(self):
        self.run_replay()
        m = [e for e in jsonl(self.p("watch", "events.jsonl")) if e["ref"] == "PROJ-7"]
        self.assertEqual(len(m), 1)
        self.assertIn("mention", m[0]["tags"])
        self.assertEqual(m[0]["kind"], "ask")
        self.assertEqual(m[0]["score"], 3)
        # control: the fixture line itself carries no tag; normalize added it
        raw = [e for e in jsonl(EVENTS) if e["ref"] == "PROJ-7"]
        self.assertTrue(raw and all(e["tags"] == [] for e in raw))

    def test_secret_in_no_file_under_state(self):
        self.run_replay()
        r = fleet(self.state, "run", "--once", "--force", "--json")  # a second real poll (dedupe, cursors, health)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["polled"], ["replay"])
        frag = SECRET.split()[-1].encode()
        hits = [p for p, data in walk_bytes(self.state) if frag in data or SECRET.encode() in data]
        self.assertEqual(hits, [])
        # the "a secret appeared" note reached DECIDE, without the value
        notes = [m for m in jsonl(self.p("inbox", "DECIDE.jsonl")) if "a secret appeared in replay" in m["text"]]
        self.assertEqual(len(notes), 1)
        # controls: the same scan finds the secret in the fixture, and finds plain event text in the state tree
        self.assertIn(frag, open(EVENTS, "rb").read())
        plain = b"can you pick a retry cap"
        self.assertTrue(any(plain in data for _p, data in walk_bytes(self.state)))

    def test_second_run_dedupes_through_seen_db(self):
        self.run_replay()
        n1 = len(jsonl(self.p("watch", "events.jsonl")))
        res = json.loads(fleet(self.state, "run", "--once", "--force", "--json").stdout.strip().splitlines()[-1])
        self.assertEqual(res["events"], 0)
        self.assertEqual(len(jsonl(self.p("watch", "events.jsonl"))), n1)
        self.assertEqual(len(jsonl(self.p("decisions.jsonl"))), EXPECTED["ask"])
        # seen.db holds hashes only (no eid text)
        db = sqlite3.connect(self.p("watch", "seen.db"))
        keys = [r[0] for r in db.execute("SELECT k FROM seen")]
        db.close()
        self.assertTrue(keys and all(len(k) == 64 and all(c in "0123456789abcdef" for c in k) for k in keys))
        # control: without seen.db the same run routes everything again
        os.remove(self.p("watch", "seen.db"))
        res = json.loads(fleet(self.state, "run", "--once", "--force", "--json").stdout.strip().splitlines()[-1])
        self.assertEqual(res["events"], n1)

    def test_due_gate_skips_until_next_poll(self):
        self.run_replay()
        res = json.loads(fleet(self.state, "run", "--once", "--json").stdout.strip().splitlines()[-1])
        self.assertEqual(res["skipped"], ["replay"])
        # control: poll-now makes it due
        self.assertEqual(fleet(self.state, "poll-now", "replay").returncode, 0)
        res = json.loads(fleet(self.state, "run", "--once", "--json").stdout.strip().splitlines()[-1])
        self.assertEqual(res["polled"], ["replay"])

    def test_desk_named_route_is_refused(self):
        self.write_cfg(replay_cfg())
        r = fleet(self.state, "run", "--once", FLEET_DECIDE="ROUTE")
        self.assertEqual(r.returncode, 1)
        self.assertIn("never addresses ROUTE", r.stderr)
        self.assertFalse(os.path.exists(self.p("inbox", "ROUTE.jsonl")))
        # control: the default desk name runs
        self.assertEqual(fleet(self.state, "run", "--once").returncode, 0)

    def test_send_refuses_route_directly(self):
        with self.assertRaises(W.RouteRefused):
            W._send("ROUTE", "hi")
        os.environ["FLEET_ROUTE"] = "BOSS"
        with self.assertRaises(W.RouteRefused):
            W._send("BOSS", "hi")
        self.assertFalse(os.path.exists(self.p("inbox", "ROUTE.jsonl")))
        # control: DECIDE is allowed
        W._send("DECIDE", "hi")
        self.assertEqual(len(jsonl(self.p("inbox", "DECIDE.jsonl"))), 1)


# ---------------------------------------------------------------- acceptance: literal token refusal

class TestCheck(StateCase):
    LITERAL = "s3cr3t-literal-value-123"

    def cfg_with(self, token):
        c = replay_cfg()
        c["sources"].append({"name": "oss", "kind": "github", "repos": ["acme/widget"],
                             "auth": {"type": "bearer", "token": token}})
        return c

    def test_literal_token_exits_1(self):
        self.write_cfg(self.cfg_with(self.LITERAL))
        r = fleet(self.state, "check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("auth.token holds a literal value", r.stdout)
        self.assertNotIn(self.LITERAL, r.stdout + r.stderr)  # the value is never echoed
        # control: a reference passes
        self.write_cfg(self.cfg_with("env:ACME_GH_TOKEN"))
        r = fleet(self.state, "check")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_literal_token_outside_auth_and_password(self):
        c = replay_cfg()
        c["sources"][0]["token"] = self.LITERAL
        self.write_cfg(c)
        self.assertEqual(fleet(self.state, "check").returncode, 1)
        c = self.cfg_with("env:X")
        c["sources"][1]["auth"]["password"] = "hunter2hunter2"
        self.write_cfg(c)
        r = fleet(self.state, "check")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("hunter2hunter2", r.stdout + r.stderr)

    def test_run_refuses_literal_and_writes_nothing(self):
        self.write_cfg(self.cfg_with(self.LITERAL))
        r = fleet(self.state, "run", "--once")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.p("watch", "events.jsonl")))
        self.assertEqual([p for p, d in walk_bytes(self.state) if self.LITERAL.encode() in d and
                          not p.endswith("watch.json")], [])

    def test_check_resolve_reports_without_value(self):
        c = replay_cfg()
        c["sources"][0]["auth"] = {"type": "bearer", "token": "env:WATCH_TEST_TOKEN"}
        self.write_cfg(c)
        r = fleet(self.state, "check", "--resolve", WATCH_TEST_TOKEN="tok-value-987")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("resolves (token)", r.stdout)
        self.assertNotIn("tok-value-987", r.stdout + r.stderr)
        # control: unset, --resolve fails while the shape check alone passes
        self.assertEqual(fleet(self.state, "check", "--resolve").returncode, 1)
        self.assertEqual(fleet(self.state, "check").returncode, 0)

    def test_check_via_dispatcher_and_bin(self):
        self.write_cfg(self.cfg_with(self.LITERAL))
        r = subprocess.run([os.path.join(ROOT, "bin", "fleet-watch"), "check"], env=env_for(self.state),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)


# ---------------------------------------------------------------- credentials

class TestCreds(unittest.TestCase):
    def test_env_ref(self):
        self.assertEqual(CR.resolve_ref("env:FOO", {"FOO": "bar"}), "bar")
        with self.assertRaises(CR.Refused) as cm:
            CR.resolve_ref("env:FOO", {})
        self.assertIn("not set", str(cm.exception))

    def test_cmd_ref_first_line(self):
        self.assertEqual(CR.resolve_ref("cmd:printf 'one\\ntwo\\n'"), "one")
        with self.assertRaises(CR.Refused):
            CR.resolve_ref("cmd:false")
        with self.assertRaises(CR.Refused):
            CR.resolve_ref("cmd:no-such-binary-xyz")

    def test_literal_refused_without_echo(self):
        for lit in ("abc123secret", "ghx:abc", "", None, "env:"):
            with self.assertRaises(CR.Refused) as cm:
                CR.resolve_ref(lit)
            if lit and lit != "env:":  # "env:" is part of the help text itself
                self.assertNotIn(lit, str(cm.exception))
        with self.assertRaises(CR.Refused) as cm:
            CR.resolve_auth({"type": "bearer", "token": "abc123secret"})
        self.assertNotIn("abc123secret", str(cm.exception))

    def test_keychain_argv(self):
        self.assertEqual(CR.keychain_argv("svc", "darwin"), ["security", "find-generic-password", "-s", "svc", "-w"])
        self.assertEqual(CR.keychain_argv("svc", "linux"), ["secret-tool", "lookup", "service", "svc"])

    def test_resolve_auth_shapes(self):
        got = CR.resolve_auth({"type": "basic", "user": "env:U", "token": "env:T"}, {"U": "alice", "T": "tt"})
        self.assertEqual(got, {"type": "basic", "user": "alice", "token": "tt"})
        got = CR.resolve_auth({"type": "hmac", "secret": "env:S"}, {"S": "ss"})
        self.assertEqual(got["secret"], "ss")
        self.assertEqual(CR.resolve_auth({"type": "basic", "user": "plain-user", "token": "env:T"}, {"T": "t"})["user"],
                         "plain-user")
        self.assertEqual(CR.resolve_auth(None), {})
        self.assertEqual(CR.resolve_auth({"type": "none"}), {})


# ---------------------------------------------------------------- engine behaviour (in process, clock injected)

T0 = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def add(self, sec):
        self.t = self.t + datetime.timedelta(seconds=sec)


def fake_source(behaviour):
    class Fake(C.Source):
        kind = "command"
        calls = []

        def poll(self, cursor):
            Fake.calls.append({"cursor": cursor, "cfg": self.cfg, "creds": dict(self.creds)})
            return behaviour(self, cursor)
    return Fake


def ev(n, **kw):
    d = dict(eid="command:fake:R-%d:t:%d" % (n, n), source="x", sourceKind="command", at="2026-11-20T11:00:%02dZ" % n,
             type="issue.comment", ref="R-%d" % n, actor="Alice_Example", quote="note %d" % n)
    d.update(kw)
    return C.Event(**d)


class TestEngine(StateCase):
    def cfg(self, **kw):
        c = {"me": {"names": ["Human_Example"], "ids": {"fake": "human-example"}},
             "sources": [{"name": "fake", "kind": "command", "argv": ["true"]}], "filters": FILTERS}
        c.update(kw)
        return c

    def test_next_poll_sec_is_a_floor(self):
        clock = Clock()
        Fake = fake_source(lambda s, c: ([], "c1", 1200))
        W.Engine(self.state, self.cfg(), now=clock, sources={"command": Fake}).run_once()
        cur = json.load(open(self.p("watch", "cursors.json")))["fake"]
        self.assertEqual(cur["nextPollAt"], "2026-11-20T12:20:00Z")  # 1200 > pollSec 900
        # control: a server hint below pollSec does not shorten it
        Fake2 = fake_source(lambda s, c: ([], "c2", 60))
        clock.add(1200)
        W.Engine(self.state, self.cfg(), now=clock, sources={"command": Fake2}).run_once()
        cur = json.load(open(self.p("watch", "cursors.json")))["fake"]
        self.assertEqual(cur["nextPollAt"], "2026-11-20T12:35:00Z")  # 12:20 + 900
        self.assertEqual(Fake2.calls[0]["cursor"], "c1")

    def test_source_state_persists_between_polls(self):
        def first(s, c):
            s.state = {"etag": "W/1", "items": {"R-1": 3}}  # reassigned, not mutated
            return [], "c1", None

        def second(s, c):
            Seen2.append(dict(s.state))
            s.state["etag"] = "W/2"
            return [], "c2", None
        Seen2 = []
        W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": fake_source(first)}).run_once()
        W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": fake_source(second)}).run_once(force=True)
        self.assertEqual(Seen2, [{"etag": "W/1", "items": {"R-1": 3}}])
        self.assertEqual(json.load(open(self.p("watch", "cursors.json")))["fake"]["state"]["etag"], "W/2")
        # control: a state holding a secret-looking value is not saved; the previous state stays

        def leaky(s, c):
            s.state = {"etag": "W/3", "auth": "token = abcd_12345678"}
            return [], "c3", None

        def leaky_in_place(s, c):
            s.state["auth"] = "token = efgh_12345678"  # mutated, not reassigned
            s.state["etag"] = "W/4"
            return [], "c4", None

        def not_json(s, c):
            s.state["obj"] = object()
            return [], "c5", None
        self.write_cfg(self.cfg())  # snapshot() reads watch.json
        for fn in (leaky, leaky_in_place, not_json):
            W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": fake_source(fn)}).run_once(force=True)
            self.assertEqual(json.load(open(self.p("watch", "cursors.json")))["fake"]["state"]["etag"], "W/2")
            self.assertTrue(W.snapshot(self.state)["sources"][0]["stateRefused"])
        self.assertFalse(any(b"abcd_12345678" in d or b"efgh_12345678" in d for _p, d in walk_bytes(self.state)))
        self.assertEqual(json.load(open(self.p("watch", "cursors.json")))["fake"]["cursor"], "c5")  # run completed

    def test_cfg_injection_me_and_defaults(self):
        Fake = fake_source(lambda s, c: ([], None, None))
        cfg = self.cfg(defaults={"quoteChars": 100})
        cfg["sources"][0]["maxEventsPerPoll"] = 7
        cfg["sources"][0]["auth"] = {"type": "bearer", "token": "env:FAKE_TOK"}
        W.Engine(self.state, cfg, now=Clock(), sources={"command": Fake}, env={"FAKE_TOK": "tv"}).run_once()
        got = Fake.calls[0]
        self.assertEqual(got["cfg"]["me"], {"names": ["Human_Example"], "id": "human-example"})
        self.assertEqual((got["cfg"]["quoteChars"], got["cfg"]["maxEventsPerPoll"], got["cfg"]["pollSec"]),
                         (100, 7, 900))
        self.assertEqual(got["cfg"]["auth"], {"type": "bearer", "token": "env:FAKE_TOK"})  # refs, unresolved (contract)
        self.assertEqual(got["creds"], {"type": "bearer", "token": "tv"})
        self.assertNotIn("tv", open(self.p("watch", "cursors.json")).read())

    def test_backoff_retry_after_and_fail_streak_note(self):
        clock = Clock()

        def boom(s, c):
            raise C.SourceError("HTTP 429", retry_after=600, status=429)
        Fake = fake_source(boom)
        eng = W.Engine(self.state, self.cfg(), now=clock, sources={"command": Fake})
        eng.rng.seed(1)
        eng.run_once()
        cur = json.load(open(self.p("watch", "cursors.json")))["fake"]
        self.assertEqual(cur["failStreak"], 1)
        self.assertGreaterEqual(W.parse_iso(cur["nextPollAt"]), clock() + datetime.timedelta(seconds=600))
        for _ in range(4):
            eng.run_once(force=True)
        notes = [m for m in jsonl(self.p("inbox", "DECIDE.jsonl")) if "failed 3 times" in m["text"]]
        self.assertEqual(len(notes), 1)  # once at 3, quiet on 4 and 5
        h = json.load(open(self.p("watch", "health.json")))["fake"]
        self.assertEqual((h["failStreak"], h["lastError"]), (5, "HTTP 429"))
        # backoff grows and stays under the cap
        delays = [eng._delay_fail(n, None) for n in range(1, 12)]
        self.assertTrue(all(C.POLL_FLOOR_SEC <= d <= C.BACKOFF_CAP_SEC for d in delays))
        self.assertGreater(delays[5], delays[0])
        # control: a success resets the streak and the note can fire again later
        Ok = fake_source(lambda s, c: ([], None, None))
        W.Engine(self.state, self.cfg(), now=clock, sources={"command": Ok}).run_once(force=True)
        self.assertEqual(json.load(open(self.p("watch", "cursors.json")))["fake"]["failStreak"], 0)

    def test_error_text_with_secret_redacted(self):
        def boom(s, c):
            raise C.SourceError("auth failed for token=Zz9" + "q" * 40 + "K1")
        W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": fake_source(boom)}).run_once()
        h = open(self.p("watch", "health.json")).read() + open(self.p("watch", "watch.log")).read()
        self.assertNotIn("Zz9", h)
        self.assertIn("redacted", h)

    def test_quiet_hours_hold_then_release(self):
        clock = Clock(datetime.datetime(2026, 11, 20, 20, 0, tzinfo=datetime.timezone.utc))
        evs = [ev(1, tags=["mention"]), ev(2, tags=["review-requested"]), ev(3)]
        Fake = fake_source(lambda s, c: (list(evs), None, None))
        cfg = self.cfg(quiet={"hours": "19:00-08:00"})
        res = W.Engine(self.state, cfg, now=clock, sources={"command": Fake}, tz=datetime.timezone.utc).run_once()
        self.assertEqual((res["ask"], res["held"]), (2, 2))
        self.assertFalse(os.path.exists(self.p("decisions.jsonl")))  # held, not opened
        self.assertEqual(W.snapshot(self.state, now=clock())["held"], 2)
        # fyi still flows during quiet hours
        self.assertTrue(any(m["text"].startswith("FYI 1 events") for m in jsonl(self.p("inbox", "DECIDE.jsonl"))))
        # after quiet hours the next run opens one batch decision
        clock.t = datetime.datetime(2026, 11, 21, 8, 30, tzinfo=datetime.timezone.utc)
        Empty = fake_source(lambda s, c: ([], None, None))
        res = W.Engine(self.state, cfg, now=clock, sources={"command": Empty}, tz=datetime.timezone.utc).run_once()
        decs = jsonl(self.p("decisions.jsonl"))
        self.assertEqual(len(decs), 2)  # one decision per held ask, so each is answered on its own
        self.assertTrue(all(d["question"].startswith("(held for quiet hours) fake R-") for d in decs))
        self.assertEqual(res["released"], [d["id"] for d in decs])
        self.assertEqual(W.snapshot(self.state)["held"], 0)
        # control: outside quiet hours asks open at once
        self.assertFalse(W.in_quiet({"hours": "19:00-08:00"}, clock(), datetime.timezone.utc))
        self.assertTrue(W.in_quiet({"hours": "19:00-08:00"}, clock() - datetime.timedelta(hours=3),
                                   datetime.timezone.utc))

    def test_quote_capped_and_score(self):
        long = "x" * 900
        Fake = fake_source(lambda s, c: ([ev(1, quote=long, type="pr.merged")], None, None))
        W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": Fake}).run_once()
        e = jsonl(self.p("watch", "events.jsonl"))[0]
        self.assertEqual(len(e["quote"]), 600)
        self.assertTrue(e["quote"].endswith("..."))
        self.assertEqual((e["kind"], e["score"]), ("fyi", 2))

    def test_secret_past_quote_cap_still_dropped(self):
        # the secret sits after char 600: scrubbing before the cap still catches it
        q = "a" * 650 + " password: Hidd3n!value"
        Fake = fake_source(lambda s, c: ([ev(1, quote=q)], None, None))
        res = W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": Fake}).run_once()
        self.assertEqual(res["dropped"], {"secret": 1})
        self.assertFalse(any(b"Hidd3n" in d for _p, d in walk_bytes(self.state)))
        # control: secret in fields is caught too
        Fake2 = fake_source(lambda s, c: ([ev(2, fields={"body": "token = abcd_12345678"})], None, None))
        res = W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": Fake2}).run_once(force=True)
        self.assertEqual(res["dropped"], {"secret": 1})

    def test_seen_purged_after_30_days(self):
        os.makedirs(self.p("watch"))
        s = W.Seen(self.p("watch", "seen.db"))
        s.add("old", (T0 - datetime.timedelta(days=31)).timestamp())
        s.add("new", (T0 - datetime.timedelta(days=29)).timestamp())
        s.close()
        Fake = fake_source(lambda s, c: ([], None, None))
        res = W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": Fake}).run_once()
        self.assertEqual(res["purged"], 1)
        s = W.Seen(self.p("watch", "seen.db"))
        self.assertEqual((s.has("old"), s.has("new")), (False, True))
        s.close()

    def test_no_rule_matched_is_dropped_and_counted(self):
        Fake = fake_source(lambda s, c: ([ev(1)], None, None))
        res = W.Engine(self.state, self.cfg(filters=[{"drop": {"actor": "nobody"}}]), now=Clock(),
                       sources={"command": Fake}).run_once()
        self.assertEqual(res["dropped"], {"no rule matched": 1})
        self.assertEqual(jsonl(self.p("watch", "events.jsonl")), [])

    def test_learn_min_score(self):
        evs = [ev(1), ev(2, type="pr.merged"), ev(3, tags=["mention"])]
        Fake = fake_source(lambda s, c: (list(evs), None, None))
        res = W.Engine(self.state, self.cfg(learn={"minScore": 2}), now=Clock(), sources={"command": Fake}).run_once()
        self.assertEqual(res["learn-item"], 2)  # the merged fyi (2) and the ask (3), not the plain fyi (1)
        learn = jsonl(self.p("inbox", "LEARN.jsonl"))
        self.assertEqual(len(learn[0]["text"].split("\n")), 2)

    def test_ask_decision_carries_watch_meta(self):
        Fake = fake_source(lambda s, c: ([ev(1, tags=["mention"], url="https://acme.example/browse/R-1")], None, None))
        W.Engine(self.state, self.cfg(), now=Clock(), sources={"command": Fake}).run_once()
        meta = W.watch_meta(C.Event.from_dict(jsonl(self.p("watch", "events.jsonl"))[0]))
        self.assertEqual(set(meta), {"sourceKind", "source", "ref", "url", "eid"})
        d = jsonl(self.p("decisions.jsonl"))[0]
        self.assertTrue(d["question"].startswith('fake R-1: Alice_Example issue.comment "note 1"'))
        # control: a non-http url is not passed
        self.assertNotIn("url", W.watch_meta(C.Event.from_dict(dict(jsonl(self.p("watch", "events.jsonl"))[0],
                                                                     url="javascript:alert(1)"))))

    def test_burst_never_folds_an_ask_into_an_fyi(self):
        evs = [ev(1, ref="R-9", at="2026-11-20T11:00:00Z"),
               ev(2, ref="R-9", at="2026-11-20T11:00:05Z", quote="@Human_Example please decide"),
               ev(3, ref="R-9", at="2026-11-20T11:00:09Z")]
        res = W.Engine(self.state, self.cfg(), now=Clock(),
                       sources={"command": fake_source(lambda s, c: (list(evs), None, None))}).run_once()
        self.assertEqual((res["ask"], res["fyi"], res["dropped"]), (1, 1, {"burst collapsed": 1}))

    def test_wrong_typed_event_is_invalid_not_fatal(self):
        bad = [ev(1, title=123), ev(2, eid=7), ev(3, tags="x"), ev(4)]
        res = W.Engine(self.state, self.cfg(), now=Clock(),
                       sources={"command": fake_source(lambda s, c: (list(bad), None, None))}).run_once()
        self.assertEqual((res["dropped"], res["fyi"]), ({"invalid": 3}, 1))
        # through the real command source too
        cfg = self.cfg()
        cfg["sources"][0]["argv"] = ["sh", "-c", "echo '{\"type\":\"t\",\"ref\":\"R\",\"title\":123}'; "
                                     "echo '{\"type\":\"t\",\"ref\":\"R2\"}'"]
        res = W.Engine(self.state, cfg, now=Clock()).run_once(force=True)
        self.assertEqual((res["dropped"], res["fyi"], res["failed"]), ({"invalid": 1}, 1, []))

    def test_webhook_events_keep_their_source(self):
        cfg = self.cfg()
        cfg["sources"] = [{"name": "oss", "kind": "github", "repos": ["acme/widget"]},
                          {"name": "oss-hook", "kind": "webhook", "for": "oss",
                           "auth": {"type": "hmac", "secret": "env:HOOK_SECRET"}}]
        hook = fake_source(lambda s, c: ([ev(1, source="oss", sourceKind="github", eid="github:acme/widget:1:t:merged"),
                                          ev(2, source="nope", sourceKind="github")], None, None))
        hook.kind = "webhook"
        W.Engine(self.state, cfg, now=Clock(), sources={"webhook": hook},
                 env={"HOOK_SECRET": "hs"}).run_once(only=["oss-hook"])
        self.assertEqual(hook.calls[0]["creds"]["secret"], "hs")  # auth.secret resolved into creds (fd-p6-webhook)
        got = {e["ref"]: (e["source"], e["sourceKind"]) for e in jsonl(self.p("watch", "events.jsonl"))}
        self.assertEqual(got["R-1"], ("oss", "github"))
        # control: an event naming an unconfigured source is stamped as the webhook entry
        self.assertEqual(got["R-2"], ("oss-hook", "webhook"))

    def test_adapter_crash_is_a_failure_not_a_dead_engine(self):
        def crash(s, c):
            raise RuntimeError("bug")
        cfg = self.cfg()
        cfg["sources"].append({"name": "ok", "kind": "call", "file": self.p("t.txt")})
        open(self.p("t.txt"), "w").write("[00:01] Alice_Example: hello @Human_Example\n")
        res = W.Engine(self.state, cfg, now=Clock(), sources={"command": fake_source(crash)}).run_once()
        self.assertEqual((res["failed"], res["polled"]), (["fake"], ["ok"]))


# ---------------------------------------------------------------- sources

class TestCommandSource(StateCase):
    def mk(self, argv, **kw):
        cfg = {"name": "cmd", "kind": "command", "argv": argv}
        cfg.update(kw)
        return C.load_source("command")(cfg, {}, {})

    def test_reads_fixture_and_fills_fields(self):
        evs, cur, nxt = self.mk(["cat", EVENTS]).poll(None)
        self.assertEqual(len(evs), 50)
        self.assertTrue(all(e.source == "cmd" and e.sourceKind == "command" for e in evs))
        self.assertIsNone(nxt)

    def test_cursor_line_and_env(self):
        script = "echo \"{\\\"type\\\":\\\"t\\\",\\\"ref\\\":\\\"R-$FLEET_WATCH_CURSOR\\\"}\"; echo '{\"cursor\": \"c9\"}'"
        evs, cur, _ = self.mk(["sh", "-c", script]).poll("c8")
        self.assertEqual((cur, evs[0].ref), ("c9", "R-c8"))
        self.assertTrue(evs[0].eid.startswith("command:cmd:R-c8:"))

    def test_token_via_env_only(self):
        src = C.load_source("command")({"name": "c", "argv": ["sh", "-c", 'echo "{\\"type\\":\\"t\\",\\"ref\\":\\"$FLEET_WATCH_TOKEN\\"}"']},
                                       {"token": "tk1"}, {})
        evs, _, _ = src.poll(None)
        self.assertEqual(evs[0].ref, "tk1")
        # control: no creds, no token variable
        os.environ["FLEET_WATCH_TOKEN"] = "leak"
        try:
            evs, _, _ = self.mk(["sh", "-c", 'echo "{\\"type\\":\\"t\\",\\"ref\\":\\"x$FLEET_WATCH_TOKEN\\"}"']).poll(None)
        finally:
            os.environ.pop("FLEET_WATCH_TOKEN")
        self.assertEqual(evs[0].ref, "x")

    def test_timeout_kills_group(self):
        src = self.mk(["sh", "-c", "sleep 30 & sleep 30"], timeoutSec=1)
        t = datetime.datetime.now()
        with self.assertRaises(C.SourceError) as cm:
            src.poll(None)
        self.assertIn("timeout", str(cm.exception))
        self.assertLess((datetime.datetime.now() - t).total_seconds(), 10)
        self.assertEqual(src.health()["failStreak"], 1)

    def test_bad_exit_and_junk_lines(self):
        with self.assertRaises(C.SourceError):
            self.mk(["false"]).poll(None)
        src = self.mk(["sh", "-c", "echo junk; echo '[1]'; echo '{\"type\":\"t\"}'; echo '{\"type\":\"t\",\"ref\":\"R\"}'"])
        evs, _, _ = src.poll(None)
        self.assertEqual((len(evs), src.health()["skipped"]), (1, 3))

    def test_max_events_per_poll(self):
        evs, _, _ = self.mk(["cat", EVENTS], maxEventsPerPoll=10).poll(None)
        self.assertEqual(len(evs), 10)

    def test_pipeline_fields_from_program_ignored(self):
        src = self.mk(["sh", "-c", "echo '{\"type\":\"t\",\"ref\":\"R\",\"kind\":\"ask\",\"to\":\"ROUTE\",\"score\":3}'"])
        e = src.poll(None)[0][0]
        self.assertEqual((e.kind, e.to, e.score), ("fyi", "DECIDE", 1))


class TestCallSource(StateCase):
    def test_tails_transcript_by_offset(self):
        t = self.p("call.txt")
        with open(t, "w") as f:
            f.write("[00:01] Alice_Example: can you pick a cap, @Human_Example?\n[00:02] Bob_Example: ok\npartial")
        src = C.load_source("call")({"name": "call", "kind": "call", "file": t}, {}, {})
        evs, cur, _ = src.poll(None)
        self.assertEqual([e.actor for e in evs], ["Alice_Example", "Bob_Example"])
        self.assertTrue(all(e.origin == "call" and e.type == "utterance" for e in evs))
        evs2, cur2, _ = src.poll(cur)
        self.assertEqual((evs2, cur2), ([], cur))
        with open(t, "a") as f:
            f.write(" line\n[00:03] Carol_Example: next\n")
        evs3, _, _ = src.poll(cur)
        self.assertEqual([e.actor for e in evs3], ["Carol_Example"])

    def test_listener_config_and_engine_routes_mention(self):
        t = self.p("call.txt")
        open(t, "w").write("[00:01] Alice_Example: @Human_Example can you decide on the cap?\n")
        json.dump({"source": {"kind": "file", "path": "$FLEET_STATE/call.txt"}}, open(self.p("listener.json"), "w"))
        cfg = {"me": {"names": ["Human_Example"]}, "filters": FILTERS,
               "sources": [{"name": "call", "kind": "call", "listener": "$FLEET_STATE/listener.json"}]}
        res = W.Engine(self.state, cfg, now=Clock()).run_once()
        self.assertEqual(res["ask"], 1)
        e = jsonl(self.p("watch", "events.jsonl"))[0]
        self.assertEqual((e["origin"], e["sourceKind"]), ("call", "call"))
        # control: a listener source that is not a file fails the poll
        json.dump({"source": {"kind": "stdin"}}, open(self.p("listener.json"), "w"))
        res = W.Engine(self.state, cfg, now=Clock()).run_once(force=True)
        self.assertEqual(res["failed"], ["call"])


# ---------------------------------------------------------------- snapshot, verbs, pane

class TestSnapshotAndPane(StateCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(replay_cfg())
        self.assertEqual(fleet(self.state, "run", "--once").returncode, 0)

    def test_snapshot_matches_contract(self):
        s = W.snapshot(self.state)
        self.assertEqual(C.validate_snapshot(s), [])
        self.assertEqual((s["counts"]["ask"], s["counts"]["fyi"]), (EXPECTED["ask"], EXPECTED["fyi"]))
        self.assertEqual(s["counts"]["dropped"], EXPECTED["dropped"])
        self.assertEqual(len(s["recent"]), 24)
        r = fleet(self.state, "snapshot", "--compact")
        self.assertEqual(json.loads(r.stdout)["counts"], s["counts"])
        # control: a snapshot carrying auth fails the contract check
        bad = json.loads(json.dumps(s))
        bad["sources"][0]["auth"] = {"token": "env:X"}
        self.assertIn("snapshot.sources[0] carries auth", C.validate_snapshot(bad))

    def test_verbs_and_pane_keys(self):
        self.assertEqual(W.pane_key(self.state, "z", "replay"), "replay paused")
        self.assertTrue(json.load(open(self.p("watch", "cursors.json")))["replay"]["paused"])
        res = json.loads(fleet(self.state, "run", "--once", "--force", "--json").stdout.strip().splitlines()[-1])
        self.assertEqual(res["skipped"], ["replay"])  # paused beats --force
        self.assertTrue(W.snapshot(self.state)["sources"][0]["paused"])
        self.assertEqual(W.pane_key(self.state, "r", "replay"), "replay resumed")
        W.pane_key(self.state, "p", "replay")
        res = json.loads(fleet(self.state, "run", "--once", "--json").stdout.strip().splitlines()[-1])
        self.assertEqual(res["polled"], ["replay"])
        self.assertEqual(W.pane_key(self.state, "h", "replay"), "nothing held")
        self.assertEqual(W.pane_key(self.state, "x", "replay"), "")
        with self.assertRaises(KeyError):
            W.pause(self.state, "nope")

    def test_status_once_and_stats(self):
        r = fleet(self.state, "status", "--once", "--select", "replay")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ask 4  task 3  fyi 17", r.stdout)
        self.assertIn("> replay", r.stdout)
        self.assertIn("p poll now", r.stdout)
        r = fleet(self.state, "stats", "--json")
        self.assertEqual(json.loads(r.stdout)["dropped"], EXPECTED["dropped"])


if __name__ == "__main__":
    unittest.main()
