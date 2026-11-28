"""Tests for the AUTH desk verbs (lib/fleet_auth.py via bin/fleet-auth): role guards, one sign-in at a time, the
heads-up to DECIDE before `started`, the number to match shown big and logged, unexpected pushes routed to DECIDE,
freeze/thaw, verdicts that pass only on the human's confirmation, secret refusal, append-only log, trace. Every run
uses a temp FLEET_STATE and a private tmux socket; the calling lane's FLEET_* vars are dropped so the live fleet is
never touched."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FA = os.path.join(ROOT, "bin", "fleet-auth")
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # fake, shaped like a GitHub PAT


def read_jsonl(p):
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def read_text(p):
    with open(p) as f:
        return f.read()


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.st = tempfile.mkdtemp(prefix="authtest.")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_") and k not in ("TMUX", "TMUX_PANE")}
        self.env.update(FLEET_STATE=self.st, FLEET_TMUX_SOCKET="authtest-%d" % os.getpid())

    def tearDown(self):
        shutil.rmtree(self.st, ignore_errors=True)

    def run_fa(self, *args, role="", name="", **extra):
        env = dict(self.env)
        if role:
            env["FLEET_ROLE"] = role
        if name:
            env["FLEET_NAME"] = name
        env.update(extra)
        p = subprocess.run([FA] + list(args), env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def ok(self, *args, **kw):
        rc, out = self.run_fa(*args, **kw)
        self.assertEqual(rc, 0, out)
        return out

    def refused(self, *args, **kw):
        rc, out = self.run_fa(*args, **kw)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("FAIL", out)
        self.assertNotIn("Traceback", out)
        return out

    def log(self):
        return read_jsonl(os.path.join(self.st, "auth", "auth-log.jsonl"))

    def inbox(self, name):
        return read_jsonl(os.path.join(self.st, "inbox", name + ".jsonl"))

    def status(self):
        return json.loads(self.ok("status", "--json"))


class TestEnvIsolation(AuthCase):
    def test_writes_only_under_temp_state(self):
        self.ok("ask", "--what", "cli token expired", role="worker", name="W1")
        self.assertTrue(os.path.exists(os.path.join(self.st, "auth", "auth-log.jsonl")))
        self.assertEqual(self.status()["log"], os.path.join(self.st, "auth", "auth-log.jsonl"))


class TestAsk(AuthCase):
    def test_worker_ask_lands_in_auth_inbox_and_log(self):
        out = self.ok("ask", "--what", "tracker token expired", "--why", "cannot read issues", role="worker", name="W1")
        self.assertIn("OK A1 asked AUTH", out)
        msgs = self.inbox("AUTH")
        self.assertEqual(len(msgs), 1)
        self.assertIn("AUTH-ASK A1 from W1: tracker token expired", json.dumps(msgs[0]))
        r = self.log()[0]
        self.assertEqual((r["id"], r["event"], r["by"]), ("A1", "requested", "W1"))
        self.assertEqual([a["id"] for a in self.status()["openAsks"]], ["A1"])

    def test_fleet_auth_name_override(self):
        self.ok("ask", "--what", "sso expired", role="worker", name="W1", FLEET_AUTH="AUTH2")
        self.assertEqual(len(self.inbox("AUTH2")), 1)
        self.assertEqual(self.inbox("AUTH"), [])

    def test_empty_what_refused(self):
        self.refused("ask", "--what", "  ", role="worker")


class TestRoleGuards(AuthCase):
    def test_only_auth_or_human_begins(self):
        for r in ("worker", "route", "decide", "watch", "learn"):
            out = self.refused("begin", "--what", "sso", role=r)
            self.assertIn("only AUTH runs sign-ins", out)
            self.assertIn("fleet auth ask", out)
        self.assertEqual([x for x in self.log() if x["event"] == "started"], [])
        self.assertEqual(self.inbox("DECIDE"), [])
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")  # control

    def test_only_auth_or_human_ends_and_shows_numbers(self):
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")
        self.refused("number", "42", role="worker")
        self.refused("end", "A1", "--result", "ok", role="worker")
        self.ok("number", "42", role="auth", name="AUTH")
        self.ok("end", "A1", "--result", "ok", role="auth", name="AUTH")

    def test_freeze_thaw_roles(self):
        for r in ("auth", "worker", "watch", "learn"):
            self.refused("freeze", "--reason", "x", role=r)
        self.ok("freeze", "--reason", "hold", role="route", name="ROUTE")
        self.refused("thaw", role="auth")
        self.ok("thaw", role="decide", name="DECIDE")
        self.ok("freeze", "--reason", "hold", role="decide", name="DECIDE")
        self.ok("thaw")  # the human


class TestNoMfaDefaultGate(AuthCase):
    def test_begin_needs_no_recorded_default(self):
        """Pushes are allowed now: a sign-in starts with nothing recorded about the MFA method."""
        out = self.ok("begin", "--what", "sso", role="auth", name="AUTH")
        self.assertIn("OK A1 started", out)
        rc, out = self.run_fa("mfa-default", "code")
        self.assertNotEqual(rc, 0, "the mfa-default verb is gone")


class TestSlot(AuthCase):
    def test_one_at_a_time(self):
        self.ok("begin", "--what", "sso one", role="auth", name="AUTH")
        out = self.refused("begin", "--what", "sso two", role="auth", name="AUTH")
        self.assertIn("one at a time", out)
        self.assertEqual(len([x for x in self.log() if x["event"] == "started"]), 1)
        self.ok("end", "A1", "--result", "ok", role="auth", name="AUTH")
        self.assertIsNone(self.status()["slot"])
        self.ok("begin", "--what", "sso two", role="auth", name="AUTH")  # control: freed slot

    def test_stale_slot_broken_with_warn(self):
        self.ok("begin", "--what", "sso one", role="auth", name="AUTH")
        sp = os.path.join(self.st, "auth", "slot.json")
        s = json.loads(read_text(sp))
        s["at"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with open(sp, "w") as f:
            json.dump(s, f)
        self.refused("begin", "--what", "sso two", role="auth", name="AUTH")  # 120 s < 900 s default
        out = self.ok("begin", "--what", "sso two", role="auth", name="AUTH", FLEET_AUTH_SLOT_SECS="60")
        self.assertIn("WARN broke stale sign-in slot A1", out)
        self.assertIn("slot-broken", [x["event"] for x in self.log()])
        v = self.status()["verdicts"]
        self.assertEqual(v["A1"], "broken")
        self.assertEqual(v["A2"], "open")


class TestHeadsUp(AuthCase):
    def test_heads_up_in_decide_before_started(self):
        self.ok("ask", "--what", "cloud sso expired", role="worker", name="W1")
        out = self.ok("begin", "--what", "cloud sso", "--ask", "A1", "--match", "42", role="auth", name="AUTH")
        self.assertIn("OK A2 started: cloud sso for W1 (password) match 42", out)
        msgs = self.inbox("DECIDE")
        self.assertEqual(len(msgs), 1)
        text = json.dumps(msgs[0])
        self.assertIn("AUTH heads-up: sign-in A2, cloud sso for W1, starting now (fresh password sign-in); "
                      "match 42 on the prompt", text)
        started = [x for x in self.log() if x["event"] == "started"][0]
        self.assertLessEqual(msgs[0].get("ts") or msgs[0].get("at") or "", started["at"])
        self.assertEqual(started["ask"], "A1")
        self.assertEqual(self.status()["openAsks"], [], "the served ask is no longer open")

    def test_session_flow_named_in_heads_up_and_row(self):
        self.ok("begin", "--what", "cloud sso device code", "--flow", "session", role="auth", name="AUTH")
        self.assertIn("reuses a signed-in browser session", json.dumps(self.inbox("DECIDE")))
        self.assertEqual(self.log()[-1]["flow"], "session")
        rc, out = self.run_fa("begin", "--what", "x", "--flow", "magic", role="auth")
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid choice", out)

    def test_heads_up_failure_starts_nothing(self):
        inbox = os.path.join(self.st, "inbox")
        os.makedirs(os.path.join(inbox, "DECIDE.jsonl"))  # a directory: the heads-up can't be written
        self.refused("begin", "--what", "sso", role="auth", name="AUTH")
        self.assertEqual([x for x in self.log() if x["event"] == "started"], [])
        self.assertIsNone(self.status()["slot"])

    def test_bad_match_and_unknown_ask_refused(self):
        self.refused("begin", "--what", "sso", "--match", "4 2;rm", role="auth")
        self.refused("begin", "--what", "sso", "--ask", "A99", role="auth")


class TestNumber(AuthCase):
    def test_number_printed_big_logged_and_sent_to_decide(self):
        self.ok("ask", "--what", "cloud sso expired", role="worker", name="W3")
        self.ok("begin", "--what", "cloud sso", "--ask", "A1", role="auth", name="AUTH")
        out = self.ok("number", "47", role="auth", name="AUTH")
        self.assertIn("MATCH THIS NUMBER (A2, cloud sso for W3", out)
        banner = out.split("MATCH THIS NUMBER", 1)[1]
        self.assertGreaterEqual(sum(1 for ln in banner.splitlines() if "##" in ln), 5, "a 5-row block banner")
        row = [x for x in self.log() if x["event"] == "number-shown"][-1]
        self.assertEqual((row["id"], row["match"], row["for"]), ("A2", "47", "W3"))
        self.assertTrue(row["at"])
        self.assertIn("AUTH A2: match 47 now", json.dumps(self.inbox("DECIDE")))
        self.assertEqual([n["match"] for n in self.status()["numbers"]], ["47"])

    def test_every_number_logged(self):
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")
        self.ok("number", "12", role="auth", name="AUTH")
        self.ok("number", "98", "A1", role="auth", name="AUTH")
        self.assertEqual([x["match"] for x in self.log() if x["event"] == "number-shown"], ["12", "98"])

    def test_begin_with_match_prints_banner(self):
        out = self.ok("begin", "--what", "sso", "--match", "35", role="auth", name="AUTH")
        self.assertIn("MATCH THIS NUMBER", out)

    def test_number_without_signin_refused(self):
        out = self.refused("number", "42", role="auth", name="AUTH")
        self.assertIn("unexpected-push", out)
        self.refused("number", "4;2", role="auth", name="AUTH")


class TestUnexpectedPush(AuthCase):
    def test_announced_push_is_not_an_anomaly(self):
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")
        self.ok("number", "42", role="auth", name="AUTH")
        self.ok("end", "A1", "--result", "ok", role="auth", name="AUTH")
        self.assertEqual(self.status()["anomalies"], [])
        self.assertNotIn("ANOMALY", json.dumps(self.inbox("DECIDE")))

    def test_unannounced_push_logged_and_routed_to_decide(self):
        self.ok("ask", "--what", "sso", role="worker", name="W1")
        out = self.ok("unexpected-push", "--note", "phone buzzed, nothing announced", role="worker", name="W9")
        self.assertIn("push-unexpected logged", out)
        row = self.log()[-1]
        self.assertEqual(row["event"], "push-unexpected")
        self.assertIn("A1 requested by W1", row["near"])
        msg = json.dumps(self.inbox("DECIDE"))
        self.assertIn("AUTH ANOMALY", msg)
        self.assertIn("no sign-in was open", msg)
        self.assertEqual(len(self.status()["anomalies"]), 1)

    def test_decide_needs_quote(self):
        self.refused("unexpected-push", role="decide", name="DECIDE")
        self.ok("unexpected-push", "--quote", "got a push I did not ask for", role="decide", name="DECIDE")

    def test_far_time_has_no_near_rows(self):
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")
        far = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.ok("unexpected-push", "--at", far)
        self.assertEqual(self.log()[-1]["near"], [])


class TestFreeze(AuthCase):
    def test_freeze_blocks_begin_thaw_lifts(self):
        self.ok("freeze", "--reason", "sign-in freeze until re-test", role="route", name="ROUTE")
        out = self.refused("begin", "--what", "sso", role="auth", name="AUTH")
        self.assertIn("frozen", out)
        self.assertIn("sign-in freeze until re-test", out)
        self.ok("ask", "--what", "sso expired", role="worker", name="W1")  # asks still land while frozen
        self.ok("thaw", role="route", name="ROUTE")
        self.refused("thaw", role="route")  # not frozen any more
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")


class TestVerdicts(AuthCase):
    def signin(self, result="ok"):
        self.ok("begin", "--what", "sso", "--for", "W1", role="auth", name="AUTH")
        rid = [x for x in self.log() if x["event"] == "started"][-1]["id"]
        self.ok("end", rid, "--result", result, role="auth", name="AUTH")
        return rid

    def test_end_ok_is_not_a_pass(self):
        rid = self.signin()
        self.assertEqual(self.status()["verdicts"][rid], "ok-unconfirmed")
        self.assertIn("unconfirmed until the human says the prompts were the announced ones", json.dumps(self.inbox("W1")))

    def test_pass_only_on_human_as_expected(self):
        rid = self.signin()
        self.refused("confirm", rid, "--as-expected", role="auth")  # AUTH can't confirm its own run
        self.refused("confirm", rid, "--as-expected", role="decide")  # DECIDE needs the human's words
        self.assertEqual(self.status()["verdicts"][rid], "ok-unconfirmed")
        self.ok("confirm", rid, "--as-expected", "--quote", "only the one I approved", role="decide", name="DECIDE")
        self.assertEqual(self.status()["verdicts"][rid], "pass")

    def test_failed_signin_never_passes(self):
        rid = self.signin("failed")
        self.ok("confirm", rid, "--as-expected")
        self.assertEqual(self.status()["verdicts"][rid], "fail")

    def test_confirm_before_end_refused(self):
        self.ok("begin", "--what", "sso", role="auth", name="AUTH")
        self.refused("confirm", "A1", "--as-expected")

    def test_confirm_unexpected_fails_and_routes(self):
        rid = self.signin()
        self.ok("confirm", rid, "--unexpected")
        s = self.status()
        self.assertEqual(s["verdicts"][rid], "fail")
        self.assertEqual(len(s["anomalies"]), 1)
        self.assertIn("AUTH ANOMALY %s" % rid, json.dumps(self.inbox("DECIDE")))
        self.assertIsNone(s["freeze"], "an anomaly is routed, not an automatic freeze")


class TestSecrets(AuthCase):
    def test_secret_text_refused_everywhere(self):
        self.ok("ask", "--what", "first", role="worker")
        before = len(self.log())
        self.refused("ask", "--what", "use " + TOKEN, role="worker")
        self.refused("ask", "--what", "sso", "--why", TOKEN, role="worker")
        self.refused("begin", "--what", TOKEN, role="auth")
        self.refused("freeze", "--reason", TOKEN)
        self.refused("unexpected-push", "--note", TOKEN)
        self.assertEqual(len(self.log()), before)
        self.assertEqual(len(self.inbox("AUTH")), 1)
        self.assertNotIn(TOKEN, read_text(os.path.join(self.st, "auth", "auth-log.jsonl")))
        self.ok("ask", "--what", "gh token for one owner expired", role="worker")  # control


class TestAppendOnly(AuthCase):
    def test_log_only_grows(self):
        p = os.path.join(self.st, "auth", "auth-log.jsonl")
        prev = ""
        for args, kw in ((("ask", "--what", "sso"), {"role": "worker"}),
                         (("begin", "--what", "sso"), {"role": "auth"}),
                         (("number", "42"), {"role": "auth"}),
                         (("end", "A2", "--result", "ok"), {"role": "auth"}),
                         (("confirm", "A2", "--as-expected"), {}),
                         (("unexpected-push",), {}),
                         (("freeze", "--reason", "r"), {}),
                         (("thaw",), {})):
            self.ok(*args, **kw)
            cur = read_text(p)
            self.assertTrue(cur.startswith(prev) and len(cur) > len(prev), args)
            prev = cur
        self.assertEqual([r["id"] for r in self.log()][:2], ["A1", "A2"])


class TestTraceAndHelpers(AuthCase):
    def test_trace_finds_who_asked(self):
        self.ok("ask", "--what", "sso expired", role="worker", name="W7")
        self.ok("begin", "--what", "sso", "--ask", "A1", role="auth", name="AUTH")
        t = self.log()[-1]["at"]
        rows = json.loads(self.ok("trace", t, "--json"))
        self.assertIn("W7", [r["by"] for r in rows if r["event"] == "requested"])
        far = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(json.loads(self.ok("trace", far, "--json")), [])

    def test_helpers_by_reference(self):
        out = self.ok("helpers")
        self.assertIn("no helpers file", out)
        hp = os.path.join(self.st, "my-helpers.json")
        with open(hp, "w") as f:
            json.dump([{"name": "sso-login", "ref": "~/bin/sso-login-with-code", "use": "expired sso"}], f)
        out = self.ok("helpers", FLEET_AUTH_HELPERS=hp)
        self.assertIn("sso-login\t~/bin/sso-login-with-code\texpired sso", out)

    def test_example_helpers_file_parses(self):
        out = self.ok("helpers", FLEET_AUTH_HELPERS=os.path.join(ROOT, "docs", "auth-helpers.example.json"))
        self.assertIn("cloud-sso-device-login", out)


if __name__ == "__main__":
    unittest.main()
