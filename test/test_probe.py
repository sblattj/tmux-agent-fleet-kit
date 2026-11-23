#!/usr/bin/env python3
"""Phase 4 probes (docs/design/four-desks.md 2.5, acceptance cell in section 7). Stdlib only, temp FLEET_STATE.

Every positive check has a control that must fail. The live-lane clause (a probe lane can't write or comment) is
test/test_probe_live.sh; this file proves the deny lists and the read verbs statically.

fd-decide-ui's DECIDE gate (fleet_decisions.register_call / pick / on_pick hook) is used when lib/fleet_decisions.py
has it; otherwise a shim with the agreed behavior is installed (PROBE_TEST_GATE says which one ran).
"""
import http.server
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, LIB)

import fleet_mesh as mesh  # noqa: E402
import fleet_probe as P  # noqa: E402
import fleet_probe_contract as C  # noqa: E402

T0 = 1795000000.0  # 2026-11-17, a fixed clock


def _shim_decisions():
    """The agreed fd-decide-ui API, for a checkout without it: register_call stores calls.json, pick marks the call,
    a yes forwards TASK ... by human:<Qn> to ROUTE, then fleet_probe.on_pick runs. No other sends."""
    m = types.ModuleType("fleet_decisions")
    m.SHIM = True

    def meta_path():
        return os.path.join(mesh.state_dir(), "decide", "calls.json")

    def calls_meta():
        try:
            with open(meta_path()) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def register_call(cid, kind, watch=None, task=None, **extra):
        d = calls_meta()
        row = dict(extra, kind=kind, task=task)
        d[cid] = row
        os.makedirs(os.path.dirname(meta_path()), exist_ok=True)
        with open(meta_path(), "w") as f:
            json.dump(d, f)
        return row

    def pick(cid, option=None, **_):
        meta = calls_meta()[cid]
        mesh.mark_decision(cid, option, only_open=True)
        if option == "yes":
            t = meta["task"]
            mesh.deliver(mesh.desk_name(), mesh.orch_name(), 'TASK %s %s "%s" %s by human:%s' % (
                t["eid"], t.get("ref") or "-", t.get("quote", "").replace('"', "'"), t.get("url") or "-", cid))
        import fleet_probe
        fleet_probe.on_pick(cid, option, meta)
        return {"ok": True}

    def human_only(verb):
        if (os.environ.get("FLEET_ROLE") or "") not in ("", "decide"):
            raise PermissionError(verb)
    m.register_call, m.pick, m.calls_meta, m.human_only = register_call, pick, calls_meta, human_only
    return m


def gate():
    try:
        D = importlib.import_module("fleet_decisions")
    except ImportError:
        D = None
    if D is None or not hasattr(D, "register_call"):
        D = _shim_decisions()
        sys.modules["fleet_decisions"] = D
    return D


D = gate()
GATE = "shim" if getattr(D, "SHIM", False) else "fd-decide-ui"


def inbox(name):
    return [r.get("text", "") for r in mesh._parse_lines(os.path.join(mesh.state_dir(), "inbox", "%s.jsonl" % name))]


def logrows():
    return mesh._parse_lines(mesh.decisions_log_path())


def make_repo(base, target):
    d = os.path.join(base, target)
    os.makedirs(d)
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ann Example",
               GIT_AUTHOR_EMAIL="ann@example.com", GIT_COMMITTER_NAME="Ann Example",
               GIT_COMMITTER_EMAIL="ann@example.com")
    g = lambda *a: subprocess.run(["git", "-C", d] + list(a), check=True, env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
    g("init", "-q", "-b", "main")
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# widget\n")
    g("add", "README.md")
    g("commit", "-q", "-m", "first commit")
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write("print('PROJ-1 fix me')\n")
    os.symlink("/etc/hosts", os.path.join(d, "escape"))
    g("add", "app.py", "escape")
    g("commit", "-q", "-m", "PROJ-1: add app")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="probe-test-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.stub = os.path.join(self.tmp, "fleet")
        with open(self.stub, "w") as f:
            f.write('#!/usr/bin/env bash\ncase $1 in\n tick) cat "$FLEET_STATE/tick.json" 2>/dev/null || echo "[]";;\n'
                    ' park) echo "park $*" >> "$FLEET_STATE/park.log";;\n *) exit 9;;\nesac\n')
        os.chmod(self.stub, 0o755)
        self.saved = dict(os.environ)
        for k in ("FLEET_ROLE", "FLEET_NAME", "FLEET_DECIDE", "FLEET_ROUTE", "GH_TOKEN", "GITHUB_TOKEN",
                  "FLEET_PROBE_GH_API", "FLEET_PROBE_GIT_BASE"):
            os.environ.pop(k, None)
        os.environ.update({"FLEET_STATE": self.state, "FLEET_BIN": self.stub, "FLEET_PROBE_NOW": str(T0),
                           "FLEET_CLAUDE_HOME": os.path.join(self.tmp, "claude"), "FLEET_ROLE": "decide"})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def at(self, sec):
        os.environ["FLEET_PROBE_NOW"] = str(T0 + sec)

    def as_role(self, role, name=None):
        os.environ["FLEET_ROLE"] = role
        if name:
            os.environ["FLEET_NAME"] = name
        else:
            os.environ.pop("FLEET_NAME", None)

    def probe(self, target="acme/widget", found=True, summary="PROJ-1 is open", pick=None):
        self.as_role("decide")
        pid, line = P.start(target)
        self.assertTrue(line.startswith("PROBE-REQUEST "), line)
        self.as_role("probe", pid)
        r, _ = P.report(pid, found, summary, ["PROJ-1: add app"], "https://example.com/acme/widget/issues/1")
        self.as_role("decide")
        if pick and r["callId"]:
            D.pick(r["callId"], option=pick)
        return pid, r


class TestRows(Base):
    def write_session(self, sid, lines):
        d = os.path.join(self.tmp, "claude", "projects", "-x")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")

    def test_one_row_per_probe_with_token_counts(self):
        # the lane's session: msg-a appears twice (a streamed message), counted once
        u = {"input_tokens": 100, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 5,
             "output_tokens": 20}
        self.write_session("sid-1", [{"message": {"id": "msg-a", "usage": u}}, {"message": {"id": "msg-a", "usage": u}},
                                     {"message": {"id": "msg-b", "usage": dict(u, output_tokens=7)}}, {"type": "user"}])
        mesh._atomic_write(os.path.join(self.state, "roster.json"),
                           json.dumps({"probe-1": {"cli": "claude", "session_id": "sid-1"}}))
        self.probe(found=False)
        self.probe(found=True)
        self.probe(target="acme/gadget", found=False)
        rs = P.rows()
        self.assertEqual([r["id"] for r in rs], ["probe-1", "probe-2", "probe-3"])
        for r in rs:
            self.assertEqual(C.validate_row(r), [], r)
            self.assertEqual(r["status"], "done")
        self.assertEqual((rs[0]["tokensIn"], rs[0]["tokensOut"]), (230, 27))
        # control: a lane the roster can't map to a session has null counts, never a made-up 0
        self.assertEqual((rs[1]["tokensIn"], rs[1]["tokensOut"]), (None, None))
        self.assertEqual(C.validate_row(dict(rs[0], tokensIn=-1)) != [], True)
        st = P.stats()
        self.assertEqual(st["today"]["count"], 3)
        self.assertEqual(st["today"]["tokensIn"], 230)
        self.assertEqual(st["perTarget"]["acme/widget"]["count"], 2)
        self.assertIn("today 3", P.status_line())

    def test_second_report_refused(self):
        pid, _ = self.probe(found=False)
        self.as_role("probe", pid)
        with self.assertRaises(ValueError):
            P.report(pid, True, "again")
        self.assertEqual(len(P.rows()), 1)

    def test_probe_reports_only_for_itself(self):
        self.as_role("decide")
        pid, _ = P.start("acme/widget")
        self.as_role("probe", "probe-99")
        with self.assertRaises(PermissionError):
            P.report(pid, True, "x")
        self.as_role("probe", pid)  # control
        P.report(pid, False, "nothing")

    def test_one_probe_per_target_at_a_time(self):
        pid, _ = P.start("acme/widget")
        again, line = P.start("acme/widget")
        self.assertEqual(again, pid)
        self.assertTrue(line.startswith("BUSY"))
        other, line2 = P.start("acme/gadget")  # control: another target starts
        self.assertTrue(line2.startswith("PROBE-REQUEST"))
        self.assertEqual(sum(1 for t in inbox("ROUTE") if t.startswith("PROBE-REQUEST")), 2)

    def test_due_uses_every_sec(self):
        mesh._atomic_write(os.path.join(self.state, "decide", "probes.json"), json.dumps(
            {"targets": [{"target": "acme/widget", "what": "open bugs", "everySec": 600},
                         {"target": "acme/gadget", "what": "red CI"}]}))
        self.assertEqual([x["target"] for x in P.due()], ["acme/widget", "acme/gadget"])
        self.probe(found=False)
        self.at(300)
        self.assertEqual([x["target"] for x in P.due()], ["acme/gadget"])
        self.at(700)  # control: past everySec it is due again
        self.assertEqual([x["target"] for x in P.due()], ["acme/widget", "acme/gadget"])


class TestGate(Base):
    def test_task_reaches_route_only_after_a_yes_pick(self):
        pid, r = self.probe(found=True)
        self.assertRegex(r["callId"] or "", r"^Q\d+$")
        self.assertFalse([t for t in inbox("ROUTE") if t.startswith("TASK")], "TASK before any pick")
        self.assertTrue([t for t in inbox("DECIDE") if "[decision %s" % r["callId"] in t], "no call at DECIDE")
        D.pick(r["callId"], option="yes")
        tasks = [t for t in inbox("ROUTE") if t.startswith("TASK")]
        self.assertEqual(len(tasks), 1, tasks)
        self.assertTrue(tasks[0].startswith("TASK probe:%s acme/widget " % pid), tasks[0])
        self.assertTrue(tasks[0].endswith("by human:%s" % r["callId"]), tasks[0])

    def test_control_no_and_nuke_send_no_task(self):
        _, r1 = self.probe(found=True, pick="no")
        _, r2 = self.probe(found=True, pick="nuke")
        self.assertTrue(r1["callId"] and r2["callId"])
        self.assertFalse([t for t in inbox("ROUTE") if t.startswith("TASK")])

    def test_nothing_found_opens_no_call(self):
        n0 = len(mesh.decisions())
        _, r = self.probe(found=False)
        self.assertIsNone(r["callId"])
        self.assertEqual(len(mesh.decisions()), n0)
        self.assertTrue([t for t in inbox("ROUTE") if t.startswith("PROBE-DONE probe-1 found=no")])

    def test_call_registered_as_probe_kind(self):
        pid, r = self.probe(found=True)
        meta = D.calls_meta()[r["callId"]]
        self.assertEqual(meta["kind"], "probe")
        self.assertEqual(meta["target"], "acme/widget")
        self.assertEqual(meta["probeId"], pid)
        self.assertEqual(meta["task"]["eid"], "probe:" + pid)
        dec = [d for d in mesh.decisions() if d["id"] == r["callId"]][0]
        self.assertEqual(dec["options"], ["yes", "no", "nuke"])
        self.assertEqual(dec.get("allow"), ["pick"])


class TestNoAndNuke(Base):
    def test_ten_noes_then_the_next_finding_still_asks(self):
        for _ in range(10):
            _, r = self.probe(found=True, pick="no")
            self.assertTrue(r["callId"])
        _, r = self.probe(found=True)
        self.assertRegex(r["callId"] or "", r"^Q11$")
        self.assertEqual(P.nuked(), {})

    def test_nuke_stops_asks_resume_brings_them_back(self):
        _, r = self.probe(found=True, pick="nuke")
        self.assertIn("acme/widget", P.nuked())
        self.assertEqual(C.validate_nuked(P.nuked()), [])
        out = subprocess.run([os.path.join(ROOT, "bin", "fleet-probe"), "nuked"], stdout=subprocess.PIPE,
                             universal_newlines=True, env=os.environ).stdout
        self.assertIn("NUKED acme/widget", out)
        pid, line = P.start("acme/widget")
        self.assertIsNone(pid)
        self.assertTrue(line.startswith("NUKED"))
        n = len(mesh.decisions())
        P.start("acme/gadget")  # control: another target is still probed
        self.assertEqual(sum(1 for t in inbox("ROUTE") if t.startswith("PROBE-REQUEST")), 2)
        self.assertEqual(len(mesh.decisions()), n)
        P.resume("acme/widget")
        out = subprocess.run([os.path.join(ROOT, "bin", "fleet-probe"), "nuked"], stdout=subprocess.PIPE,
                             universal_newlines=True, env=os.environ).stdout
        self.assertNotIn("acme/widget", out)
        _, r2 = self.probe(found=True)
        self.assertTrue(r2["callId"])
        acts = [(x["action"], x["target"]) for x in logrows() if x.get("action") in ("nuke", "resume")]
        self.assertEqual(acts, [("nuke", "acme/widget"), ("resume", "acme/widget")])
        nk = [x for x in logrows() if x.get("action") == "nuke"][0]
        self.assertEqual(nk["id"], r["callId"])
        self.assertEqual(nk["decidedBy"], "human")

    def test_a_found_report_on_a_nuked_target_opens_no_call(self):
        self.as_role("decide")
        pid, _ = P.start("acme/widget")
        P.nuke("acme/widget")  # nuked while the probe runs
        self.as_role("probe", pid)
        r, note = P.report(pid, True, "found")
        self.assertIsNone(r["callId"])
        self.assertIn("nuked", note)

    def test_nuke_and_resume_are_human_or_decide_only(self):
        self.as_role("route")
        with self.assertRaises(PermissionError):
            P.nuke("acme/widget")
        self.as_role("probe", "probe-1")
        with self.assertRaises(PermissionError):
            P.resume("acme/widget")
        self.as_role("")  # control: the human
        P.nuke("acme/widget")
        P.resume("acme/widget")

    def test_resume_of_a_target_not_nuked_fails(self):
        with self.assertRaises(ValueError):
            P.resume("acme/widget")


class TestLogRows(Base):
    def test_every_alone_action_has_a_decide_probe_row(self):
        self.probe(found=False)
        self.probe(found=True)
        P.fyi("CI on main went green", target="acme/widget")
        rows = logrows()
        seen = {r["action"] for r in rows}
        self.assertEqual(seen, set(C.ALONE_ACTIONS))
        for r in rows:
            self.assertEqual(C.validate_log_row(r), [], r)
            self.assertEqual(r["decidedBy"], C.DECIDED_BY_PROBE)
            self.assertFalse(r["human"])
        self.assertTrue([t for t in inbox("ROUTE") if t.startswith("FYI (decidedBy decide:probe) acme/widget")])
        # control: the validator rejects an alone action marked human
        bad = dict(rows[0], decidedBy="human", human=True)
        self.assertNotEqual(C.validate_log_row(bad), [])

    def test_found_row_carries_the_call_id(self):
        _, r = self.probe(found=True)
        f = [x for x in logrows() if x.get("action") == "probe-found"][0]
        self.assertEqual(f["id"], r["callId"])
        n = [x for x in logrows() if x.get("action") == "probe-start"][0]
        self.assertIsNone(n["id"])


class TestStall(Base):
    def tick(self, lane, last):
        mesh._atomic_write(os.path.join(self.state, "tick.json"),
                           json.dumps([{"name": lane, "status": "live", "lastActive": last}]))

    def test_stalled_after_15_minutes_and_parked(self):
        pid, _ = P.start("acme/widget")
        self.tick(pid, T0)
        self.at(14 * 60)  # control: 14 min of quiet is not a stall
        self.assertEqual(P.stalled(), [])
        self.at(15 * 60 + 1)
        s = P.stalled()
        self.assertEqual([x["id"] for x in s], [pid])
        self.as_role("decide")
        with self.assertRaises(PermissionError):  # only ROUTE (or the human) parks
            P.stalled(apply=True)
        self.as_role("route")
        P.stalled(apply=True)
        with open(os.path.join(self.state, "park.log")) as f:
            self.assertEqual(f.read().strip(), "park park %s --no-compact" % pid)
        self.assertEqual(P.row(pid)["status"], "stalled")
        self.assertEqual(C.validate_row(P.row(pid)), [])
        self.assertEqual(P.stalled(), [])  # parked once

    def test_recent_lane_output_or_verb_call_resets_the_clock(self):
        pid, _ = P.start("acme/widget")
        self.tick(pid, T0 + 600)  # the pane printed at 10 min
        self.at(15 * 60 + 1)
        self.assertEqual(P.stalled(), [])
        self.at(25 * 60 + 1)  # control: 15 min after that output it is stalled
        self.assertEqual(len(P.stalled()), 1)

    def test_done_probe_never_stalls(self):
        pid, _ = self.probe(found=False)
        self.tick(pid, T0)
        self.at(3600)
        self.assertEqual(P.stalled(), [])


class _GH(http.server.BaseHTTPRequestHandler):
    seen = []

    def _rec(self):
        _GH.seen.append((self.command, self.path, self.headers.get("Authorization")))

    def do_GET(self):
        self._rec()
        body = b'{"items":[{"title":"PROJ-1 crash on start"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._rec()
        self.send_response(201)
        self.end_headers()

    def log_message(self, *a):
        pass


class TestReadVerbs(Base):
    def setUp(self):
        super().setUp()
        self.git_base = os.path.join(self.tmp, "remote")
        make_repo(self.git_base, "acme/widget")
        os.environ["FLEET_PROBE_GIT_BASE"] = self.git_base
        pid, _ = P.start("acme/widget")
        self.pid = pid
        self.as_role("probe", pid)

    def test_git_verbs_read_a_throwaway_shallow_clone(self):
        out = P.run_verb("git-log", self.pid, {})
        self.assertIn("PROJ-1: add app", out)
        self.assertIn("PROJ-1 fix me", P.run_verb("read", self.pid, {"path": "app.py"}))
        self.assertIn("app.py", P.run_verb("git-show", self.pid, {"ref": "HEAD"}))
        self.assertIn("app.py", P.run_verb("git-diff", self.pid, {"base": "HEAD~1"}))
        self.assertIn("Ann Example", P.run_verb("git-blame", self.pid, {"path": "README.md"}))
        repo = os.path.join(self.state, "decide", "probes", self.pid, "repo")
        self.assertTrue(os.path.isdir(os.path.join(repo, ".git")))
        self.assertEqual(P.row(self.pid)["status"], "running")
        P.report(self.pid, False, "nothing")
        self.assertFalse(os.path.exists(os.path.join(self.state, "decide", "probes", self.pid)))  # deleted after
        with self.assertRaises(ValueError):  # control: a closed probe reads nothing
            P.run_verb("git-log", self.pid, {})
        log = mesh._parse_lines(os.path.join(self.state, "decide", "probe.log"))
        self.assertEqual([x["verb"] for x in log][:5], ["git-log", "read", "git-show", "git-diff", "git-blame"])
        self.assertTrue(all(x["ok"] for x in log[:5]))
        self.assertFalse(log[-1]["ok"])

    def test_flag_injection_refused(self):
        for ref in ("--output=/tmp/pwn", "-p", "HEAD;rm", "$(id)"):
            with self.assertRaises(ValueError, msg=ref):
                P.run_verb("git-log", self.pid, {"ref": ref})
        with self.assertRaises(ValueError):
            P.run_verb("git-diff", self.pid, {"base": "--no-index", "head": "/etc/hosts"})
        P.run_verb("git-log", self.pid, {"ref": "HEAD~1..HEAD"})  # control: a range is fine

    def test_path_and_symlink_escape_refused(self):
        for p in ("../../../../etc/hosts", "/etc/hosts", "-x", "escape"):
            with self.assertRaises(ValueError, msg=p):
                P.run_verb("read", self.pid, {"path": p})
        self.assertIn("README.md", P.run_verb("read", self.pid, {"path": "."}))  # control

    def test_unknown_verb_and_unknown_probe_refused(self):
        with self.assertRaises(ValueError):
            P.run_verb("git-push", self.pid, {})
        with self.assertRaises(ValueError):
            P.run_verb("git-log", "probe-404", {})

    def test_a_probe_lane_cannot_start_nuke_or_fyi(self):
        for fn, args in ((P.start, ("acme/other",)), (P.nuke, ("acme/other",)), (P.fyi, ("hi",))):
            with self.assertRaises(PermissionError):
                fn(*args)

    def test_clone_env_never_carries_the_human_token(self):
        os.environ["GH_TOKEN"] = "human-write-token"
        self.assertNotIn("GH_TOKEN", P.git_env())
        self.assertNotIn("GITHUB_TOKEN", P.git_env())


class TestGhGet(Base):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _GH)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        super().setUp()
        _GH.seen = []
        os.environ["FLEET_PROBE_GH_API"] = "http://127.0.0.1:%d" % self.srv.server_port
        self.pid, _ = P.start("acme/widget")
        self.as_role("probe", self.pid)

    def watch(self, auth):
        mesh._atomic_write(os.path.join(self.state, "watch.json"), json.dumps({"sources": [], "probe": {"auth": auth}}))

    def test_get_only_and_no_gh_token_fallback(self):
        os.environ["GH_TOKEN"] = "human-write-token"
        os.environ["GITHUB_TOKEN"] = "human-write-token"
        out = P.run_verb("gh-get", self.pid, {"path": "/repos/acme/widget/issues"})
        self.assertIn("PROJ-1 crash", out)
        self.assertEqual(_GH.seen, [("GET", "/repos/acme/widget/issues", None)])

    def test_probe_auth_token_is_sent_and_never_logged(self):
        os.environ["PROBE_READ_TOKEN"] = "read-only-abc"
        self.watch({"type": "bearer", "token": "env:PROBE_READ_TOKEN"})
        P.run_verb("gh-get", self.pid, {"path": "/repos/acme/widget"})
        self.assertEqual(_GH.seen[-1][2], "Bearer read-only-abc")
        with open(os.path.join(self.state, "decide", "probe.log")) as f:
            self.assertNotIn("read-only-abc", f.read())

    def test_literal_probe_token_refused(self):
        self.watch({"type": "bearer", "token": "ghp_literal"})
        with self.assertRaises(RuntimeError):
            P.run_verb("gh-get", self.pid, {"path": "/repos/acme/widget"})
        self.assertEqual(_GH.seen, [])

    def test_unset_probe_token_fails_closed(self):
        os.environ["GH_TOKEN"] = "human-write-token"
        self.watch({"type": "bearer", "token": "env:PROBE_UNSET_TOKEN"})
        with self.assertRaises(RuntimeError):
            P.run_verb("gh-get", self.pid, {"path": "/repos/acme/widget"})
        self.assertEqual(_GH.seen, [])

    def test_bad_paths_refused(self):
        for p in ("repos/acme/widget", "//evil.example.com/x", "/repos/../x", "/a b", "https://evil.example.com/"):
            with self.assertRaises(ValueError, msg=p):
                P.run_verb("gh-get", self.pid, {"path": p})
        self.assertEqual(_GH.seen, [])

    def test_non_loopback_http_base_refused(self):
        os.environ["FLEET_PROBE_GH_API"] = "http://api.example.com"
        with self.assertRaises(ValueError):
            P.gh_api_base()

    def test_ci_status(self):
        out = P.run_verb("ci-status", self.pid, {"ref": "main"})
        self.assertIn("check-runs HTTP 200", out)
        self.assertEqual([s[0] for s in _GH.seen], ["GET", "GET"])


class TestMcp(Base):
    def rpc(self, msgs):
        env = dict(os.environ, FLEET_ROLE="probe", FLEET_NAME="probe-1")
        p = subprocess.run([sys.executable, os.path.join(LIB, "fleet_probe.py"), "serve"], env=env,
                           input="".join(json.dumps(m) + "\n" for m in msgs), stdout=subprocess.PIPE,
                           universal_newlines=True, timeout=30)
        return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]

    def test_tools_list_is_only_read_verbs_and_report(self):
        out = self.rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "fleet-probe")
        names = [t["name"] for t in out[1]["result"]["tools"]]
        want = ["probe_" + v.replace("-", "_") for v in C.READ_VERBS] + ["probe_report"]
        self.assertEqual(names, want)
        for bad in ("write", "post", "comment", "push", "edit", "send", "launch", "decision"):
            self.assertFalse([n for n in names if bad in n], bad)

    def test_report_over_mcp_closes_the_lanes_own_probe(self):
        P.start("acme/widget")
        out = self.rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "probe_report", "arguments": {"found": False, "summary": "quiet"}}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "probe_post_comment", "arguments": {}}}])
        self.assertFalse(out[0]["result"]["isError"], out[0])
        self.assertTrue(out[1]["result"]["isError"])  # control: no such tool
        self.assertEqual(P.row("probe-1")["status"], "done")


class TestCli(Base):
    def run_cli(self, *a, **extra):
        env = {k: v for k, v in os.environ.items() if k != "FLEET_PROBE_ALLOW_UNVERIFIED"}
        env.update(extra)
        return subprocess.run([os.path.join(ROOT, "bin", "fleet-probe")] + list(a), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=30)

    def test_can_host(self):
        for cli in ("claude", "opencode", "kiro"):  # each passed test_probe_live.sh
            p = self.run_cli("can-host", cli)
            self.assertEqual(p.returncode, 0, cli)
            self.assertNotIn("WARN", p.stderr, cli)
        for cli in ("codex", "agy", "nope"):  # fail closed
            p = self.run_cli("can-host", cli)
            self.assertEqual(p.returncode, 1, cli)
            self.assertTrue(p.stdout.startswith("NO "), p.stdout)

    def test_allow_unverified_override(self):
        """A host with config but no live pass (simulated: opencode dropped from PROBE_ALLOWED) is refused unless the
        test-only override names it; the override is logged and never unlocks a refused host."""
        import io
        import fleet_probe as P
        saved, env = P.PROBE_ALLOWED, os.environ.get(P.ALLOW_UNVERIFIED_ENV)
        P.PROBE_ALLOWED = ("claude",)
        err = io.StringIO()
        try:
            os.environ.pop(P.ALLOW_UNVERIFIED_ENV, None)
            ok, why = P.can_host("opencode")
            self.assertFalse(ok)
            self.assertIn("unverified", why)
            os.environ[P.ALLOW_UNVERIFIED_ENV] = "kiro"  # control: set for another CLI, ignored for opencode
            self.assertFalse(P.can_host("opencode")[0])
            os.environ[P.ALLOW_UNVERIFIED_ENV] = "opencode,kiro"
            saved_err, sys.stderr = sys.stderr, err
            try:
                self.assertTrue(P.can_host("opencode")[0])
            finally:
                sys.stderr = saved_err
            self.assertIn("WARN FLEET_PROBE_ALLOW_UNVERIFIED=opencode,kiro", err.getvalue())  # every use is logged
            for cli in ("codex", "agy"):  # never unlocks a refused host
                os.environ[P.ALLOW_UNVERIFIED_ENV] = cli
                self.assertFalse(P.can_host(cli)[0], cli)
        finally:
            P.PROBE_ALLOWED = saved
            if env is None:
                os.environ.pop(P.ALLOW_UNVERIFIED_ENV, None)
            else:
                os.environ[P.ALLOW_UNVERIFIED_ENV] = env

    def test_start_line_and_stats_json(self):
        p = self.run_cli("start", "acme/widget", "--what", "open bugs")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout.strip(), r"^PROBE-REQUEST probe-1 acme/widget \S+/probe-1\.md cli=claude dir=\S+$")
        brief = p.stdout.split()[3]
        with open(brief) as f:
            self.assertIn("open bugs", f.read())
        s = json.loads(self.run_cli("stats", "--json").stdout)
        self.assertEqual(s["running"], ["probe-1"])
        self.assertEqual(self.run_cli("start", "not a target").returncode, 1)  # control
        self.assertIn("probe-1", json.dumps(json.loads(self.run_cli("snapshot").stdout)))

    def test_bad_probes_json_is_reported(self):
        mesh._atomic_write(os.path.join(self.state, "decide", "probes.json"),
                           json.dumps({"targets": [{"target": "acme/widget", "what": "x", "token": "env:X"}]}))
        self.assertEqual(self.run_cli("check").returncode, 1)
        mesh._atomic_write(os.path.join(self.state, "decide", "probes.json"),
                           json.dumps({"targets": [{"target": "acme/widget", "what": "x"}]}))
        self.assertEqual(self.run_cli("check").returncode, 0)  # control


if __name__ == "__main__":
    sys.stderr.write("PROBE_TEST_GATE=%s\n" % GATE)
    unittest.main(verbosity=2)
