#!/usr/bin/env python3
"""test_learn_serve.py: `fleet learn serve` (lib/fleet_learn_serve.py, lib/learn-ui.html) on a temp FLEET_STATE.

Phase 7 acceptance (docs/design/four-desks.md section 7): the page is served on loopback with a token (403
without one), and an answer in the page and one in the TUI (`fleet learn quiz`) end in the same state, the same
test shape as test/test_decisions.py. Plus score history per topic, the snapshot read and the verb guards.
Every positive check has a control that must fail. Run: python3 -m unittest test.test_learn_serve -v
"""
import datetime
import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
FLEARN = os.path.join(ROOT, "bin", "fleet-learn")
LIB = os.path.join(ROOT, "lib")
NOW = "2026-11-20T09:00:00Z"
LATER = "2026-11-21T10:00:00Z"   # 25 h after NOW

# Hand-written ACME fixture: 3 fleet rows (topic widget), 2 outside rows (topic widget and docs), so every row
# has at least 3 others and gets a choice card. Quotes are distinct and long enough for a source check.
ROWS = [
    ("decision", "fleet", "decisions-log:Q12", "a: cap at 3, jittered", "Retry cap", "widget"),
    ("decision", "fleet", "decisions-log:Q13", "b: ship the docs fix alone", "Docs fix ships alone", "widget"),
    ("done", "fleet", "route-done:lane-a", "done: upload queue drains in order", "Queue order", "widget"),
    ("change", "outside", "watch:acme/widget#7", "Merged PROJ-1: widget retries use backoff", "PR 7 merged", "widget"),
    ("doc", "outside", "watch:example.com/docs/3", "Page edit: setup needs Python 3.8", "Setup page", "docs"),
]


def scrubbed_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FLEET_") and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    env.update(extra)
    return env


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="learnserve-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.env = {"FLEET_STATE": self.state, "FLEET_LEARN_NOW": NOW}
        self.assertTrue(self.state.startswith(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def learn(self, *args, state=None, now=NOW, inp=None, role=None):
        env = scrubbed_env({"FLEET_STATE": state or self.state, "FLEET_LEARN_NOW": now})
        if role:
            env["FLEET_ROLE"] = role
        return subprocess.run([FLEARN] + list(args), env=env, input=inp, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def fixture(self, state=None):
        for kind, origin, src, quote, title, topic in ROWS:
            p = self.learn("add", "--kind", kind, "--origin", origin, "--source", src, "--quote", quote,
                           "--title", title, "--topic", topic, state=state)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.see_all(state)

    def see_all(self, state=None):
        """Teach before test: show every waiting lesson so the fixture's cards come due."""
        for _ in range(20):
            p = self.learn("lesson", state=state)
            self.assertEqual(p.returncode, 0, p.stderr)
            if "no new lessons" in p.stdout:
                return
        self.fail("lessons never ran out")

    def learn_dir(self, state=None):
        return os.path.join(state or self.state, "learn")

    def cards(self, state=None):
        with open(os.path.join(self.learn_dir(state), "cards.jsonl")) as f:
            return {c["cid"]: c for c in (json.loads(ln) for ln in f if ln.strip())}

    def end_state(self, state):
        """Everything a review writes: reviews (latency dropped: a human's typing speed is not state), the
        card schedules, proof.json, and the snapshot at a fixed clock."""
        d = self.learn_dir(state)
        revs = []
        p = os.path.join(d, "reviews.jsonl")
        if os.path.exists(p):
            with open(p) as f:
                for ln in f:
                    if ln.strip():
                        r = json.loads(ln)
                        r.pop("latencyMs", None)
                        revs.append(r)
        with open(os.path.join(d, "proof.json")) as f:
            proof = json.load(f)
        snap = json.loads(self.learn("snapshot", state=state).stdout)
        snap.pop("dir")
        return {"reviews": revs, "cards": self.cards(state), "proof": proof, "snapshot": snap}


class Server:
    def __init__(self, t, state=None, now=NOW, extra_args=()):
        self.errpath = os.path.join(t.tmp, "serve-%d.err" % id(self))
        self.err = open(self.errpath, "w")
        env = scrubbed_env({"FLEET_STATE": state or t.state, "FLEET_LEARN_NOW": now})
        # the production entry point: bin/fleet -> bin/fleet-learn -> fleet_learn.main -> fleet_learn_serve
        self.p = subprocess.Popen([FLEET, "learn", "serve", "--port", "0"] + list(extra_args), env=env,
                                  stdout=subprocess.PIPE, stderr=self.err, universal_newlines=True)
        line = self.p.stdout.readline()
        m = re.search(r"LEARN UI: http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)", line)
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
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, data, hdrs

    def json(self, method, path, body=None):
        st, data, _ = self.req(method, path, body)
        return st, json.loads(data)

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=10)
        self.p.stdout.close()
        self.err.close()


def correct_answer(c):
    """What a human who knows the material types, per format, in TUI terms."""
    if c["format"] == "choice":
        return "abcd"[c["options"].index(c["answer"])]
    return c["answer"]


# ------------------------------------------------------------------ token and loopback

class TestAccess(Base):
    def setUp(self):
        super().setUp()
        self.fixture()
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()

    def test_loopback_bind_and_token_required(self):
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(self.s.p.pid), "-iTCP", "-sTCP:LISTEN"],
                             stdout=subprocess.PIPE, universal_newlines=True).stdout
        # bin/fleet -> bash -> python: the listener is a child; search the whole tree
        if "127.0.0.1:%d" % self.s.port not in out:
            out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % self.s.port, "-sTCP:LISTEN"],
                                 stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("127.0.0.1:%d" % self.s.port, out)
        self.assertNotIn("*:%d" % self.s.port, out)
        cid = sorted(self.cards())[0]
        # without a token: 403 on every route
        self.assertEqual(self.s.req("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/", token=False)[0], 403)
        self.assertEqual(self.s.req("GET", "/?t=wrong", token=False)[0], 403)
        self.assertEqual(self.s.req("POST", "/api/review", {"cid": cid, "answer": "x"}, token=False)[0], 403)
        self.assertEqual(self.s.req("POST", "/api/review", {"cid": cid, "answer": "x"}, token=False,
                                    headers={"X-Fleet-Token": "wrong"})[0], 403)
        # a query token is not accepted for POST (a link click must not answer a quiz)
        self.assertEqual(self.s.req("POST", "/api/review?t=%s" % self.s.token, {"cid": cid, "answer": "x"},
                                    token=False)[0], 403)
        # a foreign Host (DNS rebinding) and a cross-origin POST are refused even with the token
        self.assertEqual(self.s.req("GET", "/api/state", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.s.req("POST", "/api/review", {"cid": cid, "answer": "x"},
                                    headers={"Origin": "http://evil.example.com"})[0], 403)
        self.assertFalse(os.path.exists(os.path.join(self.learn_dir(), "reviews.jsonl")),
                         "a refused request wrote a review")
        # controls: the same requests with the token succeed
        self.assertEqual(self.s.req("GET", "/?t=%s" % self.s.token, token=False)[0], 200)
        self.assertEqual(self.s.req("GET", "/api/state")[0], 200)
        self.assertEqual(self.s.req("GET", "/api/state", headers={"Host": "localhost:%d" % self.s.port})[0], 200)

    def test_url_file_is_private_and_matches(self):
        p = os.path.join(self.state, "learn-ui.url")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        with open(p) as f:
            self.assertIn("t=%s" % self.s.token, f.read())

    def test_page_shape(self):
        st, html, hdrs = self.s.req("GET", "/?t=%s" % self.s.token, token=False)
        self.assertEqual(st, 200)
        self.assertIn("<title>LEARN</title>", html)
        for section in ("Quiz", "Score history per topic", "Heard only", "Recent reviews"):
            self.assertIn(section, html)
        self.assertIn("frame-ancestors 'none'", hdrs.get("Content-Security-Policy", ""))
        self.assertEqual(hdrs.get("Cache-Control"), "no-store")
        self.assertNotIn(self.s.token, html)

    def test_port_in_use_is_a_one_line_error(self):
        p = subprocess.run([FLEET, "learn", "serve", "--port", str(self.s.port)], env=scrubbed_env(self.env),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("can't listen", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class TestServeGuards(Base):
    def run_serve(self, *args, role=None):
        env = scrubbed_env(self.env)
        if role:
            env["FLEET_ROLE"] = role
        return subprocess.run([FLEET, "learn", "serve", "--port", "0"] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)

    def test_non_loopback_bind_refused(self):
        p = self.run_serve("--bind", "0.0.0.0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("loopback only", p.stderr)

    def test_lane_roles_cannot_serve_the_quiz(self):
        for role in ("learn", "worker", "decide"):
            p = self.run_serve(role=role)
            self.assertEqual(p.returncode, 4, role)
            self.assertIn("only the human takes LEARN quizzes", p.stderr)
        # control: with no role, the human's shell, it serves (TestAccess.setUp proves it starts and answers)
        s = Server(self)
        s.stop()


# ------------------------------------------------------------------ reads and verbs

class TestPage(Base):
    def setUp(self):
        super().setUp()
        self.fixture()
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()

    def test_state_is_the_learn_snapshot_and_hides_answers(self):
        st, body = self.s.json("GET", "/api/state")
        self.assertEqual(st, 200)
        cli = json.loads(self.learn("snapshot").stdout)
        self.assertEqual(body["snapshot"], cli)
        self.assertEqual({o for o in body["snapshot"]["byOrigin"]}, {"fleet", "outside"})
        due = json.loads(self.learn("due", "--json").stdout)
        self.assertEqual([c["cid"] for c in body["quiz"]], [c["cid"] for c in due])
        self.assertTrue(body["quiz"])
        for q in body["quiz"]:
            self.assertNotIn("answer", q)
            self.assertNotIn("grounding", q)
            if q["format"] != "choice":
                self.assertNotIn("options", q)
                # control for the hiding check: the full card does carry the quote
                full = self.cards()[q["cid"]]
                self.assertIn(full["answer"], json.dumps(full))
                self.assertNotIn(full["answer"], json.dumps(q))

    def test_reveal_only_for_recall_and_writes_nothing(self):
        cs = self.cards()
        recall = next(c for c in cs.values() if c["format"] == "recall")
        source = next(c for c in cs.values() if c["format"] == "source")
        st, r = self.s.json("POST", "/api/reveal", {"cid": recall["cid"]})
        self.assertEqual((st, r["answer"]), (200, recall["answer"]))
        self.assertEqual(self.s.json("POST", "/api/reveal", {"cid": source["cid"]})[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.learn_dir(), "reviews.jsonl")))

    def test_bad_answers_are_refused_without_writing(self):
        cs = self.cards()
        recall = next(c for c in cs.values() if c["format"] == "recall")
        self.assertEqual(self.s.json("POST", "/api/review", {"cid": recall["cid"], "answer": "x"})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/review", {"cid": recall["cid"], "answer": "x",
                                                             "rating": "meh"})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/review", {"cid": "C999", "answer": "x"})[0], 404)
        self.assertEqual(self.s.json("POST", "/api/review", {"cid": "nope", "answer": "x"})[0], 400)
        self.assertEqual(self.s.req("POST", "/api/review", [recall["cid"]])[0], 400)  # not a JSON object
        self.assertFalse(os.path.exists(os.path.join(self.learn_dir(), "reviews.jsonl")))
        # control: a well-formed answer writes exactly one review
        st, r = self.s.json("POST", "/api/review", {"cid": recall["cid"], "answer": "x", "rating": "good"})
        self.assertEqual((st, r["review"]["grade"]), (200, "pass"))
        with open(os.path.join(self.learn_dir(), "reviews.jsonl")) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1)

    def test_a_second_answer_to_the_same_card_is_409(self):
        src = next(c for c in self.cards().values() if c["format"] == "source")
        st, r = self.s.json("POST", "/api/review", {"cid": src["cid"], "answer": src["answer"]})
        self.assertEqual((st, r["review"]["grade"], r["answer"]), (200, "pass", src["answer"]))
        self.assertEqual(self.s.json("POST", "/api/review", {"cid": src["cid"], "answer": src["answer"]})[0], 409)
        with open(os.path.join(self.learn_dir(), "reviews.jsonl")) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1)

    def test_wrong_source_paste_fails_and_right_one_passes(self):
        cs = [c for c in self.cards().values() if c["format"] == "source"]
        st, r = self.s.json("POST", "/api/review", {"cid": cs[0]["cid"], "answer": "not in the quote at all"})
        self.assertEqual((st, r["review"]["grade"]), (200, "fail"))
        st, r = self.s.json("POST", "/api/review", {"cid": cs[1]["cid"], "answer": cs[1]["answer"]})
        self.assertEqual((st, r["review"]["grade"]), (200, "pass"))


# ------------------------------------------------------------------ the acceptance clause: page == TUI

class TestLessonPage(Base):
    """The Lesson tab: GET /api/lessons is the pane's preview (records nothing); POST /api/seen is the pane's
    seen verb, and only after it do that row's cards join the quiz (teach before test)."""
    def setUp(self):
        super().setUp()
        for kind, origin, src, quote, title, topic in ROWS:
            p = self.learn("add", "--kind", kind, "--origin", origin, "--source", src, "--quote", quote,
                           "--title", title, "--topic", topic)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()

    def seen_path(self):
        return os.path.join(self.learn_dir(), "seen.jsonl")

    def test_lessons_equal_the_pane_and_record_nothing(self):
        st, body = self.s.json("GET", "/api/lessons")
        self.assertEqual(st, 200)
        self.assertEqual(body["waiting"], json.loads(self.learn("lessons", "--json").stdout))
        pane = json.loads(self.learn("lesson", "--preview", "--json", "--topic", body["lesson"]["topic"]).stdout)
        for k in ("topic", "terms", "context", "dropped", "lids"):
            self.assertEqual(body["lesson"][k], pane[k], k)
        for row in body["lesson"]["context"]:
            self.assertTrue(row["quote"] and row["source"], row)
        self.assertFalse(os.path.exists(self.seen_path()))
        self.assertEqual(json.loads(self.learn("due", "--json").stdout), [])
        self.assertEqual(self.s.json("GET", "/api/state")[1]["quiz"], [])

    def test_seen_starts_the_quiz_for_those_rows_only(self):
        lesson = self.s.json("GET", "/api/lessons")[1]["lesson"]
        st, r = self.s.json("POST", "/api/seen", {"lids": lesson["lids"]})
        self.assertEqual((st, r["marked"]), (200, lesson["lids"]))
        quiz = self.s.json("GET", "/api/state")[1]["quiz"]
        self.assertTrue(quiz)
        lid_of = {c["cid"]: c["lid"] for c in self.cards().values()}
        self.assertEqual({lid_of[q["cid"]] for q in quiz}, set(lesson["lids"]))
        with open(self.seen_path()) as f:
            vias = {json.loads(ln)["via"] for ln in f if ln.strip()}
        self.assertEqual(vias, {"page"})
        left = self.s.json("GET", "/api/lessons")[1]["waiting"]
        self.assertNotIn(lesson["topic"], [w["topic"] for w in left])

    def test_bad_seen_requests_are_refused_without_writing(self):
        self.assertEqual(self.s.json("POST", "/api/seen", {"lids": []})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/seen", {"lids": ["L99"]})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/seen", {"lids": "L1"})[0], 400)
        self.assertEqual(self.s.req("POST", "/api/seen", {"lids": ["L1"]}, token=False)[0], 403)
        self.assertFalse(os.path.exists(self.seen_path()))


class TestPageAndTuiSameState(Base):
    def answer_all_in_page(self, state, wrong_cid=None):
        s = Server(self, state=state)
        try:
            st, body = s.json("GET", "/api/state")
            cs = self.cards(state)
            for q in body["quiz"]:
                c = cs[q["cid"]]
                if c["format"] == "recall":
                    self.assertEqual(s.json("POST", "/api/reveal", {"cid": c["cid"]})[0], 200)
                    req = {"cid": c["cid"], "answer": "from memory", "rating": "good"}
                else:
                    req = {"cid": c["cid"], "answer": correct_answer(c)}
                if c["cid"] == wrong_cid:
                    req["answer"] = "d" if req["answer"] != "d" else "a"
                st, r = s.json("POST", "/api/review", dict(req, latencyMs=1234))
                self.assertEqual(st, 200, r)
            self.assertEqual(s.json("GET", "/api/state")[1]["quiz"], [])
        finally:
            s.stop()

    def answer_all_in_tui(self, state, wrong_cid=None):
        due = json.loads(self.learn("due", "--json", state=state).stdout)
        lines = []
        for c in due:
            if c["format"] == "recall":
                lines += ["from memory", "g"]
            elif c["cid"] == wrong_cid:
                a = correct_answer(c)
                lines.append("d" if a != "d" else "a")
            else:
                lines.append(correct_answer(c))
        p = self.learn("quiz", state=state, inp="\n".join(lines) + "\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("card(s) due", p.stdout)
        return p

    def copies(self, n):
        base = os.path.join(self.tmp, "base")
        os.makedirs(base)
        self.fixture(state=base)
        out = []
        for i in range(n):
            d = os.path.join(self.tmp, "copy%d" % i)
            shutil.copytree(base, d)
            out.append(d)
        return out

    def test_page_answer_and_tui_answer_end_in_the_same_state(self):
        page, tui, tui_wrong = self.copies(3)
        choice = next(c for c in self.cards(page).values() if c["format"] == "choice")
        self.answer_all_in_page(page)
        self.answer_all_in_tui(tui)
        a, b = self.end_state(page), self.end_state(tui)
        self.assertTrue(a["reviews"], "no reviews written")
        self.assertEqual(len(a["reviews"]), len(self.cards(page)))
        self.assertEqual(a, b)
        # control: the TUI with one different answer must NOT match (the comparison can fail)
        self.answer_all_in_tui(tui_wrong, wrong_cid=choice["cid"])
        self.assertNotEqual(a, self.end_state(tui_wrong))

    def test_same_state_holds_a_day_later_and_proof_turns_learned(self):
        page, tui = self.copies(2)
        self.answer_all_in_page(page)
        self.answer_all_in_tui(tui)
        # 25 h later every card is due again (1 d step); answer them both ways once more
        for st in (page, tui):
            self.assertTrue(json.loads(self.learn("due", "--json", state=st, now=LATER).stdout))
        s = Server(self, state=page, now=LATER)
        try:
            cs = self.cards(page)
            for q in s.json("GET", "/api/state")[1]["quiz"]:
                c = cs[q["cid"]]
                req = ({"cid": c["cid"], "answer": "from memory", "rating": "good"} if c["format"] == "recall"
                       else {"cid": c["cid"], "answer": correct_answer(c)})
                self.assertEqual(s.json("POST", "/api/review", req)[0], 200)
        finally:
            s.stop()
        due = json.loads(self.learn("due", "--json", state=tui, now=LATER).stdout)
        lines = []
        for c in due:
            lines += ["from memory", "g"] if c["format"] == "recall" else [correct_answer(c)]
        self.learn("quiz", state=tui, now=LATER, inp="\n".join(lines) + "\n")
        a, b = self.end_state(page), self.end_state(tui)
        self.assertEqual(a, b)
        self.assertTrue(all(v["learned"] for v in a["proof"].values()), a["proof"])
        # the page's history now has two days per topic, scores 100 both days
        s = Server(self, state=page, now=LATER)
        try:
            hist = s.json("GET", "/api/state")[1]["history"]
        finally:
            s.stop()
        self.assertTrue(hist)
        for h in hist:
            self.assertEqual([p["day"] for p in h["points"]], ["2026-11-20", "2026-11-21"])
            self.assertEqual([p["score"] for p in h["points"]], [100, 100])


# ------------------------------------------------------------------ score history per topic (in process)

class TestHistory(Base):
    def setUp(self):
        super().setUp()
        self.fixture()
        self._env = dict(os.environ)
        os.environ.pop("FLEET_ROLE", None)
        os.environ["FLEET_STATE"] = self.state
        os.environ.pop("FLEET_LEARN_NOW", None)
        sys.path.insert(0, LIB)
        self.fl = _load("fleet_learn", os.path.join(LIB, "fleet_learn.py"))
        sys.modules["fleet_learn"] = self.fl
        self.srv = _load("fleet_learn_serve", os.path.join(LIB, "fleet_learn_serve.py"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        sys.modules.pop("fleet_learn", None)
        if LIB in sys.path:
            sys.path.remove(LIB)
        super().tearDown()

    def at(self, s):
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)

    def series(self, hist, topic, origin):
        for h in hist:
            if (h["topic"], h["origin"]) == (topic, origin):
                return h["points"]
        return None

    def test_history_is_per_topic_per_origin_per_day(self):
        cs = self.cards()
        rows = {r["lid"]: r for r in self.fl.items()}
        fleet_widget = [c for c in cs.values() if rows[c["lid"]]["origin"] == "fleet"
                        and c["format"] == "source"]
        out_widget = [c for c in cs.values() if rows[c["lid"]]["origin"] == "outside"
                      and rows[c["lid"]]["topic"] == "widget" and c["format"] == "source"][0]
        d1, d2 = self.at(NOW), self.at(LATER)
        # day 1: fleet/widget one pass, one fail (50); outside/widget one fail (0)
        self.fl.review(fleet_widget[0]["cid"], fleet_widget[0]["answer"], now=d1)
        self.fl.review(fleet_widget[1]["cid"], "wrong paste here", now=d1)
        self.fl.review(out_widget["cid"], "wrong paste here", now=d1)
        # day 2: fleet/widget one pass (100)
        self.fl.review(fleet_widget[1]["cid"], fleet_widget[1]["answer"], now=d2)
        hist = self.srv.history(now=d2)
        self.assertEqual(self.series(hist, "widget", "fleet"),
                         [{"day": "2026-11-20", "reviews": 2, "passes": 1, "score": 50},
                          {"day": "2026-11-21", "reviews": 1, "passes": 1, "score": 100}])
        # same topic name, other origin: a separate series, never blended
        self.assertEqual(self.series(hist, "widget", "outside"),
                         [{"day": "2026-11-20", "reviews": 1, "passes": 0, "score": 0}])
        # control: a topic with no reviews has no series
        self.assertIsNone(self.series(hist, "docs", "outside"))
        # older than the window drops out
        self.assertEqual(self.srv.history(now=d2 + datetime.timedelta(days=90)), [])

    def test_retired_rows_keep_their_history(self):
        c = next(c for c in self.cards().values() if c["format"] == "source")
        self.fl.review(c["cid"], c["answer"], now=self.at(NOW))
        self.fl.drop(c["lid"], reason="test")
        topic = self.fl.item(c["lid"])["topic"]
        self.assertTrue(self.series(self.srv.history(now=self.at(NOW)), topic, self.fl.item(c["lid"])["origin"]))


class TestPaneParity(Base):
    """Q698: the page is a superset of the pane. Every pane verb has a page route that works, and the pane keeps a
    full text version of every verb (the phone over mosh)."""
    def setUp(self):
        super().setUp()
        for kind, origin, src, quote, title, topic in ROWS:
            p = self.learn("add", "--kind", kind, "--origin", origin, "--source", src, "--quote", quote,
                           "--title", title, "--topic", topic)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.s = Server(self)

    def tearDown(self):
        self.s.stop()
        super().tearDown()

    def pane_verbs(self):
        sys.path.insert(0, LIB)
        import fleet_learn
        return fleet_learn.cli_verbs()

    def test_every_pane_verb_has_a_page_route_and_the_page_calls_it(self):
        verbs = self.pane_verbs()
        self.assertIn("lesson", verbs)
        st, a = self.s.json("GET", "/api/actions")
        self.assertEqual(st, 200)
        self.assertEqual(sorted(a["actions"]), sorted(verbs))
        with open(os.path.join(LIB, "learn-ui.html")) as f:
            html = f.read()
        for verb, route in a["actions"].items():
            for part in route.split(" + "):
                method, path = part.split(" ", 1)
                path = path.split("?")[0]
                self.assertTrue(path == "/" or '"%s' % path in html, "%s: the page never calls %s" % (verb, path))
                if method == "GET":
                    q = "?topic=widget" if path == "/api/session" else ""
                    self.assertNotEqual(self.s.req("GET", path + q)[0], 404, "%s %s" % (verb, path))
            if route == "POST /api/cli":
                self.assertIn(verb, a["cli"])
        for verb in ("quiz", "session", "serve"):
            self.assertNotIn(verb, a["cli"])
            self.assertEqual(self.s.json("POST", "/api/cli", {"argv": [verb]})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/cli", {"argv": ["nope"]})[0], 400)
        self.assertEqual(self.s.req("POST", "/api/cli", {"argv": ["list"]}, token=False)[0], 403)

    def test_the_pane_keeps_a_text_version_of_every_verb(self):
        help_text = self.learn("help").stdout
        for verb in self.pane_verbs():
            self.assertIn("fleet learn %s" % verb, help_text, verb)

    def test_page_commands_print_what_the_pane_prints(self):
        for argv in (["list"], ["lessons"], ["digest", "--since", "30d", "--preview"], ["entity", "list"],
                     ["sources"], ["due"]):
            st, r = self.s.json("POST", "/api/cli", {"argv": argv})
            pane = self.learn(*argv)
            self.assertEqual((st, r["rc"], r["out"]), (200, pane.returncode, pane.stdout), argv)
        st, r = self.s.json("POST", "/api/cli", {"argv": ["show", "--bogus"]})
        self.assertEqual(st, 200)
        self.assertNotEqual(r["rc"], 0)

    def test_lesson_for_any_waiting_topic(self):
        st, body = self.s.json("GET", "/api/lessons?topic=docs")
        self.assertEqual((st, body["lesson"]["topic"]), (200, "docs"))
        pane = json.loads(self.learn("lesson", "--preview", "--json", "--topic", "docs").stdout)
        self.assertEqual(body["lesson"]["lids"], pane["lids"])

    def test_session_on_one_topic_teaches_then_quizzes_that_topic_only(self):
        since = NOW
        st, s1 = self.s.json("GET", "/api/session?topic=docs&since=" + since)
        self.assertEqual(st, 200)
        self.assertEqual((s1["lesson"]["topic"], s1["quiz"]), ("docs", []))  # teach before test
        self.assertEqual(self.s.json("POST", "/api/seen", {"lids": s1["lesson"]["lids"]})[0], 200)
        s2 = self.s.json("GET", "/api/session?topic=docs&since=" + since)[1]
        self.assertIsNone(s2["lesson"])
        self.assertTrue(s2["quiz"])
        lid_of = {c["cid"]: c for c in self.cards().values()}
        self.assertEqual({lid_of[q["cid"]]["lid"] for q in s2["quiz"]}, set(s1["lesson"]["lids"]))
        c = lid_of[s2["quiz"][0]["cid"]]
        body = {"cid": c["cid"], "answer": correct_answer(c)}
        if c["format"] == "recall":
            body["rating"] = "good"
        self.assertEqual(self.s.json("POST", "/api/review", body)[0], 200)
        s3 = self.s.json("GET", "/api/session?topic=docs&since=" + since)[1]
        self.assertRegex(s3["line"], r"^session docs: \d+ lesson\(s\), 1 card\(s\), 1 pass, 0 fail, 0 learned$")
        self.assertEqual(self.s.json("GET", "/api/session")[0], 400)


if __name__ == "__main__":
    unittest.main()
