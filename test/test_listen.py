"""Tests for lib/fleet_listen.py (the LISTENER UI). Stdlib unittest, temp FLEET_STATE, synthetic fixtures only.

Run: python3 -m unittest test.test_listen   (from the repo root)
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTEN = os.path.join(ROOT, "lib", "fleet_listen.py")
FX = os.path.join(ROOT, "test", "fixtures", "listen")
FLEET = os.path.join(ROOT, "bin", "fleet")

spec = importlib.util.spec_from_file_location("fleet_listen", LISTEN)
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

FACTS = [{"match": ["p95"], "say": "p95 went from 840 ms to 310 ms on the last run", "source": "lane-a, 11:40"}]
FAKE_TOKEN = "sk-test-FAKE0123456789abcdefFAKE0123"


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="listentest-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.env = {"FLEET_STATE": self.state, "FLEET_NAME": "WATCH", "FLEET_DECIDE": "DECISIONS",
                    "FLEET_ROUTE": "ORCHESTRATOR", "FLEET_TMUX_SOCKET": "listentest-none"}
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, **kw):
        c = {"me": "Human_Example", "speakerAliases": {"SPEAKER_00": "Bob_Example"}, "facts": FACTS,
             "batchSec": 0}
        c.update(kw)
        return L.load_config(c)

    def replay(self, cfg, path=os.path.join(FX, "call.jsonl")):
        lis = L.Listener(cfg)
        with open(path) as f:
            for raw in f:
                u = L.parse_utterance(raw, cfg["speakerAliases"])
                if u:
                    lis.feed(u)
        lis.flush()
        return lis

    def decisions(self):
        return read_jsonl(os.path.join(self.state, "decisions.jsonl"))

    def orch(self):
        return read_jsonl(os.path.join(self.state, "inbox", "ORCHESTRATOR.jsonl"))

    def run_cli(self, *args, timeout=30, stdin=None, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([sys.executable, LISTEN] + list(args), capture_output=True, text=True,
                              timeout=timeout, input=stdin, env=e)


class ParseTest(Base):
    def test_json_line(self):
        u = L.parse_utterance('{"at": "2026-01-01T15:22:58Z", "speaker": "Alice_Example", "text": "hi", "final": true}')
        self.assertEqual((u["speaker"], u["text"], u["final"], u["clock"]), ("Alice_Example", "hi", True, "15:22:58"))

    def test_whisperlive_segment_and_alias(self):
        u = L.parse_utterance('{"start": 813.0, "end": 815.5, "speaker": "SPEAKER_00", "text": "yo", "completed": true}',
                              {"SPEAKER_00": "Bob_Example"})
        self.assertEqual((u["speaker"], u["final"], u["clock"]), ("Bob_Example", True, "00:13:33"))
        u = L.parse_utterance('{"start": 1.0, "speaker": "SPEAKER_01", "text": "partial", "completed": false}')
        self.assertFalse(u["final"])
        self.assertEqual(u["speaker"], "SPEAKER_01")

    def test_text_line(self):
        u = L.parse_utterance("[00:22:58] Alice_Example: can we get it?\n")
        self.assertEqual((u["clock"], u["speaker"], u["text"], u["final"]),
                         ("00:22:58", "Alice_Example", "can we get it?", True))

    def test_junk_is_none(self):
        for raw in ("", "   ", "no speaker here", "{not json", '{"speaker": "A"}', "[]"):
            self.assertIsNone(L.parse_utterance(raw), raw)


class SecretTest(Base):
    def test_fake_tokens_detected(self):
        for t in ("the key is " + FAKE_TOKEN, "ghp_FAKEabcdefghijklmnopqrstuvwxyz0123", "AKIAFAKE000000000000",
                  "my password is Tr0ub4dor-fake", "api key: abcdEFGH1234ijklMNOP",
                  "-----BEGIN RSA PRIVATE KEY-----", "xoxb-1234567890-FAKEFAKEFAKE"):
            self.assertTrue(L.is_secret(t), t)

    def test_plain_speech_not_secret(self):
        for t in ("the token bucket limits requests", "what was the p95 after the cache change?",
                  "password resets are broken for some users", "I'll need the staging numbers before Friday",
                  "the token is expired", "the API key is invalid", "the password is required for staging",
                  "see run 3f2b9c1e-8d4a-4b6e-9f10-2a7c5d8e1b3f for details",
                  "it landed in 9fceb02d0ae598e95dc970b74767f19372d61af8 this morning",
                  "the notes are in docs/release-notes/2026-01-01-weekly-sync-and-retro-follow-ups"):
            self.assertFalse(L.is_secret(t), t)
        for t in ("the password is hunter2!", "api key: Zq81xLm0Pd77Rw", "use Xk9mP2qL7vN4rT8wY3bH6jF1cD5sG0aZ"):
            self.assertTrue(L.is_secret(t), t)


class RoutingTest(Base):
    def test_replay_routes_asks_actions_say(self):
        lis = self.replay(self.cfg())
        ds = self.decisions()
        self.assertEqual(len(ds), 1, ds)  # duplicate ask dropped
        q = ds[0]["question"]
        for part in ("can we get the retry change into this week?", "Alice_Example", "15:22:58"):
            self.assertIn(part, q)
        self.assertEqual(ds[0]["from"], "WATCH")
        desk = read_jsonl(os.path.join(self.state, "inbox", "DECISIONS.jsonl"))
        self.assertEqual(len(desk), 1)
        text = "\n".join(m["text"] for m in self.orch())
        self.assertIn("I'll need the staging numbers before Friday", text)
        self.assertIn("owner: unresolved", text)
        self.assertIn("due: Friday", text)
        self.assertIn("decided-on-call", text)
        self.assertIn("uncertain", text)
        self.assertNotIn("weather", text)  # classified other: nothing sent
        self.assertNotIn("weather", q)
        self.assertTrue(any("840 ms to 310 ms" in s["text"] and s["source"] == "lane-a, 11:40" for s in lis.say))
        self.assertEqual(lis.counts["ask"], 1)
        self.assertEqual(lis.counts["action"], 1)
        self.assertEqual(lis.counts["uncertain"], 1)
        self.assertEqual(lis.counts["secret"], 1)

    def test_decided_goes_to_decisions_log_unconfirmed(self):
        self.replay(self.cfg())
        log = read_jsonl(os.path.join(self.state, "decisions-log.jsonl"))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["kind"], "decided-on-call")
        self.assertIsNone(log[0]["id"])
        self.assertIn("let's bundle it", log[0]["verbatim"])
        self.assertFalse(log[0]["confirmed"])
        self.assertTrue(all(d["status"] == "open" for d in self.decisions()))  # never closes a desk call

    def test_secret_never_forwarded(self):
        lis = self.replay(self.cfg())
        blob = json.dumps(self.decisions()) + json.dumps(self.orch())
        for p in ("decisions-log.jsonl", "inbox/DECISIONS.jsonl"):
            fp = os.path.join(self.state, p)
            if os.path.exists(fp):
                with open(fp) as fh:
                    blob += fh.read()
        self.assertNotIn("FAKE0123", blob)
        self.assertNotIn("FAKE0123", json.dumps([ln for ln in lis.lines]))
        self.assertNotIn("FAKE0123", L.render(lis, 100, 40))
        self.assertTrue(any("secret" in ln.get("note", "") for ln in lis.lines))

    def test_other_sends_nothing(self):
        lis = L.Listener(self.cfg())
        lis.feed(L.parse_utterance('{"speaker": "Carol_Example", "text": "the weather is nice today", "final": true}'))
        lis.flush()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.orch(), [])

    def test_non_final_never_routed_and_rendered_dim(self):
        lis = L.Listener(self.cfg())
        lis.feed(L.parse_utterance('{"speaker": "Alice_Example", "text": "can we ship the retry change today?", "final": false}'))
        lis.flush()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.orch(), [])
        out = L.render(lis, 100, 30)
        self.assertIn("\x1b[2m", out)
        self.assertIn("can we ship the retry change today?", out)

    def test_speaker_filter_limits_routing_and_changes_live(self):
        cfg = self.cfg(speakers=["Alice_Example"])
        lis = L.Listener(cfg)
        lis.feed(L.parse_utterance('{"speaker": "Bob_Example", "text": "I\'ll write the migration by Monday", "final": true}'))
        lis.flush()
        self.assertEqual(self.orch(), [])
        lis.set_filter("Bob_Example")
        self.assertEqual(lis.speakers, ["Bob_Example"])
        lis.feed(L.parse_utterance('{"speaker": "Bob_Example", "text": "I\'ll write the docs by Tuesday", "final": true}'))
        lis.flush()
        self.assertIn("write the docs", "\n".join(m["text"] for m in self.orch()))

    def test_display_filter_hides_other_speakers_and_widening_restores(self):
        lis = L.Listener(self.cfg(speakers=["Alice_Example"]))
        lis.feed(L.parse_utterance('{"speaker": "Alice_Example", "text": "alpha line from alice", "final": true}'))
        lis.feed(L.parse_utterance('{"speaker": "Bob_Example", "text": "bravo line from bob", "final": true}'))
        lis.feed(L.parse_utterance('{"speaker": "Bob_Example", "text": "bob still talking", "final": false}'))
        out = L.render(lis, 100, 30)
        self.assertIn("alpha line from alice", out)
        self.assertNotIn("bravo line from bob", out)
        self.assertNotIn("bob still talking", out)
        lis.set_filter("Bob_Example")
        out = L.render(lis, 100, 30)
        self.assertIn("bravo line from bob", out)  # history comes back: the filter is applied at draw time
        self.assertNotIn("alpha line from alice", out)
        lis.set_filter("")
        out = L.render(lis, 100, 30)
        self.assertIn("alpha line from alice", out)
        self.assertIn("bravo line from bob", out)

    def test_narrow_pane_wraps_long_lines_and_say_this(self):
        long_say = "p95 went from 840 ms to 310 ms on the last run, and the error budget is back above " \
                   "ninety percent for the quarter so the freeze can lift"
        lis = L.Listener(self.cfg(facts=[{"match": ["p95"], "say": long_say, "source": "lane-a"}]))
        text = "what was the p95 after the cache change, " + "and how does it compare to last week " * 4 + "END-OF-LINE?"
        lis.feed(L.parse_utterance(json.dumps({"speaker": "Alice_Example", "text": text, "final": True})))
        lis.flush()
        out = L.render(lis, 60, 60, color=False)
        self.assertTrue(all(len(r) <= 60 for r in out.splitlines()), out)
        self.assertIn("END-OF-LINE", out)
        self.assertIn("freeze can lift", out)

    def test_asks_always_go_to_the_desk(self):
        cfg = self.cfg(route={"asks": "SOMEONE_ELSE"})
        self.assertEqual(cfg["route"]["asks"], "DECISIONS")

    def test_owner_guess_from_roster_lane_name(self):
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"lane-a": {"cli": "claude"}}, f)
        lis = L.Listener(self.cfg())
        lis.feed(L.parse_utterance('{"speaker": "Bob_Example", "text": "lane-a will rerun the load test by Friday", "final": true}'))
        lis.flush()
        self.assertIn("owner: lane-a", "\n".join(m["text"] for m in self.orch()))

    def test_burst_batched_into_one_orchestrator_message(self):
        lis = L.Listener(self.cfg(batchSec=60))
        for t in ("I'll fix the build", "I'll update the docs", "I'll ping the vendor"):
            lis.feed(L.parse_utterance(json.dumps({"speaker": "Bob_Example", "text": t, "final": True})))
        self.assertEqual(self.orch(), [])  # still inside the batch window
        lis.flush()
        msgs = self.orch()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"].count("ACTION"), 3)

    def test_render_layout(self):
        lis = self.replay(self.cfg(title="weekly sync"))
        out = L.render(lis, 100, 40)
        for part in ("LISTENER: weekly sync", "filter:", "SAY THIS", "routed: 1 ask, 1 action", "unsure: 1",
                     "-> ASK  sent to DECISIONS as Q1", "-> ACTION  sent to ORCHESTRATOR (owner: unresolved)"):
            self.assertIn(part, out)


class CommandClassifierTest(Base):
    def stub_cfg(self, **kw):
        self.stub_log = os.path.join(self.tmp, "stub.log")
        os.environ["STUB_LOG"] = self.stub_log
        c = {"kind": "command", "run": os.path.join(FX, "classify-stub.sh"), "timeoutSec": 5, "maxTokens": 300,
             "minIntervalSec": 0, "tailChars": 1800}
        c.update(kw)
        return self.cfg(classifier=c)

    def tearDown(self):
        os.environ.pop("STUB_LOG", None)
        super().tearDown()

    def feed_three(self, lis):
        for t in ("can we get the retry change into this week?", "I'll need the staging numbers before Friday",
                  "what was the p95 after the cache change?"):
            lis.feed(L.parse_utterance(json.dumps({"speaker": "Alice_Example", "text": t, "final": True})))

    def test_strict_lines_grounding_and_ignored(self):
        lis = L.Listener(self.stub_cfg())
        self.feed_three(lis)
        lis.flush()
        self.assertEqual(len(self.decisions()), 1)
        text = "\n".join(m["text"] for m in self.orch())
        self.assertIn("owner: lane-a", text)
        self.assertNotIn("a quote nobody said", text)  # ungrounded: not sent
        self.assertGreaterEqual(lis.counts["ungrounded"], 1)
        self.assertGreaterEqual(lis.counts["ignored"], 2)  # no prefix + unknown prefix
        self.assertTrue(any("310 ms" in s["text"] for s in lis.say))
        self.assertIsNotNone(lis.latency_ms)
        self.assertIn("classifier", L.render(lis, 100, 30))

    def test_throttle_and_bounded_tail(self):
        lis = L.Listener(self.stub_cfg(minIntervalSec=60, tailChars=50))
        self.feed_three(lis)
        with open(self.stub_log) as f:
            self.assertEqual(len(f.read().split()), 1)  # one call inside the interval, rest pending
        lis.flush()
        with open(self.stub_log) as f:
            self.assertEqual(len(f.read().split()), 2)
        with open(self.stub_log + ".last") as f:
            req = json.load(f)
        self.assertLessEqual(len(req["tail"]), 50)
        self.assertEqual(req["maxTokens"], 300)
        self.assertIn("speakers", req)

    def test_timeout_shows_in_status_and_keeps_going(self):
        cfg = self.cfg(classifier={"kind": "command", "run": "sleep 5", "timeoutSec": 1, "minIntervalSec": 0})
        lis = L.Listener(cfg)
        t0 = time.time()
        lis.feed(L.parse_utterance('{"speaker": "Alice_Example", "text": "can we ship it?", "final": true}'))
        lis.flush()
        self.assertLess(time.time() - t0, 4)
        self.assertIn("classifier", lis.status.get("classifier", ""))
        self.assertIn("timeout", lis.status.get("classifier", ""))

    def test_nonzero_exit_routes_nothing(self):
        cfg = self.cfg(classifier={"kind": "command", "run": "echo 'ASK: can we ship it?'; exit 2", "timeoutSec": 5,
                                   "minIntervalSec": 0})
        lis = L.Listener(cfg)
        lis.feed(L.parse_utterance('{"speaker": "Alice_Example", "text": "can we ship it?", "final": true}'))
        lis.flush()
        self.assertEqual(self.decisions(), [])
        self.assertIn("exit 2", lis.status.get("classifier", ""))

    def test_classifier_never_sees_secret(self):
        lis = L.Listener(self.stub_cfg())
        lis.feed(L.parse_utterance(json.dumps({"speaker": "Alice_Example", "text": "paste: " + FAKE_TOKEN, "final": True})))
        lis.flush()
        self.assertFalse(os.path.exists(self.stub_log))


class WatcherTest(Base):
    def test_trigger_parse(self):
        self.assertEqual(L.parse_trigger("every 30s"), ("every", 30.0))
        self.assertEqual(L.parse_trigger("on speaker Alice_Example"), ("speaker", "Alice_Example"))
        self.assertEqual(L.parse_trigger("on keyword p95"), ("keyword", "p95"))
        with self.assertRaises(ValueError):
            L.parse_trigger("sometimes")

    def test_two_watchers_side_by_side_on_replay(self):
        tick_log = os.path.join(self.tmp, "tick.log")
        cfg = self.cfg(watchers=[
            {"name": "numbers", "trigger": "on keyword p95", "run": os.path.join(FX, "watcher-numbers.sh"),
             "routeTo": "say"},
            {"name": "tick", "trigger": "every 1s", "run": os.path.join(FX, "watcher-tick.sh"), "routeTo": "say"},
        ])
        p = os.path.join(self.tmp, "cfg.json")
        with open(p, "w") as f:
            json.dump(cfg, f)
        r = self.run_cli("--source", "replay", os.path.join(FX, "call.jsonl"), "--speed", "10", "--config", p,
                         "--plain", "--exit-at-eof", env={"TICK_LOG": tick_log})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SAY p95 is 310 ms (numbers watcher)", r.stdout)
        with open(tick_log) as f:
            self.assertGreaterEqual(len(f.read().split()), 2)
        self.assertEqual(r.stdout.count("\nSAY p95 is 310 ms (numbers watcher)"), 1)  # watcher fired once


class SourceTest(Base):
    def cfgfile(self, **kw):
        p = os.path.join(self.tmp, "cfg.json")
        with open(p, "w") as f:
            json.dump(self.cfg(**kw), f)
        return p

    def test_replay_cli_end_to_end(self):
        r = self.run_cli("--source", "replay", os.path.join(FX, "call.jsonl"), "--speed", "0",
                         "--config", self.cfgfile(), "--plain", "--exit-at-eof")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Alice_Example", r.stdout)
        self.assertIn("ASK  sent to DECISIONS as Q1", r.stdout)
        self.assertNotIn("FAKE0123", r.stdout + r.stderr)
        self.assertEqual(len(self.decisions()), 1)

    def test_replay_speed(self):
        p = os.path.join(self.tmp, "two.jsonl")
        with open(p, "w") as f:
            f.write('{"at": "2026-01-01T10:00:00Z", "speaker": "A_Example", "text": "one", "final": true}\n')
            f.write('{"at": "2026-01-01T10:00:02Z", "speaker": "A_Example", "text": "two", "final": true}\n')
        t0 = time.time()
        self.run_cli("--source", "replay", p, "--speed", "10", "--config", self.cfgfile(), "--plain", "--exit-at-eof")
        dt = time.time() - t0
        self.assertGreater(dt, 0.15)
        self.assertLess(dt, 1.9)

    def test_stdin_source(self):
        with open(os.path.join(FX, "call.txt")) as f:
            data = f.read()
        r = self.run_cli("--source", "stdin", "--config", self.cfgfile(), "--plain", "--exit-at-eof", stdin=data)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Bob_Example", r.stdout)
        self.assertEqual(len(self.decisions()), 1)

    def test_command_source(self):
        r = self.run_cli("--source", "command", "cat " + os.path.join(FX, "call.txt"), "--config", self.cfgfile(),
                         "--plain", "--exit-at-eof")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("staging numbers", r.stdout)
        self.assertEqual(len(self.decisions()), 1)

    def test_file_source_follows_appends_and_whole_file_rewrites(self):
        p = os.path.join(self.tmp, "live.md")
        with open(p, "w") as f:
            f.write("[00:00:01] Alice_Example: first line here\n")
        proc = subprocess.Popen([sys.executable, LISTEN, "--source", "file", p, "--config", self.cfgfile(),
                                 "--plain", "--poll", "0.2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=dict(os.environ))
        try:
            time.sleep(1.0)
            with open(p, "a") as f:
                f.write("[00:00:05] Bob_Example: second line here\n")
            time.sleep(1.0)
            # whole-file rewrite (how a live transcriber updates its file): old lines must not replay
            with open(p + ".tmp", "w") as f:
                f.write("[00:00:01] Alice_Example: first line here\n[00:00:05] Bob_Example: second line here\n"
                        "[00:00:09] Alice_Example: third line here\n")
            os.replace(p + ".tmp", p)
            time.sleep(1.0)
        finally:
            proc.terminate()
            out, err = proc.communicate(timeout=10)
        self.assertEqual(out.count("first line here"), 1, out)
        self.assertEqual(out.count("second line here"), 1, out)
        self.assertEqual(out.count("third line here"), 1, out)

    def test_whosaid_live_shape_through_file_source_with_rewrite(self):
        p = os.path.join(self.tmp, "live.jsonl")
        with open(os.path.join(FX, "whosaid-live.jsonl")) as f:
            rows = f.read().splitlines()
        with open(p, "w") as f:
            f.write("\n".join(rows[:2]) + "\n")
        proc = subprocess.Popen([sys.executable, LISTEN, "--source", "file", p, "--config", self.cfgfile(),
                                 "--plain", "--poll", "0.2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=dict(os.environ))
        try:
            time.sleep(1.0)
            with open(p + ".tmp", "w") as f:  # the live file is rewritten whole, meta line included
                f.write("\n".join(rows) + "\n")
            os.replace(p + ".tmp", p)
            time.sleep(1.5)
        finally:
            proc.terminate()
            out, err = proc.communicate(timeout=10)
        self.assertEqual(out.count("can we get the retry change into this week?"), 1, out)  # each line once across the rewrite
        self.assertIn("Bob_Example", out)
        self.assertEqual(len(self.decisions()), 1)
        self.assertIn("staging numbers", "\n".join(m["text"] for m in self.orch()))
        self.assertNotIn("meta", out)

    def test_source_failure_shown_once_and_keeps_running(self):
        proc = subprocess.Popen([sys.executable, LISTEN, "--source", "file", os.path.join(self.tmp, "missing.md"),
                                 "--config", self.cfgfile(), "--plain", "--poll", "0.2"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        time.sleep(2.0)
        alive = proc.poll() is None
        proc.terminate()
        out, err = proc.communicate(timeout=10)
        self.assertTrue(alive, out + err)
        self.assertEqual(out.count("STATUS source: file error"), 1, out)

    def test_failing_command_source_status_once(self):
        r = self.run_cli("--source", "command", "echo '[00:00:01] Alice_Example: hello there'; exit 3",
                         "--config", self.cfgfile(), "--plain", "--exit-at-eof")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello there", r.stdout)
        self.assertEqual(r.stdout.count("STATUS source: command error: exit 3"), 1, r.stdout)

    def test_fleet_listen_subcommand_exists(self):
        r = subprocess.run([FLEET, "listen", "--source", "replay", os.path.join(FX, "call.jsonl"), "--speed", "0",
                            "--config", self.cfgfile(), "--plain", "--exit-at-eof"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SAY THIS", r.stdout)


class PageTest(Base):
    def test_page_loopback_only(self):
        lis = self.replay(self.cfg(glossary={"p95": "the time 95 of every 100 requests finish under"}))
        page = L.Page(lis, 0)
        url = page.start()
        try:
            self.assertEqual(page.server.server_address[0], "127.0.0.1")
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            html = urllib.request.urlopen(url, timeout=5).read().decode()
            self.assertIn("SAY THIS", html)
            self.assertIn("load more", html)
            st = json.loads(urllib.request.urlopen(url + "state.json", timeout=5).read())
            self.assertTrue(any("310 ms" in s["text"] for s in st["say"]))
            self.assertTrue(any(r.get("ref") == "Q1" for r in st["routed"]))
            self.assertTrue(any("95 of every 100" in e for e in json.dumps(st["explainer"]).split("\\n")))
            self.assertNotIn("FAKE0123", json.dumps(st))
            more = json.loads(urllib.request.urlopen(url + "lines.json?before=3&limit=2", timeout=5).read())
            self.assertLessEqual(len(more["lines"]), 2)
            ip = self._lan_ip()
            if ip:
                with self.assertRaises(OSError):
                    socket.create_connection((ip, page.server.server_address[1]), timeout=2).close()
        finally:
            page.stop()

    def test_cli_has_no_host_flag(self):
        r = self.run_cli("--source", "stdin", "--page-host", "0.0.0.0", "--plain", stdin="")
        self.assertNotEqual(r.returncode, 0)

    @staticmethod
    def _lan_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))  # TEST-NET, nothing is sent
            ip = s.getsockname()[0]
            s.close()
            return None if ip.startswith("127.") else ip
        except OSError:
            return None


if __name__ == "__main__":
    unittest.main()
