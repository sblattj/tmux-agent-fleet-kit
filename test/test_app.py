#!/usr/bin/env python3
"""fleet app (lib/fleet_app.py), the text side of Fleet Desk (app/desk-ui): human-only verbs, deduped notifications,
live desk URLs, the phone tunnel (same port, 127.0.0.1 only, Host check intact), and Q698 parity for the app.
No cargo, no network: the Rust source is read as text."""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FLEET = os.path.join(ROOT, "bin", "fleet")
MAIN_RS = os.path.join(ROOT, "app", "desk-ui", "src-tauri", "src", "main.rs")
CONF = os.path.join(ROOT, "app", "desk-ui", "src-tauri", "tauri.conf.json")
SHELL = os.path.join(ROOT, "app", "desk-ui", "shell", "index.html")
sys.path.insert(0, LIB)
import fleet_app as fa  # noqa: E402


def scrubbed_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FLEET_") and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"
           and not k.startswith("TMUX")}
    env.update(extra)
    return env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apptest-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.state, "learn"))
        self.env = scrubbed_env({"FLEET_STATE": self.state, "FLEET_NAME": "tester"})
        self.assertTrue(self.state.startswith(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fleet(self, *args, role=None):
        env = dict(self.env)
        if role:
            env["FLEET_ROLE"] = role
        return subprocess.run([FLEET] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)

    def call(self, frm, q):
        code = "import fleet_mesh as m; m.call_tool('decision_open', {'from': %r, 'question': %r})" % (frm, q)
        p = subprocess.run([sys.executable, "-c", code], cwd=LIB, env=self.env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)

    def lesson(self, title, topic):
        p = self.run_fleet("learn", "add", "--kind", "doc", "--origin", "fleet", "--source", "s-%s" % title,
                           "--quote", "quote for %s" % title, "--title", title, "--topic", topic)
        self.assertEqual(p.returncode, 0, p.stderr)

    def notify(self, *extra):
        p = self.run_fleet("app", "notify", "--json", *extra)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)


class TestHumanOnly(Base):
    def test_every_verb_refuses_a_role(self):
        for verb in ("urls", "serve", "notify", "tunnel"):
            p = self.run_fleet("app", verb, role="worker")
            self.assertEqual(p.returncode, 4, (verb, p.stdout, p.stderr))
            self.assertIn("FAIL role worker", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, fa.SEEN_FILE)))

    def test_human_runs_them(self):
        p = self.run_fleet("app", "urls", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads(p.stdout)
        self.assertEqual([r["name"] for r in rows], [d["name"] for d in fa.DESKS])
        self.assertFalse(any(r["alive"] for r in rows))  # nothing serves in a fresh state


class TestNotify(Base):
    def test_calls_and_lessons_fire_once(self):
        self.call("lane-a", "Ship tonight?")
        self.call("lane-b", "Fixed ports?")
        self.lesson("tunnel", "net")
        self.lesson("loopback", "net")
        self.lesson("dedupe", "app")
        first = self.notify()
        ids = sorted(n["id"].split(":")[0] for n in first)
        self.assertEqual(ids, ["call", "call", "lessons", "lessons"], first)
        self.assertTrue(any(n["title"].startswith("DECIDE: Q") and "Ship tonight?" == n["body"] for n in first))
        net = [n for n in first if n["title"] == "LEARN: lesson waiting on net"]
        self.assertEqual(len(net), 1)
        self.assertIn("2 new lesson row(s)", net[0]["body"])
        # a second poll (a new process, as after an app restart): nothing again
        self.assertEqual(self.notify(), [])
        # a new lesson on a topic already notified fires that topic once, counting only the new row
        self.lesson("ssh agent", "net")
        again = self.notify()
        self.assertEqual(len(again), 1, again)
        self.assertIn("1 new lesson row(s)", again[0]["body"])
        # a new call fires once
        self.call("lane-c", "Rename the app?")
        new = self.notify()
        self.assertEqual([n["body"] for n in new], ["Rename the app?"])
        self.assertEqual(self.notify(), [])
        with open(os.path.join(self.state, fa.SEEN_FILE)) as f:
            seen = json.load(f)["seen"]
        self.assertEqual(sorted(k for k in seen if k.startswith("call:")), ["call:Q1", "call:Q2", "call:Q3"])
        self.assertEqual(len([k for k in seen if k.startswith("lesson:")]), 4)

    def test_dry_run_records_nothing(self):
        self.call("lane-a", "Ship tonight?")
        self.assertEqual(len(self.notify("--dry-run")), 1)
        self.assertEqual(len(self.notify("--dry-run")), 1)
        self.assertEqual(len(self.notify()), 1)
        self.assertEqual(self.notify(), [])

    def test_a_burst_is_one_summary(self):
        for i in range(fa.MAX_BURST + 2):
            self.call("lane-%d" % i, "Question number %d?" % i)
        out = self.notify()
        self.assertEqual(len(out), 1, out)
        self.assertIn("%d new call(s)" % (fa.MAX_BURST + 2), out[0]["body"])
        self.assertEqual(self.notify(), [])

    def test_no_double_fire_when_decisions_serve_notifies(self):
        self.call("lane-a", "Ship tonight?")
        with mock.patch.dict(os.environ, {"FLEET_STATE": self.state}, clear=False):
            os.environ.pop("FLEET_ROLE", None)
            with mock.patch.object(fa, "_decisions_notifies_itself", return_value=True):
                self.assertEqual(fa.notify(), [])  # decisions serve fires it; the app stays quiet
            with mock.patch.object(fa, "_decisions_notifies_itself", return_value=False):
                self.assertEqual(fa.notify(), [])  # and it was marked, so it never fires later either

    def test_a_broken_source_is_skipped(self):
        self.lesson("tunnel", "net")
        with mock.patch.dict(os.environ, {"FLEET_STATE": self.state}, clear=False):
            os.environ.pop("FLEET_ROLE", None)
            with mock.patch.object(fa, "open_calls", side_effect=ValueError("half-written file")):
                out = fa.notify()
        self.assertEqual([n["title"] for n in out], ["LEARN: lesson waiting on net"])


class TestServeAndTunnel(Base):
    def setUp(self):
        super().setUp()
        self.ports = {"ui": free_port()}  # one hub (fleet ui serve, Q700): one port for every desk
        with open(os.path.join(self.state, "app.json"), "w") as f:
            json.dump({"ports": self.ports}, f)
        self.lesson("tunnel", "net")

    def tearDown(self):
        for s in fa.SERVERS:
            for line in subprocess.run(["lsof", "-t", "-iTCP:%d" % self.ports[s], "-sTCP:LISTEN"],
                                       stdout=subprocess.PIPE, universal_newlines=True).stdout.split():
                try:
                    os.kill(int(line), 15)
                except (ValueError, OSError):
                    pass
        time.sleep(0.3)
        super().tearDown()

    def get(self, port, path, host):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": host})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_serve_starts_the_hub_on_loopback(self):
        p = self.run_fleet("app", "serve", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = {r["name"]: r for r in json.loads(p.stdout)}
        port = self.ports["ui"]
        tokens = set()
        for name, tab in (("decisions", "decide"), ("learn", "learn"), ("watch", "watch"), ("route", "route")):
            with open(os.path.join(self.state, "app-ui.log")) as f:
                self.assertTrue(rows[name]["alive"], (name, rows[name], f.read()))
            self.assertTrue(rows[name]["url"].startswith("http://127.0.0.1:%d/%s/?t=" % (port, tab)), rows[name])
            tokens.add(rows[name]["url"].split("?t=")[1])
        self.assertEqual(len(tokens), 1, tokens)  # one token for every desk
        self.assertEqual(rows["lesson"]["url"], rows["learn"]["url"] + "#lesson")
        for s, port in self.ports.items():
            out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"], stdout=subprocess.PIPE,
                                 universal_newlines=True).stdout
            self.assertIn("127.0.0.1:%d" % port, out, s)
            self.assertNotIn("*:%d" % port, out, s)
        # a second serve starts nothing new: the same tokens
        again = {r["name"]: r for r in json.loads(self.run_fleet("app", "serve", "--json").stdout)}
        self.assertEqual(again["learn"]["url"], rows["learn"]["url"])

        # the tunnel: one ssh -N command, the same port on both ends, 127.0.0.1 on both ends
        t = self.run_fleet("app", "tunnel", "me@mac").stdout
        cmd = t.splitlines()[0]
        self.assertTrue(cmd.startswith("ssh -N ") and cmd.endswith(" me@mac"), cmd)
        fwd = re.findall(r"-L (\S+)", cmd)
        self.assertEqual(len(fwd), 1)
        for s, port in self.ports.items():
            self.assertIn("127.0.0.1:%d:127.0.0.1:%d" % (port, port), fwd)
        self.assertIn(rows["lesson"]["url"], t)
        # why the same port: the page checks Host. Through the tunnel the phone sends Host 127.0.0.1:<port>
        tok = rows["learn"]["url"].split("?t=")[1]
        lp = self.ports["ui"]
        self.assertEqual(self.get(lp, "/learn/?t=" + tok, "127.0.0.1:%d" % lp), 200)
        self.assertEqual(self.get(lp, "/learn/?t=" + tok, "127.0.0.1:%d" % (lp + 1)), 403)  # another local port
        self.assertEqual(self.get(lp, "/learn/?t=" + tok, "mac.example:%d" % lp), 403)  # not a loopback name
        self.assertEqual(self.get(lp, "/learn/", "127.0.0.1:%d" % lp), 403)  # no token

    def test_stale_url_file_is_not_alive(self):
        dead = free_port()
        with open(os.path.join(self.state, "ui.url"), "w") as f:
            f.write("http://127.0.0.1:%d/?t=old\n" % dead)
        rows = {r["name"]: r for r in json.loads(self.run_fleet("app", "urls", "--json").stdout)}
        self.assertFalse(rows["learn"]["alive"])
        self.assertFalse(rows["lesson"]["alive"])
        # a non-loopback URL in the file is never offered
        with open(os.path.join(self.state, "ui.url"), "w") as f:
            f.write("http://10.0.0.5:7343/?t=x\n")
        rows = {r["name"]: r for r in json.loads(self.run_fleet("app", "urls", "--json").stdout)}
        self.assertIsNone(rows["learn"]["url"])


class TestAppParity(unittest.TestCase):
    """Q698 for Fleet Desk: the app is a superset of the pane. Every app action runs a `fleet app` verb, every desk
    in its menu has a pane command, and the app ships no copy of a desk page."""

    def setUp(self):
        self.rs = open(MAIN_RS).read()

    def test_every_tauri_command_maps_to_a_verb(self):
        handler = re.search(r"generate_handler!\[([^\]]*)\]", self.rs).group(1)
        cmds = sorted(x.strip() for x in handler.split(",") if x.strip())
        self.assertEqual(cmds, sorted(fa.COMMANDS))
        for verbs in fa.COMMANDS.values():
            for v in verbs.split("|"):
                self.assertIn(v, fa.VERBS)

    def test_rust_runs_only_known_verbs(self):
        lits = re.findall(r'fleet_app\(&\[\s*(?:if [^{]+\{\s*"(\w+)"\s*\}\s*else\s*\{\s*"(\w+)"\s*\}|"(\w+)")', self.rs)
        used = {v for t in lits for v in t if v}
        self.assertTrue(used, "no fleet_app(...) calls found in main.rs")
        self.assertLessEqual(used, set(fa.VERBS))
        self.assertIn("notify", used)

    def test_menu_is_the_desk_list(self):
        self.assertIn("desk_list(false)", self.rs.split("fn menu(", 1)[1].split("\nfn ", 1)[0])
        keys = [d["key"] for d in fa.DESKS]
        self.assertEqual(len(keys), len(set(keys)))
        for d in fa.DESKS:
            self.assertIn(d["server"], fa.SERVERS)
            self.assertTrue(d["pane"].startswith("fleet "), d)

    def test_every_desk_has_a_pane_command(self):
        env = scrubbed_env({"FLEET_STATE": tempfile.mkdtemp(prefix="apptest-help-")})
        helps = {
            "decisions": subprocess.run([FLEET, "decisions", "-h"], env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, universal_newlines=True).stdout,
            "learn": subprocess.run([FLEET, "learn", "help"], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, universal_newlines=True).stdout,
            "watch": open(os.path.join(ROOT, "bin", "fleet-watch")).read(),
        }
        top = open(FLEET).read()  # a top-level verb (`fleet ls`) is in bin/fleet's own usage header
        shutil.rmtree(env["FLEET_STATE"], ignore_errors=True)
        for d in fa.DESKS:
            for cmd in d["pane"].split(";"):
                words = cmd.split()
                self.assertEqual(words[0], "fleet", d)
                if len(words) == 2:
                    self.assertIn("#   fleet %s " % words[1], top, (d["name"], cmd))
                    continue
                self.assertIn(words[2], helps[words[1]], (d["name"], cmd))

    def test_no_fork_of_a_desk_page(self):
        conf = json.load(open(CONF))
        self.assertEqual(conf["build"]["frontendDist"], "../shell")
        app_files = []
        for dp, _, fs in os.walk(os.path.join(ROOT, "app")):
            app_files += [os.path.join(dp, f) for f in fs]
        pages = [open(os.path.join(LIB, p)).read() for p in os.listdir(LIB) if p.endswith("-ui.html")]
        for f in app_files:
            if f.endswith((".html", ".js")):
                body = open(f).read()
                for page in pages:
                    # no 3 consecutive lines of a desk page in the app
                    lines = [l for l in page.splitlines() if len(l.strip()) > 30]
                    for i in range(len(lines) - 2):
                        self.assertNotIn("\n".join(lines[i:i + 3]), body, f)

    def test_remote_pages_get_no_ipc(self):
        cap = json.load(open(os.path.join(ROOT, "app", "desk-ui", "src-tauri", "capabilities", "default.json")))
        self.assertTrue(cap.get("local"))
        self.assertNotIn("remote", cap)
        self.assertEqual(cap["windows"], ["main"])


if __name__ == "__main__":
    unittest.main()
