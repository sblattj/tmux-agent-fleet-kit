#!/usr/bin/env python3
"""Side-run usage accounting (issue #13): tokens from the result file or the codex session, child counts, fmt.

Pure functions over files: no FLEET_STATE store is read or written. Fixtures live in test/fixtures/side/.
The child-count test uses a throwaway `sh -c 'sleep 30 & sleep 30 & wait'`, never a model CLI.
"""
import os
import signal
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = os.path.join(ROOT, "test", "fixtures", "side")
sys.path.insert(0, os.path.join(ROOT, "lib"))
import fleet_side_usage as U  # noqa: E402

MAIN = "11111111-aaaa-7000-8000-000000000001"


def fx(name):
    return os.path.join(FX, name)


class ResultTokens(unittest.TestCase):
    def test_ach_runresult_sums_records(self):
        u = U.usage({"host": "ach", "result": fx("ach_result.json")})
        # in = input + cacheRead + cacheWrite per record: (1000+300+50) + (2000+0+25)
        self.assertEqual((u["tokensIn"], u["tokensOut"], u["source"]), (3375, 600, "ach"))

    def test_generic_tokens_shape(self):
        u = U.usage({"host": "gemini", "result": fx("generic_result.json")})
        self.assertEqual((u["tokensIn"], u["tokensOut"], u["source"]), (5, 7, "result"))

    def test_missing_or_broken_result_is_none_not_zero(self):
        for r in (fx("nope.json"), fx("broken_result.json"), None):
            u = U.usage({"host": "gemini", "result": r})
            self.assertIsNone(u["tokensIn"], r)
            self.assertIsNone(u["tokensOut"], r)
            self.assertIsNone(u["source"], r)

    def test_children_none_when_unknown(self):
        self.assertIsNone(U.usage({"host": "gemini"})["children"])
        self.assertEqual(U.usage({"host": "gemini", "children": 4})["children"], 4)


class CodexSession(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = fx("codex")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old

    def test_codex_session_plus_helpers(self):
        u = U.usage({"host": "codex", "sessionId": MAIN, "result": fx("nope.json")})
        # main 1000/100 + helper A 200/20 + its helper B 30/3; the unrelated session is not counted
        self.assertEqual((u["tokensIn"], u["tokensOut"], u["source"]), (1230, 123, "codex"))

    def test_codex_unknown_session_is_none(self):
        u = U.usage({"host": "codex", "sessionId": "00000000-0000-0000-0000-00000000dead"})
        self.assertEqual((u["tokensIn"], u["tokensOut"], u["source"]), (None, None, None))

    def test_result_file_wins_over_session(self):
        u = U.usage({"host": "codex", "sessionId": MAIN, "result": fx("generic_result.json")})
        self.assertEqual((u["tokensIn"], u["tokensOut"]), (5, 7))

    def test_control_non_codex_host_ignores_session(self):
        u = U.usage({"host": "gemini", "sessionId": MAIN})
        self.assertIsNone(u["tokensIn"])


class Children(unittest.TestCase):
    def test_sh_plus_two_sleeps_then_zero(self):
        p = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30 & wait"], start_new_session=True)
        try:
            row = {"pid": p.pid}
            n = 0
            for _ in range(50):
                n = U.sample_children(row)
                if n == 3:
                    break
                time.sleep(0.1)
            self.assertEqual(n, 3)
        finally:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        n = 3
        for _ in range(50):
            n = U.sample_children(row)
            if n == 0:
                break
            time.sleep(0.1)
        self.assertEqual(n, 0)

    def test_no_pid_is_zero(self):
        self.assertEqual(U.sample_children({}), 0)


class Fmt(unittest.TestCase):
    ROW = {"host": "codex", "model": "gpt-x", "startedAt": "2026-11-23T10:00:00Z", "boundSec": 1800,
           "tokensIn": 2000000, "tokensOut": 100000, "endedAt": None}
    NOW = 1795428000 + 7 * 60  # 2026-11-23T10:07:00Z

    def test_fmt_fixed_row_and_clock(self):
        self.assertEqual(U.fmt(self.ROW, now=self.NOW), "codex gpt-x 7m/30m 2.1M tok")

    def test_fmt_unknown_tokens(self):
        r = dict(self.ROW, tokensIn=None, tokensOut=None)
        self.assertEqual(U.fmt(r, now=self.NOW), "codex gpt-x 7m/30m ? tok")

    def test_fmt_ended_row_stops_the_clock(self):
        r = dict(self.ROW, endedAt="2026-11-23T10:03:00Z", tokensIn=900, tokensOut=300)
        self.assertEqual(U.fmt(r, now=self.NOW + 3600), "codex gpt-x 3m/30m 1.2k tok")


if __name__ == "__main__":
    unittest.main()
