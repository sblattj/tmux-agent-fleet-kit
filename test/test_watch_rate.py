"""Tests for the WATCH per-source token bucket (docs/design/four-desks.md 3.6): lib/fleet_watch_rate.py and its use in
lib/fleet_watch.py. Injected clock and sleep, no real sleeps, no network: urllib.request.urlopen is a fake.
Run: python3 -m unittest test/test_watch_rate.py
"""
import datetime
import email.message
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as C  # noqa: E402
import fleet_watch as W  # noqa: E402
import fleet_watch_rate as R  # noqa: E402

T0 = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, sec):  # the engine's injected sleep: moves the fake clock, never blocks
        self.sleeps.append(round(sec, 3))
        self.t = self.t + datetime.timedelta(seconds=sec)


class FakeResp:
    def __init__(self, remaining=None):
        self.headers = email.message.Message()
        if remaining is not None:
            self.headers["X-RateLimit-Remaining"] = str(remaining)

    def read(self):
        return b"[]"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Stands in for urllib.request.urlopen; records every request that would have gone out."""

    def __init__(self, remaining=None, error_at=None):
        self.sent = []
        self.remaining = remaining
        self.error_at = error_at

    def __call__(self, req, *a, **kw):
        self.sent.append(getattr(req, "full_url", req))
        if self.error_at is not None and len(self.sent) == self.error_at:
            h = email.message.Message()
            h["X-RateLimit-Remaining"] = "0"
            raise urllib.error.HTTPError(self.sent[-1], 429, "Too Many Requests", h, None)
        return FakeResp(self.remaining)


def http_source(n_requests):
    """A Source whose poll makes n requests through urllib.request.urlopen, like every HTTP adapter does."""
    class Http(C.Source):
        kind = "command"
        polls = 0

        def poll(self, cursor):
            Http.polls += 1
            for i in range(n_requests):
                with urllib.request.urlopen("https://acme.example/api/%d" % i, timeout=20) as r:
                    r.read()
            return [], "c%d" % Http.polls, None
    return Http


class RateCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="watch-rate-")
        self._env = os.environ.get("FLEET_STATE")
        os.environ["FLEET_STATE"] = self.state
        self._urlopen = urllib.request.urlopen
        self.net = FakeNet()
        urllib.request.urlopen = self.net
        self.clock = Clock()

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        if self._env is None:
            os.environ.pop("FLEET_STATE", None)
        else:
            os.environ["FLEET_STATE"] = self._env
        shutil.rmtree(self.state, ignore_errors=True)

    def cfg(self, per_min=None, **src):
        s = {"name": "api", "kind": "command", "argv": ["true"]}
        s.update(src)
        c = {"me": {"names": ["Human_Example"]}, "sources": [s], "filters": [{"kind": "fyi", "when": {"any": True}}]}
        if per_min is not None:
            c["defaults"] = {"ratePerMin": per_min}
        return c

    def run_engine(self, cfg, n, force=True):
        cls = http_source(n)
        res = W.Engine(self.state, cfg, now=self.clock, sources={"command": cls}, sleep=self.clock.sleep).run_once(
            force=force)
        return res, cls

    def cur(self):
        return json.load(open(os.path.join(self.state, "watch", "cursors.json")))["api"]


class TestBucket(unittest.TestCase):
    def test_refill_and_take(self):
        b = R.TokenBucket(6, tokens=0, at=1000.0)
        self.assertAlmostEqual(b.take(1000.0), 10.0)  # 6/min = one token every 10 s; nothing taken
        self.assertEqual(b.take(1010.0), 0)
        self.assertAlmostEqual(b.tokens, 0.0)
        self.assertEqual(R.TokenBucket(6, tokens=0, at=0).wait_for(10_000), 0)  # refill caps at capacity
        b = R.TokenBucket(6, tokens=0, at=0)
        b.refill(10_000)
        self.assertEqual(b.tokens, 6)

    def test_roundtrip_and_bad_state(self):
        b = R.TokenBucket.from_dict(30, {"tokens": 4.5, "at": 12.0})
        self.assertEqual(b.to_dict(), {"tokens": 4.5, "at": 12.0})
        self.assertEqual(R.TokenBucket.from_dict(30, {"tokens": "x"}).tokens, 30)
        self.assertEqual(R.TokenBucket.from_dict(30, None).tokens, 30)
        self.assertEqual(R.TokenBucket.from_dict(30, {"tokens": 99}).tokens, 30)  # never above capacity

    def test_rate_per_min_precedence(self):
        cfg = {"defaults": {"ratePerMin": 10}}
        self.assertEqual(R.rate_per_min(cfg, {"ratePerMin": 5}, 30), 5)
        self.assertEqual(R.rate_per_min(cfg, {}, 30), 10)
        self.assertEqual(R.rate_per_min({}, {}, 30), 30)
        self.assertEqual(R.rate_per_min({}, {"ratePerMin": -1}, 30), 30)
        self.assertEqual(C.RATE_PER_MIN, 30)  # the spec's default comes from the contract


class TestEngineRate(RateCase):
    def test_under_the_limit_calls_at_once(self):
        res, cls = self.run_engine(self.cfg(), 3)
        self.assertEqual((len(self.net.sent), self.clock.sleeps, res["deferred"], res["polled"]), (3, [], [], ["api"]))
        self.assertEqual(self.cur()["bucket"]["tokens"], 27)  # default 30/min, 3 spent
        self.assertEqual(self.cur()["cursor"], "c1")

    def test_burst_past_the_bucket_defers_without_calling_out(self):
        res, cls = self.run_engine(self.cfg(per_min=6), 8)  # capacity 6, the 7th request is 10 s away (> 5 s)
        self.assertEqual(len(self.net.sent), 6)  # the 7th and 8th never went out
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual((res["deferred"], res["polled"], res["failed"]), (["api"], [], []))
        cur = self.cur()
        self.assertNotIn("cursor", cur)  # the cursor did not move, so the next poll refetches
        self.assertEqual(cur.get("failStreak", 0), 0)  # rate-limited is not failed
        self.assertEqual(cur["nextPollAt"], "2026-11-20T12:01:00Z")  # the wait, floored at POLL_FLOOR_SEC
        log = open(os.path.join(self.state, "watch", "watch.log")).read()
        self.assertIn("deferred api: rate limit", log)
        # control: the same poll under a bigger bucket goes through at once
        shutil.rmtree(os.path.join(self.state, "watch"))
        self.net.sent.clear()
        res, _ = self.run_engine(self.cfg(per_min=10), 8)
        self.assertEqual((len(self.net.sent), res["deferred"], res["polled"]), (8, [], ["api"]))

    def test_short_wait_sleeps_on_the_injected_clock(self):
        t = time.monotonic()
        res, _ = self.run_engine(self.cfg(per_min=60), 62)  # 60 at once, then two 1 s waits
        self.assertEqual(len(self.net.sent), 62)
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])
        self.assertEqual(res["polled"], ["api"])
        self.assertLess(time.monotonic() - t, 2)  # no real sleep
        h = json.load(open(os.path.join(self.state, "watch", "health.json")))["api"]
        self.assertEqual(h["rate"], {"calls": 62, "waitedSec": 2.0, "perMin": 60.0})

    def test_empty_bucket_defers_the_next_poll_before_any_call(self):
        self.run_engine(self.cfg(per_min=6), 6)  # spends the bucket exactly
        self.assertEqual(len(self.net.sent), 6)
        res, cls = self.run_engine(self.cfg(per_min=6), 1)
        self.assertEqual((res["deferred"], cls.polls, len(self.net.sent)), (["api"], 0, 6))  # source not even polled
        # control: a minute later the bucket holds 6 again and the poll goes through
        self.clock.t += datetime.timedelta(seconds=60)
        res, cls = self.run_engine(self.cfg(per_min=6), 6)
        self.assertEqual((res["polled"], cls.polls, len(self.net.sent)), (["api"], 1, 12))

    def test_bucket_survives_between_processes(self):
        self.run_engine(self.cfg(per_min=6), 4)
        self.assertEqual(self.cur()["bucket"]["tokens"], 2)
        self.clock.t += datetime.timedelta(seconds=20)  # +2 tokens
        self.run_engine(self.cfg(per_min=6), 4)
        self.assertEqual(len(self.net.sent), 8)
        self.assertEqual(self.cur()["bucket"]["tokens"], 0)

    def test_ratelimit_remaining_zero_drains_the_bucket(self):
        self.net.remaining = 0
        res, _ = self.run_engine(self.cfg(), 3)  # 30/min: each later request waits 2 s
        self.assertEqual(self.clock.sleeps, [2.0, 2.0])
        self.assertEqual(len(self.net.sent), 3)
        h = json.load(open(os.path.join(self.state, "watch", "health.json")))["api"]
        self.assertEqual(h["rateRemaining"], 0)
        # control: headroom left, no waiting
        shutil.rmtree(os.path.join(self.state, "watch"))
        self.clock.sleeps.clear()
        self.net.remaining = 50
        self.run_engine(self.cfg(), 3)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(json.load(open(os.path.join(self.state, "watch", "health.json")))["api"]["rateRemaining"], 50)

    def test_429_error_headers_drain_too(self):
        self.net.error_at = 1

        class Retry(C.Source):
            kind = "command"

            def poll(self, cursor):
                try:
                    urllib.request.urlopen("https://acme.example/a")
                except urllib.error.HTTPError:
                    pass
                urllib.request.urlopen("https://acme.example/b")
                return [], None, None
        W.Engine(self.state, self.cfg(), now=self.clock, sources={"command": Retry},
                 sleep=self.clock.sleep).run_once()
        self.assertEqual(self.clock.sleeps, [2.0])  # the second call waited for a token after the 429

    def test_per_source_buckets_are_separate(self):
        cfg = self.cfg(per_min=6)
        cfg["sources"].append({"name": "other", "kind": "command", "argv": ["true"]})
        res, _ = self.run_engine(cfg, 6)
        self.assertEqual((sorted(res["polled"]), len(self.net.sent)), (["api", "other"], 12))

    def test_non_http_source_spends_nothing(self):
        cls = type("Quiet", (C.Source,), {"kind": "command", "poll": lambda self, c: ([], None, None)})
        W.Engine(self.state, self.cfg(per_min=6), now=self.clock, sources={"command": cls}).run_once()
        self.assertEqual(self.cur()["bucket"]["tokens"], 6)


class TestGateScope(RateCase):
    def test_no_gate_passes_through(self):
        R.install()
        for _ in range(100):
            urllib.request.urlopen("https://acme.example/x")
        self.assertEqual(len(self.net.sent), 100)

    def test_gate_is_thread_local(self):
        gate = R.Gate(R.TokenBucket(1, tokens=0, at=0.0), lambda: 0.0, lambda s: None)
        out = {}

        def other():
            try:
                urllib.request.urlopen("https://acme.example/other")
                out["ok"] = True
            except R.RateDeferred:
                out["ok"] = False
        with R.gated(gate):
            th = threading.Thread(target=other)
            th.start()
            th.join()
            with self.assertRaises(R.RateDeferred):  # control: this thread is gated and the bucket is empty
                urllib.request.urlopen("https://acme.example/mine")
        self.assertTrue(out["ok"])
        self.assertEqual(self.net.sent, ["https://acme.example/other"])

    def test_real_adapter_requests_go_through_the_gate(self):
        """The shipped HTTP adapters call urllib.request.urlopen by attribute, so the gate sees them."""
        import re
        for name in ("github", "gitlab", "ado", "confluence"):
            p = os.path.join(LIB, "watch_sources", name + ".py")
            if not os.path.exists(p):
                continue
            src = open(p).read()
            self.assertRegex(src, r"urllib\.request\.urlopen\(", name)
            self.assertIsNone(re.search(r"from urllib\.request import[^\n]*urlopen", src), name)


class TestCheckConfig(unittest.TestCase):
    def test_bad_rate_per_min_fails_check(self):
        st = tempfile.mkdtemp(prefix="watch-rate-check-")
        try:
            def check(v):
                cfg = {"sources": [{"name": "x", "kind": "command", "argv": ["true"], "ratePerMin": v}]}
                json.dump(cfg, open(os.path.join(st, "watch.json"), "w"))
                return subprocess.run([os.path.join(ROOT, "bin", "fleet"), "watch", "check"], capture_output=True,
                                      text=True, env=dict(os.environ, FLEET_STATE=st), timeout=30)
            r = check("lots")
            self.assertEqual(r.returncode, 1)
            self.assertIn("ratePerMin must be a positive number", r.stdout + r.stderr)
            self.assertEqual(check(0).returncode, 1)
            self.assertEqual(check(10).returncode, 0)  # control
        finally:
            shutil.rmtree(st, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
