"""Tests for the DECISIONS UI (lib/fleet_decisions.py, `fleet decisions ...`), issue #1.

Every test runs on a temp FLEET_STATE with stub tmux/codex/fleet binaries and a scrubbed env, so nothing reaches a
live fleet. Run: python3 -m unittest test/test_decisions.py
"""
import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
# `fleet decisions serve` is an alias of the `fleet ui serve` hub (Q700, test/test_ui.py); the server tests here run
# the standalone DECISIONS page the hub mounts at /decide/.
STANDALONE = ["python3", os.path.join(ROOT, "lib", "fleet_decisions.py"), "serve"]
MESH = os.path.join(ROOT, "lib", "fleet_mesh.py")
DECISIONS = os.path.join(ROOT, "lib", "fleet_decisions.py")
DESK = os.path.join(ROOT, "skills", "running-the-decisions-desk", "desk.sh")
GOAL = os.path.join(ROOT, "bin", "fleet-goal")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mesh = _load("fleet_mesh", MESH)

# A fake binary: records argv (one arg per line, then --END--); exits 1 while "<self>.fail" exists.
FAKE = ("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done >> \"$0.argv\"\n"
        "printf -- '--END--\\n' >> \"$0.argv\"\n[ -e \"$0.fail\" ] && { echo stub-fail >&2; exit 1; }\nexit 0\n")

Q1 = "Retry the flaky migration test once, or quarantine it?"
Q2 = "Ship the docs fix alone or bundle it with the next release?"


def scrubbed_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FLEET_") and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dectest-")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        for b in ("tmux", "codex", "fleet", "notify"):
            p = os.path.join(self.bin, b)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.goal = os.path.join(self.state, "desk.goal.json")
        self.env = {"FLEET_STATE": self.state, "FLEET_DESK_GOAL": self.goal, "FLEET_TMUX_SOCKET": "dectest-none",
                    "FLEET_CODEX_BIN": os.path.join(self.bin, "codex"), "FLEET_BIN": os.path.join(self.bin, "fleet"),
                    "FLEET_GOAL_BIN": GOAL, "FLEET_DECIDE": "DECISIONS", "FLEET_ROUTE": "ORCHESTRATOR",
                    "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self._saved = {k: os.environ.get(k) for k in list(self.env) + ["FLEET_NAME", "FLEET_ROLE"]}
        os.environ.update(self.env)
        os.environ.pop("FLEET_NAME", None)
        os.environ.pop("FLEET_ROLE", None)
        self.roster = {
            "lane-a": {"name": "lane-a", "cli": "claude", "mode": "goal", "pane": "%3", "session_id": "u-a"},
            "lane-b": {"name": "lane-b", "cli": "codex", "mode": "goal", "pane": "%4", "session_id": "thr-b"},
            "lane-c": {"name": "lane-c", "cli": "claude", "mode": "goal", "pane": "%5", "session_id": "u-c"},
            "ORCHESTRATOR": {"name": "ORCHESTRATOR", "cli": "claude", "mode": "prompt", "pane": "%8"},
            "DECISIONS": {"name": "DECISIONS", "cli": "claude", "mode": "desk", "pane": "%9"},
        }
        self.write_roster()
        self.fixture()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def write_roster(self):
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump(self.roster, f)

    def fixture(self):
        """Synthetic calls: Q1 lists its recommended option second; Q2 is asked by lane-a and (desk relayto) lane-c."""
        mesh.call_tool("decision_open", {"from": "lane-b", "question": Q1, "options": [
            "quarantine (downside: test stops guarding the migration)",
            "retry once (rec) (downside: hides a real race 1 time in 20)"]})
        mesh.call_tool("decision_open", {"from": "lane-a", "question": Q2, "options": [
            "(a, rec) alone (downside: two small releases)", "bundle (downside: docs wait a week)"]})
        self.run_ok([GOAL, "new", self.goal, "--goal", "Run the decisions desk"])
        self.run_ok([DESK, "add", "Q2", "lane-a", Q2, "lane-a"])
        self.run_ok([DESK, "relayto", "Q2", "add", "lane-c"])
        self.reset_stub_calls()

    def reset_stub_calls(self):
        for b in ("tmux", "codex", "fleet", "notify"):
            try:
                os.remove(os.path.join(self.bin, b + ".argv"))
            except FileNotFoundError:
                pass

    def sh(self, argv, env=None, stdin=None, timeout=60):
        e = scrubbed_env(self.env)
        e.update(env or {})
        return subprocess.run(argv, env=e, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout)

    def run_ok(self, argv, **kw):
        p = self.sh(argv, **kw)
        self.assertEqual(p.returncode, 0, "%s failed: %s %s" % (argv, p.stdout, p.stderr))
        return p

    def fleet(self, *args, **kw):
        return self.sh([FLEET, "decisions"] + list(args), **kw)

    def inbox(self, name):
        p = os.path.join(self.state, "inbox", name + ".jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def answers_in(self, name):
        return [m["text"] for m in self.inbox(name) if m["text"].startswith("[answer ")]

    def log_lines(self):
        p = os.path.join(self.state, "decisions-log.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def rows(self):
        with open(os.path.join(self.state, "decisions.jsonl")) as f:
            return {r["id"]: r for r in (json.loads(x) for x in f if x.strip())}

    def goalfile(self):
        with open(self.goal) as f:
            return json.load(f)

    def state_json(self):
        p = self.fleet("state", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)


# ------------------------------------------------------------------ cards

class TestCards(Base):
    def test_open_calls_are_cards_rec_first_with_downsides_newest_on_top(self):
        st = self.state_json()
        ids = [c["id"] for c in st["open"]]
        self.assertEqual(ids, ["Q2", "Q1"], "newest call on top")
        q1 = st["open"][1]
        self.assertEqual(q1["options"][0]["text"], "retry once")
        self.assertTrue(q1["options"][0]["rec"])
        self.assertEqual(q1["options"][0]["letter"], "a")
        self.assertEqual(q1["options"][0]["downside"], "hides a real race 1 time in 20")
        self.assertEqual(q1["options"][1]["text"], "quarantine")
        self.assertEqual(q1["options"][1]["downside"], "test stops guarding the migration")
        q2 = st["open"][0]
        self.assertEqual(q2["options"][0]["text"], "alone")
        self.assertTrue(q2["options"][0]["rec"])
        self.assertEqual(q2["alsoAskedBy"], ["lane-c"])
        self.assertEqual(q2["askers"], ["lane-a", "lane-c"])

    def test_duplicate_question_from_two_lanes_shows_once(self):
        mesh.call_tool("decision_open", {"from": "lane-c", "question": Q1.upper(), "options": ["retry once"]})
        st = self.state_json()
        self.assertEqual([c["id"] for c in st["open"]], ["Q2", "Q1"])
        q1 = [c for c in st["open"] if c["id"] == "Q1"][0]
        self.assertEqual(q1["alsoAskedBy"], ["lane-c"])
        self.assertEqual(q1["dupIds"], ["Q3"])


# ------------------------------------------------------------------ pick

class TestPick(Base):
    def test_pick_relays_to_every_asker_and_orchestrator_with_via_ui(self):
        p = self.fleet("pick", "Q2", "a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.answers_in("lane-a"), ["[answer Q2] a: alone"])
        self.assertEqual(self.answers_in("lane-c"), ["[answer Q2] a: alone"])
        self.assertEqual(len([m for m in self.inbox("ORCHESTRATOR") if "Q2" in m["text"]]), 1)
        last = self.log_lines()[-1]
        self.assertEqual(last["id"], "Q2")
        self.assertEqual(last["via"], "ui")
        self.assertEqual(last["verbatim"], "a: alone")
        self.assertEqual(last["relayedTo"], ["lane-a", "lane-c"])
        self.assertEqual(last["copiedTo"], ["ORCHESTRATOR"])
        self.assertEqual(len([x for x in self.log_lines() if x["id"] == "Q2"]), 1, "one log line per pick")
        self.assertIn("delivered", last["delivery"]["lane-a"])
        self.assertEqual(self.rows()["Q2"]["status"], "answered")
        self.assertEqual(self.goalfile()["openCalls"][0]["status"], "answered")

    def test_pick_on_a_legacy_openForHuman_desk_file(self):
        # issue #3: a desk goal file that still uses openForHuman is read and written under that key
        g = self.goalfile()
        g["openForHuman"] = g.pop("openCalls")
        with open(self.goal, "w") as f:
            json.dump(g, f)
        p = self.fleet("pick", "Q2", "a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # lane-c is a relay target only in the goal file (desk.sh relayto), so this proves the legacy read
        self.assertEqual(self.answers_in("lane-c"), ["[answer Q2] a: alone"])
        g = self.goalfile()
        self.assertNotIn("openCalls", g)
        self.assertEqual(g["openForHuman"][0]["status"], "answered")

    def test_own_answer_is_verbatim(self):
        text = "b, but only for the staging profile\nand tag it"
        p = self.fleet("pick", "Q1", "--text", text)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.log_lines()[-1]["verbatim"], text)
        self.assertEqual(self.log_lines()[-1]["kind"], "own")
        self.assertEqual(self.rows()["Q1"]["answer"], text)

    def test_second_pick_cannot_relay_twice_even_concurrently(self):
        procs = [subprocess.Popen([FLEET, "decisions", "pick", "Q1", opt], env=scrubbed_env(self.env),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                 for opt in ("a", "b")]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            outs.append((p.returncode, out + err))
        codes = sorted(c for c, _ in outs)
        self.assertEqual(codes, [0, 3], outs)
        self.assertEqual(len(self.answers_in("lane-b")), 1)
        self.assertEqual(len([x for x in self.log_lines() if x["id"] == "Q1"]), 1)
        again = self.fleet("pick", "Q1", "a")
        self.assertEqual(again.returncode, 3)
        self.assertIn("already", again.stdout + again.stderr)
        self.assertEqual(len(self.answers_in("lane-b")), 1)

    def test_worker_role_cannot_pick_for_the_human(self):
        p = self.fleet("pick", "Q1", "a", env={"FLEET_ROLE": "worker", "FLEET_NAME": "lane-b"})
        self.assertEqual(p.returncode, 4, p.stderr)  # the role guard's own exit code, not a crash
        self.assertIn("role worker", p.stderr)
        self.assertEqual(self.answers_in("lane-b"), [])
        self.assertEqual(self.rows()["Q1"]["status"], "open")

    def test_goal_file_only_call_parses_inline_options_relays_once_and_closes(self):
        q = "Rename the flag or keep the old name? (a, rec) rename now (downside: one config edit) (b) keep it"
        self.run_ok([DESK, "add", "Q7", "lane-c", q, "lane-c"])
        card = [c for c in self.state_json()["open"] if c["id"] == "Q7"][0]
        self.assertEqual([o["text"] for o in card["options"]][:2], ["rename now", "keep it"])
        self.assertIn("one config edit", json.dumps(card["options"][0]))
        self.assertTrue(card["options"][0].get("rec"), card["options"][0])
        p = self.fleet("pick", "Q7", "a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.answers_in("lane-c"), ["[answer Q7] a: rename now"])
        item = [g for g in self.goalfile()["openCalls"] if g["id"] == "Q7"][0]
        self.assertEqual(item["status"], "answered")
        self.assertEqual(len([x for x in self.log_lines() if x["id"] == "Q7"]), 1)
        again = self.fleet("pick", "Q7", "b")
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        self.assertEqual(len(self.answers_in("lane-c")), 1)

    def test_goal_only_call_with_failing_desk_sh_cannot_relay_twice(self):
        self.run_ok([DESK, "add", "Q7", "lane-c", "Keep the cache? (a, rec) yes (b) no", "lane-c"])
        shim = os.path.join(self.tmp, "jqshim")  # desk.sh answer's jq edit fails, so the goal item stays open
        os.makedirs(shim)
        with open(os.path.join(shim, "jq"), "w") as f:
            f.write('#!/bin/sh\ncase "$*" in *answered*) exit 5;; esac\nexec %s "$@"\n' % shutil.which("jq"))
        os.chmod(os.path.join(shim, "jq"), 0o755)
        p = self.fleet("pick", "Q7", "a", env={"PATH": shim + os.pathsep + self.env["PATH"]})
        self.assertIn("desk.sh answer Q7 failed", p.stdout + p.stderr)
        item = [g for g in self.goalfile()["openCalls"] if g["id"] == "Q7"][0]
        self.assertEqual(item["status"], "open", "the shim really made desk.sh answer fail")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.answers_in("lane-c")), 1)
        self.assertEqual(len([x for x in self.log_lines() if x["id"] == "Q7"]), 1, "fallback log line")
        self.assertNotIn("Q7", [c["id"] for c in self.state_json()["open"]])
        again = self.fleet("pick", "Q7", "a")
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        self.assertEqual(len(self.answers_in("lane-c")), 1)

    def test_same_id_different_question_in_two_stores_is_not_merged_or_picked(self):
        self.run_ok([DESK, "add", "Q1", "lane-c", "A different call that reuses the id Q1? (a) yes (b) no", "lane-c"])
        card = [c for c in self.state_json()["open"] if c["id"] == "Q1"][0]
        self.assertTrue(card.get("conflict"), card)
        p = self.fleet("pick", "Q1", "a")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("ambiguous", p.stderr)
        self.assertEqual(self.answers_in("lane-b") + self.answers_in("lane-c"), [])
        self.assertEqual(self.rows()["Q1"]["status"], "open")

    def test_allow_is_enforced_server_side_not_only_hidden(self):
        mesh.call_tool("decision_open", {"from": "lane-a", "question": "Pick a region for the demo box?",
                                         "options": ["(a, rec) west", "east"], "allow": ["pick"]})
        qid = [r for r in self.rows().values() if r["question"].startswith("Pick a region")][0]["id"]
        own = self.fleet("pick", qid, "--text", "somewhere else")
        self.assertEqual(own.returncode, 1, own.stdout + own.stderr)
        self.assertIn("does not allow own", own.stderr)
        sn = self.fleet("snooze", qid)
        self.assertEqual(sn.returncode, 1, sn.stdout + sn.stderr)
        self.assertEqual(self.rows()[qid]["status"], "open")
        self.assertEqual(self.fleet("pick", qid, "a").returncode, 0)

    def test_listener_rows_with_null_id_in_the_log_are_tolerated(self):
        row = {"id": None, "at": "2026-11-20T10:00:00Z", "asker": "LISTENER", "verbatim": "ship the docs fix alone",
               "relayedTo": ["ORCHESTRATOR"], "kind": "decided-on-call", "confirmed": False, "from": "LISTENER",
               "clock": "00:12:31"}
        with open(os.path.join(self.state, "decisions-log.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
        snap = self.state_json()
        self.assertEqual(sorted(c["id"] for c in snap["open"]), ["Q1", "Q2"])
        self.assertNotIn(None, [c["id"] for c in snap["called"]])
        f = self.fleet("find", "ship the docs")
        self.assertEqual((f.returncode, f.stdout.strip()), (1, "no past decision matches"), f.stderr)
        self.assertNotIn("Traceback", f.stderr)
        self.assertEqual(self.fleet("refresh", "--json").returncode, 0)
        self.assertEqual(self.fleet("pick", "Q2", "a").returncode, 0)
        self.assertEqual([c["id"] for c in self.state_json()["called"]], ["Q2"])

    def test_called_list_shows_real_delivery_result_and_retry(self):
        open(os.path.join(self.bin, "codex.fail"), "w").close()  # lane-b's wake fails
        p = self.fleet("pick", "Q1", "a")
        self.assertEqual(p.returncode, 0, p.stderr)
        called = {c["id"]: c for c in self.state_json()["called"]}
        self.assertEqual(called["Q1"]["badge"], "failed")
        self.assertIn("FAILED", called["Q1"]["delivery"]["lane-b"])
        self.assertTrue(called["Q1"]["retry"])
        os.remove(os.path.join(self.bin, "codex.fail"))
        r = self.fleet("retry", "Q1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        called = {c["id"]: c for c in self.state_json()["called"]}
        self.assertEqual(called["Q1"]["badge"], "relayed")
        self.assertEqual(len(self.answers_in("lane-b")), 1, "retry re-wakes, it does not duplicate the message")

    def test_queued_and_reopened_badges(self):
        del self.roster["lane-a"]
        self.roster["lane-c"]["status"] = "parked"
        self.write_roster()
        self.fleet("pick", "Q2", "b")
        c = {c["id"]: c for c in self.state_json()["called"]}["Q2"]
        self.assertIn("queued", c["delivery"]["lane-a"])
        self.assertIn("reopened", c["delivery"]["lane-c"])
        self.assertEqual(c["badge"], "queued")

    def test_acted_on_report_moves_the_badge(self):
        self.fleet("pick", "Q1", "a")
        mesh.call_tool("decision_report", {"id": "Q1", "ok": True, "note": "retried, green", "from": "lane-b"})
        c = {c["id"]: c for c in self.state_json()["called"]}["Q1"]
        self.assertEqual(c["acted"], "acted on")
        mesh.call_tool("decision_report", {"id": "Q1", "ok": False, "note": "flaked again", "from": "lane-b"})
        c = {c["id"]: c for c in self.state_json()["called"]}["Q1"]
        self.assertEqual(c["acted"], "act failed")

    def test_snooze_hides_a_call(self):
        self.fleet("snooze", "Q1", "30")
        st = self.state_json()
        self.assertEqual([c["id"] for c in st["open"]], ["Q2"])
        self.assertEqual(st["snoozed"], 1)


# ------------------------------------------------------------------ same state as the desk pane

class TestSameStateAsDesk(Base):
    def snapshot(self):
        rows = {k: {f: v for f, v in r.items() if f not in ("at", "answeredAt", "answerKind", "via")}
                for k, r in self.rows().items()}
        g = self.goalfile()
        ofh = [{f: v for f, v in i.items() if f not in ("at", "answeredAt")} for i in g["openCalls"]]
        decs = [{f: v for f, v in d.items() if f != "at"} for d in g["decisions"]]
        latest = {}
        for ln in self.log_lines():
            latest[ln["id"]] = {f: ln.get(f) for f in ("id", "asker", "verbatim", "relayedTo")}
        inboxes = {n: [m["text"] for m in self.inbox(n)] for n in ("lane-a", "lane-b", "lane-c")}
        return {"rows": rows, "openCalls": ofh, "decisions": decs, "log": latest, "inboxes": inboxes}

    def test_ui_pick_and_desk_pane_answer_end_in_the_same_state(self):
        verbatim = "a: alone"
        # path 1: the UI
        self.assertEqual(self.fleet("pick", "Q2", "a").returncode, 0)
        ui = self.snapshot()
        # path 2: a fresh copy of the same fixture, answered the desk-pane way (decision_answer + peer_send + desk.sh)
        shutil.rmtree(self.state)
        os.makedirs(self.state)
        self.write_roster()
        self.fixture()
        os.environ["FLEET_NAME"] = "DECISIONS"
        mesh.call_tool("decision_answer", {"id": "Q2", "answer": verbatim})
        mesh.call_tool("peer_send", {"to": "lane-c", "text": "[answer Q2] %s" % verbatim})
        os.environ.pop("FLEET_NAME")
        self.run_ok([DESK, "answer", "Q2", verbatim, "lane-a", "lane-c"])
        desk = self.snapshot()
        self.assertEqual(ui, desk)


# ------------------------------------------------------------------ ask / find

class TestAskFind(Base):
    def ask_bg(self, *args):
        env = scrubbed_env(self.env)
        env.update({"FLEET_NAME": "lane-a", "FLEET_ROLE": "worker"})
        return subprocess.Popen([FLEET, "decisions", "ask"] + list(args), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)

    def wait_open(self, n):
        for _ in range(100):
            if "Q%d" % n in self.rows():
                return
            time.sleep(0.1)
        self.fail("Q%d never opened" % n)

    def test_ask_wait_exit_0_on_pick(self):
        p = self.ask_bg("Use the new cache?", "--options", "yes (rec),no", "--wait", "--timeout", "20s")
        self.wait_open(3)
        self.assertEqual(self.fleet("pick", "Q3", "a").returncode, 0)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("a: yes", out)

    def test_ask_wait_exit_1_on_own_answer_or_refusal(self):
        p = self.ask_bg("Use the new cache?", "--options", "yes,no", "--wait", "--timeout", "20s")
        self.wait_open(3)
        self.fleet("pick", "Q3", "--text", "yes, but behind a flag")
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)
        p = self.ask_bg("Drop the old table?", "--options", "yes,no", "--wait", "--timeout", "20s")
        self.wait_open(4)
        self.fleet("pick", "Q4", "--refuse")
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)

    def test_ask_wait_exit_2_on_timeout(self):
        p = self.ask_bg("Anyone there?", "--wait", "--timeout", "1s")
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 2, out + err)
        self.assertIn("no answer yet for Q3", out)  # a real timeout, not a usage error (also exit 2)
        self.assertEqual(self.rows()["Q3"]["status"], "open")

    def test_find_and_exact_repeat_answered_from_the_log(self):
        self.fleet("pick", "Q1", "--text", "retry once, then quarantine if it flakes again")
        f = self.fleet("find", "retry the flaky migration test once")
        self.assertEqual(f.returncode, 0)
        self.assertIn("Q1", f.stdout)
        n_before = len(self.rows())
        p = self.ask_bg(Q1, "--options", "retry,quarantine", "--wait", "--timeout", "5s")
        out, err = p.communicate(timeout=30)
        self.assertEqual(len(self.rows()), n_before, "an exact repeat does not open a new call")
        self.assertIn('"retry once, then quarantine if it flakes again"', out)
        self.assertIn("Q1", out)
        self.assertEqual(p.returncode, 1, "the past answer was own text, so exit 1 like a fresh own answer")


# ------------------------------------------------------------------ refresh all + A/B approval

class TestRefresh(Base):
    def setUp(self):
        super().setUp()
        chat = os.path.join(self.tmp, "chat.jsonl")
        with open(chat, "w") as f:
            f.write(json.dumps({"at": "2026-11-19T16:40:00Z", "from": "human",
                                "text": "Q2: bundle it, no rush"}) + "\n")
            f.write(json.dumps({"at": "2026-11-19T16:41:00Z", "from": "human", "text": "lunch?"}) + "\n")
        notes = os.path.join(self.tmp, "notes.txt")
        with open(notes, "w") as f:
            f.write("nothing about migrations here\n")
        cand = os.path.join(self.tmp, "cands.sh")
        with open(cand, "w") as f:
            f.write("#!/bin/sh\necho '%s'\n" % json.dumps(
                {"id": "Q1", "kind": "answer", "quote": "quarantine it for good", "path": notes, "at": "x"}))
        os.chmod(cand, 0o755)
        with open(os.path.join(self.state, "decisions-ui.json"), "w") as f:
            json.dump({"copyOrchestrator": True, "notify": False, "refreshSources": [
                {"kind": "jsonl", "path": chat, "label": "chat"},
                {"kind": "candidates", "run": cand, "label": "finder"}]}, f)

    def test_refresh_never_relays_without_choice_and_second_approval(self):
        r = self.fleet("refresh", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual([row["id"] for row in res["rows"]], ["Q2"])
        self.assertEqual(res["rows"][0]["b"]["quote"], "Q2: bundle it, no rush")
        self.assertEqual(res["rows"][0]["b"]["source"], "chat")
        self.assertEqual(res["ungrounded"], 1, "the finder's quote is not in notes.txt")
        self.assertTrue(any("refresh" in m["text"].lower() for m in self.inbox("ORCHESTRATOR")))
        self.assertEqual(self.answers_in("lane-a"), [], "refresh relays nothing by itself")
        # a bare confirm is refused
        c = self.fleet("refresh", "confirm", "Q2")
        self.assertNotEqual(c.returncode, 0)
        self.assertEqual(self.answers_in("lane-a"), [])
        # keep A, then confirm: still refused
        self.fleet("refresh", "choose", "Q2", "A")
        self.assertNotEqual(self.fleet("refresh", "confirm", "Q2").returncode, 0)
        self.assertEqual(self.rows()["Q2"]["status"], "open")
        # use B, then confirm: relayed
        self.assertEqual(self.fleet("refresh", "choose", "Q2", "B").returncode, 0)
        self.assertEqual(self.answers_in("lane-a"), [], "choosing B alone relays nothing")
        c = self.fleet("refresh", "confirm", "Q2")
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertEqual(self.answers_in("lane-a"), ["[answer Q2] Q2: bundle it, no rush"])
        last = self.log_lines()[-1]
        self.assertEqual((last["via"], last["kind"]), ("ui", "found"))
        self.assertEqual(last["source"], "chat")


# ------------------------------------------------------------------ web server

class Server:
    def __init__(self, t, extra_args=()):
        self.errpath = os.path.join(t.tmp, "serve.err")
        self.err = open(self.errpath, "w")  # a file, not a pipe: the server logs every request to stderr
        self.p = subprocess.Popen(STANDALONE + ["--port", "0"] + list(extra_args),
                                  env=scrubbed_env(t.env), stdout=subprocess.PIPE, stderr=self.err,
                                  universal_newlines=True)
        line = self.p.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)", line)
        if not m:
            self.stop()
            with open(self.errpath) as f:
                raise AssertionError("no URL line: %r %s" % (line, f.read()))
        self.port, self.token = int(m.group(1)), m.group(2)

    def req(self, method, path, body=None, token=True, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Fleet-Token"] = self.token
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        c.close()
        return r.status, data

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=10)
        self.p.stdout.close()
        self.err.close()


class TestServer(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.state, "decisions-ui.json"), "w") as f:
            json.dump({"notifyCommand": [os.path.join(self.bin, "notify"), "{title}", "{body}", "{url}"]}, f)
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()

    def test_port_in_use_is_a_one_line_error_not_a_traceback(self):
        p = self.sh(STANDALONE + ["--port", str(self.s.port)], timeout=30)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("can't listen", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_loopback_bind_and_token_required(self):
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(self.s.p.pid), "-iTCP", "-sTCP:LISTEN"],
                             stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("127.0.0.1:%d" % self.s.port, out)
        self.assertEqual(self.s.req("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/?t=wrong", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/?t=%s" % self.s.token, token=False)[0], 200)
        self.assertEqual(self.s.req("POST", "/api/pick", {"id": "Q1", "option": "a"}, token=False)[0], 403)
        st, _ = self.s.req("POST", "/api/pick", {"id": "Q1", "option": "a"},
                           headers={"Host": "evil.example:%d" % self.s.port})
        self.assertEqual(st, 403, "foreign Host header refused (DNS rebinding)")
        st, _ = self.s.req("POST", "/api/pick", {"id": "Q1", "option": "a"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403, "foreign Origin refused")
        self.assertEqual(self.answers_in("lane-b"), [], "no refused request relayed anything")
        # the query token is not accepted for POST (a link click must not be able to pick)
        st, _ = self.s.req("POST", "/api/pick?t=%s" % self.s.token, {"id": "Q1", "option": "a"}, token=False)
        self.assertEqual(st, 403)

    def test_pick_over_http_then_duplicate_is_409(self):
        st, body = self.s.req("POST", "/api/pick", {"id": "Q1", "option": "a"})
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["status"], "relayed")
        st, _ = self.s.req("POST", "/api/pick", {"id": "Q1", "option": "b"})
        self.assertEqual(st, 409)
        self.assertEqual(len(self.answers_in("lane-b")), 1)
        last = self.log_lines()[-1]
        self.assertEqual(last["via"], "ui")

    def test_page_shape(self):
        st, html = self.s.req("GET", "/?t=%s" % self.s.token, token=False)
        self.assertEqual(st, 200)
        self.assertIn("@keyframes", html, "new cards animate in")
        self.assertIn("Notification", html)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML =", html.replace("innerHTML = ''", ""), "lane text never goes through innerHTML")

    def test_new_call_pushes_an_event_and_notifies_with_a_deep_link(self):
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=20)
        c.request("GET", "/events?t=%s" % self.s.token)
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type").split(";")[0], "text/event-stream")
        first = r.fp.readline().decode()
        self.assertTrue(first.startswith("event:") or first.startswith("data:"), first)
        mesh.call_tool("decision_open", {"from": "lane-c", "question": "Cut the release tonight?",
                                         "options": ["yes (rec)", "no"]})
        seen = ""
        deadline = time.time() + 15
        while time.time() < deadline and '"Q3"' not in seen:
            seen += r.fp.readline().decode()
        c.close()
        self.assertIn('"Q3"', seen)
        argv_file = os.path.join(self.bin, "notify.argv")
        for _ in range(50):
            if os.path.exists(argv_file):
                break
            time.sleep(0.1)
        with open(argv_file) as f:
            args = f.read()
        self.assertIn("lane-c", args)
        self.assertIn("Cut the release tonight?", args)
        self.assertIn("http://127.0.0.1:%d/?t=%s#Q3" % (self.s.port, self.s.token), args)


# ------------------------------------------------------------------ terminal fallback

class TestTui(Base):
    def test_plain_tui_pick_own_refresh_and_called(self):
        p = self.fleet("tui", "--plain", stdin="j\nb\no hold it until Monday\nv\nr\nq\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # newest first: Q2, Q1; j moves to Q1, b picks quarantine
        self.assertEqual(self.answers_in("lane-b"), ["[answer Q1] b: quarantine"])
        # the cursor is back on the remaining call, Q2: own answer
        self.assertEqual(self.answers_in("lane-a"), ["[answer Q2] hold it until Monday"])
        self.assertIn("Called (2)", p.stdout)
        self.assertRegex(p.stdout, r"Q1 .*b: quarantine.*-> lane-b")
        self.assertIn("refresh:", p.stdout)
        self.assertTrue(any("refresh" in m["text"].lower() for m in self.inbox("ORCHESTRATOR")))

    def test_help_lists_keys(self):
        p = self.fleet("tui", "--plain", stdin="?\nq\n")
        for k in ("j/k", "a/b/c", "o", "r", "v", "?"):
            self.assertIn(k, p.stdout)


# ------------------------------------------------------------------ four desks: WATCH source, task gate, learn this
# docs/design/four-desks.md 2.3, 2.4, 3.7 and the phase 4 and 7 rows of section 7.

PROBE_C = os.path.join(ROOT, "lib", "fleet_probe_contract.py")
HAVE_LEARN = os.path.exists(os.path.join(ROOT, "lib", "fleet_learn.py"))
LEARN_SKIP = "needs lib/fleet_learn.py (branch fd/fleet-learn); lands with it"
EID1 = "github:acme-gh:acme/widget#12:2026-11-20T10:00:00Z:opened"
EID2 = "jira:acme-jira:PROJ-1:2026-11-20T11:00:00Z:created"
TC1 = 'TASK-CANDIDATE %s acme/widget#12 "flaky upload test, can someone fix it" https://github.example.com/acme/widget/issues/12' % EID1
TC2 = 'TASK-CANDIDATE %s PROJ-1 "add a retry cap" https://jira.example.com/browse/PROJ-1' % EID2


def load_decisions():
    """fleet_decisions in this process (for the probe hook test); lib/ goes on sys.path as the CLI does."""
    return _load("fleet_decisions", DECISIONS)


class DeskBase(Base):
    def task_lines(self, name="ORCHESTRATOR"):
        return [m["text"] for m in self.inbox(name) if m["text"].startswith("TASK ")]

    def route_mentions(self, needle, name="ORCHESTRATOR"):
        return [m["text"] for m in self.inbox(name) if needle in m["text"]]

    def intake(self, text, env=None):
        p = self.fleet("intake", text, env=env)
        return p

    def meta(self):
        p = os.path.join(self.state, "decide", "calls.json")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            return json.load(f)

    def card(self, cid):
        for c in self.state_json()["open"]:
            if c["id"] == cid:
                return c
        return None

    def append_row(self, row):
        with open(os.path.join(self.state, "decisions.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")


class TestWatchSource(DeskBase):
    def test_card_shows_watch_source_and_link_from_the_call_row(self):
        self.append_row({"id": "Q3", "at": mesh.now(), "from": "WATCH", "question": "acme-gh acme/widget#12: review?",
                         "options": ["yes", "no"], "status": "open", "answer": None, "answeredAt": None,
                         "watch": {"sourceKind": "github", "source": "acme-gh", "ref": "acme/widget#12",
                                   "url": "https://github.example.com/acme/widget/pull/12", "eid": EID1,
                                   "auth": "env:GH_TOKEN"}})
        c = self.card("Q3")
        self.assertEqual(c["watch"], {"sourceKind": "github", "source": "acme-gh", "ref": "acme/widget#12",
                                      "url": "https://github.example.com/acme/widget/pull/12", "eid": EID1},
                         "only the five WATCH keys survive (no auth)")
        self.assertIsNone(self.card("Q1")["watch"], "control: a lane's call has no WATCH badge")
        p = self.fleet("tui", "--plain", stdin="q\n")
        self.assertIn("[github] acme/widget#12  https://github.example.com/acme/widget/pull/12", p.stdout)

    def test_non_http_url_is_dropped_never_a_link(self):
        for i, url in enumerate(("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "http://ok.example/a")):
            self.append_row({"id": "Q%d" % (3 + i), "at": mesh.now(), "from": "WATCH", "question": "q %d %s" % (i, url),
                             "options": [], "status": "open", "answer": None,
                             "watch": {"sourceKind": "jira", "ref": "PROJ-%d" % i, "url": url}})
        got = {c["id"]: c["watch"] for c in self.state_json()["open"] if c["watch"]}
        self.assertNotIn("url", got["Q3"])
        self.assertNotIn("url", got["Q4"])
        self.assertNotIn("url", got["Q5"])
        self.assertEqual(got["Q6"]["url"], "http://ok.example/a", "control: an http url stays")

    def test_sidecar_registration_gives_a_badge_when_the_row_has_none(self):
        d = load_decisions()
        d.register_call("Q1", "ask", watch={"sourceKind": "call", "ref": "standup 09:00"})
        self.assertEqual(self.card("Q1")["watch"], {"sourceKind": "call", "ref": "standup 09:00"})
        self.assertIsNone(self.card("Q2")["watch"], "control: unregistered call")
        with self.assertRaises(ValueError):
            d.register_call("Q1", "task-candidate")  # a task call without its task is refused
        with self.assertRaises(ValueError):
            d.register_call("bogus", "ask")

    @unittest.skipUnless("watch" in open(MESH).read().split("def t_decision_open", 1)[1].split("\ndef ", 1)[0],
                         "needs the decision_open watch patch (LANDER-NOTES-fd-decide-ui.md)")
    def test_decision_open_passes_watch_through(self):
        mesh.call_tool("decision_open", {"from": "WATCH", "question": "acme/widget#12 review?", "watch": {
            "sourceKind": "github", "ref": "acme/widget#12", "url": "javascript:x", "auth": "env:T"}})
        self.assertEqual(self.rows()["Q3"]["watch"], {"sourceKind": "github", "ref": "acme/widget#12"})


class TestTaskGate(DeskBase):
    def test_task_candidate_reaches_route_only_after_a_yes_pick(self):
        p = self.intake(TC1 + "\n" + TC2)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual(res["calls"], ["Q3", "Q4"])
        # open: ROUTE has heard nothing about either candidate
        self.assertEqual(self.task_lines(), [])
        self.assertEqual(self.route_mentions("acme/widget#12") + self.route_mentions("PROJ-1"), [])
        c3 = self.card("Q3")
        self.assertEqual(c3["task"], "task-candidate")
        self.assertEqual(c3["watch"]["sourceKind"], "github")
        self.assertEqual(c3["watch"]["url"], "https://github.example.com/acme/widget/issues/12")
        self.assertEqual([o["text"] for o in c3["options"]], ["yes: start a writing lane", "learn only", "no: drop"])
        # control: "no" is a pick too, and still sends ROUTE nothing
        self.run_ok([FLEET, "decisions", "pick", "Q4", "c"])
        self.assertEqual(self.task_lines(), [])
        self.assertEqual(self.route_mentions("PROJ-1"), [], "no answer copy of a task call reaches ROUTE")
        # a yes forwards exactly one TASK, with the human's call id
        self.run_ok([FLEET, "decisions", "pick", "Q3", "a"])
        self.assertEqual(self.task_lines(), [
            'TASK %s acme/widget#12 "flaky upload test, can someone fix it" '
            'https://github.example.com/acme/widget/issues/12 by human:Q3' % EID1])
        self.assertEqual(self.inbox("ORCHESTRATOR")[-1]["from"], "DECISIONS")
        p = self.fleet("pick", "Q3", "a")
        self.assertEqual(p.returncode, 3, "second pick is already answered")
        self.assertEqual(len(self.task_lines()), 1, "and forwards nothing more")
        called = {r["id"]: r for r in self.state_json()["called"]}
        self.assertEqual(called["Q3"]["forwarded"], "ORCHESTRATOR")
        self.assertIsNone(called["Q4"]["forwarded"])

    def test_refuse_or_own_words_never_forward(self):
        self.intake(TC1)
        p = self.fleet("pick", "Q3", "--text", "yes")
        self.assertEqual(p.returncode, 1, "a task call takes picks, not own answers")
        self.run_ok([FLEET, "decisions", "pick", "Q3", "--refuse", "yes later maybe"])
        self.assertEqual(self.task_lines(), [])
        self.assertEqual(self.meta()["Q3"]["choice"], "refuse")

    def test_a_lane_answering_a_task_call_forwards_nothing(self):
        self.intake(TC1)
        os.environ["FLEET_NAME"] = "lane-a"
        try:
            mesh.call_tool("decision_answer", {"id": "Q3", "answer": "a: yes: start a writing lane"})
        finally:
            os.environ.pop("FLEET_NAME")
        p = self.fleet("forward")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)[0]["unverified"])
        self.intake(TC2)  # intake sweeps answered task calls too
        self.assertEqual(self.task_lines(), [], "a yes not given by the desk never reaches ROUTE")
        self.assertNotIn("settledAt", self.meta()["Q3"])

    @unittest.skipUnless('"by"' in open(MESH).read().split("def t_decision_answer", 1)[1].split("\ndef ", 1)[0],
                         "needs the decision_answer `by` patch (LANDER-NOTES-fd-decide-ui.md)")
    def test_desk_pane_answer_forwards_once_through_forward(self):
        self.intake(TC1 + "\n" + TC2)
        os.environ["FLEET_NAME"] = "DECISIONS"
        try:
            mesh.call_tool("decision_answer", {"id": "Q3", "answer": "a: yes: start a writing lane"})
            mesh.call_tool("decision_answer", {"id": "Q4", "answer": "yes, but only after the release"})
        finally:
            os.environ.pop("FLEET_NAME")
        self.assertEqual(self.task_lines(), [], "decision_answer alone does not forward")
        self.run_ok([FLEET, "decisions", "forward"])
        self.run_ok([FLEET, "decisions", "forward"])
        lines = self.task_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("by human:Q3"))
        self.assertEqual(self.meta()["Q4"]["choice"], "own", "control: own words that contain yes are not a yes")

    def test_same_candidate_twice_opens_one_call(self):
        self.intake(TC1)
        res = json.loads(self.intake(TC1).stdout)
        self.assertEqual(res["calls"], [])
        self.assertEqual(len([r for r in self.rows().values() if EID1 in json.dumps(r) or "acme/widget#12" in r["question"]]), 1)

    def test_watch_role_cannot_run_intake(self):
        p = self.intake(TC1, env={"FLEET_ROLE": "watch"})
        self.assertEqual(p.returncode, 4)
        self.assertEqual(self.task_lines(), [])
        p = self.intake(TC1, env={"FLEET_ROLE": "decide"})
        self.assertEqual(p.returncode, 0, "control: DECIDE can")

    def test_probe_call_yes_forwards_and_every_choice_reaches_the_probe_hook(self):
        import sys
        import types
        calls = []
        fake = types.ModuleType("fleet_probe")
        fake.on_pick = lambda cid, choice, meta: calls.append((cid, choice, meta.get("target")))
        saved = sys.modules.get("fleet_probe")
        sys.modules["fleet_probe"] = fake
        try:
            d = load_decisions()
            for target in ("acme/widget", "acme/gadget", "acme/gizmo"):
                mesh.call_tool("decision_open", {"from": "DECISIONS", "question": "found work in %s, start a lane?" % target,
                                                 "options": ["yes", "no", "nuke"], "allow": ["pick"]})
            for cid, target in (("Q3", "acme/widget"), ("Q4", "acme/gadget"), ("Q5", "acme/gizmo")):
                d.register_call(cid, "probe", target=target, probeId="P" + cid[1:],
                                task={"eid": "probe:P" + cid[1:], "ref": target, "quote": "3 open good-first-issues"})
            self.assertEqual(self.task_lines(), [])
            d.pick("Q4", option="b")
            d.pick("Q5", option="c")
            self.assertEqual(self.task_lines(), [], "no and nuke forward nothing")
            d.pick("Q3", option="a")
            self.assertEqual(self.task_lines(), ['TASK probe:P3 acme/widget "3 open good-first-issues" - by human:Q3'])
            self.assertEqual(sorted(calls), [("Q3", "yes", "acme/widget"), ("Q4", "no", "acme/gadget"),
                                             ("Q5", "nuke", "acme/gizmo")])
        finally:
            if saved is None:
                sys.modules.pop("fleet_probe", None)
            else:
                sys.modules["fleet_probe"] = saved


class TestFyi(DeskBase):
    def test_fyi_goes_to_route_alone_with_a_decide_probe_row(self):
        probe_c = _load("fleet_probe_contract", PROBE_C)
        self.assertEqual(self.route_mentions("FYI"), [], "control: nothing before intake")
        before = self.state_json()["calledCount"]
        res = json.loads(self.intake("FYI 2 events: %s %s" % (EID1, EID2)).stdout)
        self.assertEqual(len(res["fyi"]), 1)
        self.assertEqual(self.route_mentions("FYI"), ["FYI 2 events: %s %s decidedBy: decide:probe" % (EID1, EID2)])
        rows = [r for r in self.log_lines() if r.get("action") == "fyi-forward"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(probe_c.validate_log_row(rows[0]), [])
        self.assertEqual((rows[0]["decidedBy"], rows[0]["human"]), ("decide:probe", False))
        self.assertEqual(rows[0]["eids"], [EID1, EID2])
        self.assertEqual(self.task_lines(), [], "an fyi is never a TASK")
        self.assertEqual(self.rows().keys(), {"Q1", "Q2"}, "an fyi opens no call")
        self.assertEqual(self.state_json()["calledCount"], before, "an alone row is not a Called answer")
        # the same eids again: nothing new
        self.intake("FYI 1 events: %s" % EID1)
        self.assertEqual(len(self.route_mentions("FYI")), 1)

    def test_probe_action_rows_do_not_overwrite_an_answer_in_called(self):
        self.run_ok([FLEET, "decisions", "pick", "Q1", "b"])
        probe_c = _load("fleet_probe_contract", PROBE_C)
        mesh._append_line(os.path.join(self.state, "decisions-log.jsonl"),
                          json.dumps(probe_c.log_row("nuke", mesh.now(), "acme/widget", call_id="Q1")))
        called = {r["id"]: r for r in self.state_json()["called"]}
        self.assertEqual(called["Q1"]["verbatim"], "b: quarantine")


class LearnBase(DeskBase):
    def ledger(self):
        p = os.path.join(self.state, "learn", "ledger.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]


@unittest.skipUnless(HAVE_LEARN, LEARN_SKIP)
class TestLearnThis(LearnBase):
    def test_tui_learn_key_adds_one_row_with_the_humans_verbatim_words(self):
        self.assertEqual(self.ledger(), [], "control: empty before")
        p = self.fleet("tui", "--plain", stdin="l Q2\no hold it until Monday\nl Q2\nl Q2\nq\n")
        self.assertIn("error: Q2 is still open", p.stdout, "an open call can't be learned")
        rows = self.ledger()
        self.assertEqual(len(rows), 1, rows)
        r = rows[0]
        self.assertEqual(r["quote"], "hold it until Monday")
        self.assertEqual(r["quote"], [x for x in self.log_lines() if x.get("id") == "Q2"][-1]["verbatim"])
        self.assertEqual((r["kind"], r["origin"], r["by"]), ("decision", "fleet", "decide"))
        self.assertEqual(r["source"]["type"], "decide-learn-this")
        self.assertEqual(r["source"]["ref"], "Q2")
        self.assertIn("already in LEARN as %s" % r["lid"], p.stdout, "the second press adds nothing")
        self.assertIn("learned %s" % r["lid"], self.fleet("tui", "--plain", stdin="v\nq\n").stdout)

    def test_learn_this_keeps_the_watch_link(self):
        self.intake(TC1)
        self.run_ok([FLEET, "decisions", "pick", "Q3", "a"])
        self.run_ok([FLEET, "decisions", "learn", "Q3"])
        r = self.ledger()[-1]
        self.assertEqual(r["quote"], "a: yes: start a writing lane")
        self.assertEqual(r["links"], [{"type": "github", "ref": "acme/widget#12", "eid": EID1,
                                       "url": "https://github.example.com/acme/widget/issues/12"}])

    def test_learn_only_sends_the_item_to_learn_and_nothing_to_route(self):
        self.intake(TC1)
        self.run_ok([FLEET, "decisions", "pick", "Q3", "b"])
        self.assertEqual(self.task_lines(), [])
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["origin"], rows[0]["source"]["type"]), ("outside", "watch"))
        self.assertEqual(rows[0]["quote"], "flaky upload test, can someone fix it")


@unittest.skipIf(HAVE_LEARN, "fleet_learn is present")
class TestLearnMissing(LearnBase):
    def test_learn_this_without_fleet_learn_is_a_one_line_error(self):
        self.run_ok([FLEET, "decisions", "pick", "Q1", "b"])
        p = self.fleet("learn", "Q1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("LEARN is not installed", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_learn_only_without_fleet_learn_answers_the_call_and_sends_route_nothing(self):
        self.intake(TC1)
        p = self.fleet("pick", "Q3", "b")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        self.assertIn("LEARN is not installed", p.stdout + p.stderr, "the miss is a visible note")
        self.assertEqual(self.rows()["Q3"]["status"], "answered")
        self.assertEqual(self.task_lines(), [])
        self.assertEqual(self.meta()["Q3"]["choice"], "learn")


class DeskServerBase(LearnBase):
    def setUp(self):
        super().setUp()
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()


class TestDeskPage(DeskServerBase):
    def test_new_routes_need_the_token(self):
        self.run_ok([FLEET, "decisions", "pick", "Q1", "b"])
        self.assertEqual(self.s.req("POST", "/api/learn", {"id": "Q1"}, token=False)[0], 403)
        self.assertEqual(self.s.req("POST", "/api/learn?t=%s" % self.s.token, {"id": "Q1"}, token=False)[0], 403)
        self.assertEqual(self.s.req("POST", "/api/learn", {"id": "Q1"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.ledger(), [], "no refused request wrote to LEARN")

    def test_page_renders_source_badges_and_safe_links(self):
        st, html = self.s.req("GET", "/?t=%s" % self.s.token, token=False)
        self.assertEqual(st, 200)
        for needle in ("srcBadge", "b-src", "safeUrl", "noopener noreferrer", "/api/learn", "Learn this"):
            self.assertIn(needle, html)
        self.assertRegex(html, r"https\?:")
        self.assertNotIn("innerHTML =", html.replace("innerHTML = ''", ""))
        self.intake(TC1)
        st, body = self.s.req("GET", "/api/state")
        c = [x for x in json.loads(body)["open"] if x["id"] == "Q3"][0]
        self.assertEqual((c["watch"]["sourceKind"], c["watch"]["ref"]), ("github", "acme/widget#12"))

    def test_yes_in_the_page_forwards_once_like_the_cli(self):
        self.intake(TC1)
        self.assertEqual(self.task_lines(), [])
        st, body = self.s.req("POST", "/api/pick", {"id": "Q3", "option": "a"})
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["forwarded"], "ORCHESTRATOR")
        self.assertEqual(self.s.req("POST", "/api/pick", {"id": "Q3", "option": "a"})[0], 409)
        self.assertEqual(len(self.task_lines()), 1)

    @unittest.skipUnless(HAVE_LEARN, LEARN_SKIP)
    def test_learn_in_the_page_and_in_the_tui_end_in_the_same_state(self):
        self.run_ok([FLEET, "decisions", "pick", "Q1", "--text", "quarantine it until Friday"])
        self.run_ok([FLEET, "decisions", "pick", "Q2", "--text", "ship the docs fix alone"])
        st, body = self.s.req("POST", "/api/learn", {"id": "Q1"})
        self.assertEqual(st, 200, body)
        self.assertEqual(self.s.req("POST", "/api/learn", {"id": "Q1"})[0], 409, "second press")
        self.fleet("tui", "--plain", stdin="l Q2\nq\n")
        a, b = self.ledger()
        strip = lambda r: {k: v for k, v in r.items() if k not in ("lid", "at", "title", "topic")}
        self.assertEqual(dict(strip(a), source=None, quote=None), dict(strip(b), source=None, quote=None))
        self.assertEqual((a["quote"], b["quote"]), ("quarantine it until Friday", "ship the docs fix alone"))
        st, body = self.s.req("GET", "/api/state")
        learned = {r["id"]: r["learned"] for r in json.loads(body)["called"]}
        self.assertEqual(learned, {"Q1": a["lid"], "Q2": b["lid"]})


if __name__ == "__main__":
    unittest.main()
