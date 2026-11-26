#!/usr/bin/env python3
"""fleet ui serve (lib/fleet_ui.py), the one-port hub (Q700): token required, lane roles refused, each tab answers 200,
a keep-alive connection moves between tabs, POSTs reach the mounted page, the ROUTE board escapes lane text, the
old serve commands are aliases that print the hub URL. Every check runs on a temp FLEET_STATE; never the live fleet.
Run: python3 -m unittest test.test_ui -v"""
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
FLEET = os.path.join(ROOT, "bin", "fleet")
TABS = ("decide", "learn", "watch", "route")


def scrubbed_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FLEET_") and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"
           and not k.startswith("TMUX")}
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uitest-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.state, "learn"))
        self.assertTrue(self.state.startswith(self.tmp))
        self.env = scrubbed_env({"FLEET_STATE": self.state, "FLEET_NAME": "tester",
                                 "FLEET_TMUX_SOCKET": "uitest-none-%d" % os.getpid()})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fleet(self, *args, role=None, timeout=30):
        env = dict(self.env)
        if role:
            env["FLEET_ROLE"] = role
        return subprocess.run([FLEET] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout)


class Hub:
    def __init__(self, t, *argv):
        self.errpath = os.path.join(t.tmp, "hub-%d.err" % time.time_ns())
        self.err = open(self.errpath, "w")
        argv = list(argv) or ["ui", "serve", "--port", "0"]
        self.p = subprocess.Popen([FLEET] + argv, env=t.env, stdout=subprocess.PIPE, stderr=self.err,
                                  universal_newlines=True)
        self.first = self.p.stdout.readline()
        line = self.first if self.first.startswith("FLEET UI:") else self.p.stdout.readline()
        m = re.search(r"FLEET UI: http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)", line)
        if not m:
            self.stop()
            with open(self.errpath) as f:
                raise AssertionError("no URL line: %r %r %s" % (self.first, line, f.read()))
        self.port, self.token = int(m.group(1)), m.group(2)
        self.url = "http://127.0.0.1:%d/?t=%s" % (self.port, self.token)

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)

    def req(self, method, path, body=None, token="header", headers=None, conn=None):
        c = conn or self.conn()
        h = {"Content-Type": "application/json"}
        if token == "header":
            h["X-Fleet-Token"] = self.token
        elif token == "query":
            path += ("&" if "?" in path else "?") + "t=" + self.token
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        if conn is None:
            c.close()
        return r.status, data, dict(r.getheaders())

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=10)
        self.p.stdout.close()
        self.err.close()


class TestHub(Base):
    def setUp(self):
        super().setUp()
        # a roster row and an answer whose text is HTML: the ROUTE board must show it as text
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"lane-<b>x</b>": {"role": "worker", "cli": "claude", "status": "live",
                                         "goal": "/tmp/<script>alert(1)</script>.json"}}, f)
        with open(os.path.join(self.state, "decisions-log.jsonl"), "w") as f:
            f.write(json.dumps({"id": "Q7", "at": "2026-11-26T00:00:00Z", "asker": "lane-a",
                                "verbatim": "<img src=x onerror=alert(1)>"}) + "\n")
        self.h = Hub(self)

    def tearDown(self):
        self.h.stop()
        super().tearDown()

    def test_each_tab_answers_200_with_the_token_and_403_without(self):
        for tab in TABS:
            for path in ("/%s/" % tab, "/%s/api/state" % tab):
                st, body, _ = self.h.req("GET", path, token="query")
                self.assertEqual(st, 200, (path, body[:200]))
                st, _, _ = self.h.req("GET", path, token=None)
                self.assertEqual(st, 403, path)  # control: no token
                st, _, _ = self.h.req("GET", path, token=None, headers={"X-Fleet-Token": "wrong"})
                self.assertEqual(st, 403, path)
        st, _, _ = self.h.req("GET", "/decide/refresh", token="query")
        self.assertEqual(st, 200)
        st, _, hdrs = self.h.req("GET", "/", token="query")
        self.assertEqual(st, 302)
        self.assertTrue(hdrs["Location"].startswith("/decide/?t="), hdrs)
        self.assertEqual(self.h.req("GET", "/nope/", token="query")[0], 404)

    def test_pages_carry_base_and_the_tab_strip(self):
        for tab in ("decide", "learn", "watch"):
            st, body, hdrs = self.h.req("GET", "/%s/" % tab, token="query")
            self.assertEqual(st, 200)
            self.assertIn('window.FLEET_BASE="/%s"' % tab, body)
            self.assertIn('id="fleet-tabs"', body)
            for t in TABS:
                self.assertIn('href="/%s/?t=' % t, body)
            self.assertIn("frame-ancestors 'none'", hdrs.get("Content-Security-Policy", ""))
            self.assertEqual(hdrs.get("Cache-Control"), "no-store")
        # the page source itself (served standalone) uses BASE for every request it makes
        for f in ("decisions-ui.html", "learn-ui.html", "watch-ui.html"):
            src = open(os.path.join(LIB, f)).read()
            self.assertIn("fetch(BASE + path", src, f)
            self.assertNotRegex(src, r'fetch\("/', f)
            self.assertNotRegex(src, r'EventSource\("/', f)

    def test_keep_alive_connection_moves_between_tabs(self):
        c = self.h.conn()
        try:
            seen = []
            for tab in ("decide", "learn", "route", "watch", "decide"):
                st, body, _ = self.h.req("GET", "/%s/api/state" % tab, conn=c)
                self.assertEqual(st, 200, (tab, body[:200]))
                seen.append(json.loads(body))
            self.assertIn("open", seen[0])  # DECISIONS snapshot
            self.assertIn("snapshot", seen[1])  # LEARN state
            self.assertIn("lanes", seen[2])  # ROUTE board
            self.assertIn("open", seen[4])
            # the hub's own routes still work after a mounted request on the same connection
            st, _, hdrs = self.h.req("GET", "/", conn=c)
            self.assertEqual(st, 302)
        finally:
            c.close()

    def test_post_reaches_the_mounted_page_with_the_header_token_only(self):
        st, body, _ = self.h.req("POST", "/learn/api/cli", body={"argv": ["list"]})
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["rc"], 0)
        st, _, _ = self.h.req("POST", "/learn/api/cli", body={"argv": ["list"]}, token="query")
        self.assertEqual(st, 403)  # a link click (query token) can't post
        st, _, _ = self.h.req("POST", "/learn/api/cli", body={"argv": ["list"]},
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)  # cross-origin
        st, _, _ = self.h.req("POST", "/learn/api/cli", body={"argv": ["list"]},
                              headers={"Host": "evil.example:%d" % self.h.port})
        self.assertEqual(st, 403)  # DNS rebinding: not a loopback Host
        st, body, _ = self.h.req("POST", "/decide/api/pick", body={"id": "Q404", "option": "a"})
        self.assertEqual(st, 404, body)  # the DECISIONS page answered (no such call), not the hub
        self.assertEqual(self.h.req("POST", "/route/", body={})[0], 404)  # the board is read only

    def test_decide_events_stream_through_the_hub(self):
        c = self.h.conn()
        try:
            c.request("GET", "/decide/events?t=" + self.h.token)
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn("text/event-stream", r.getheader("Content-Type"))
            first = r.fp.readline().decode("utf-8")
            self.assertTrue(first.startswith("event: state"), first)
        finally:
            c.close()

    def test_route_board_shows_lane_text_as_text(self):
        st, body, _ = self.h.req("GET", "/route/", token="query")
        self.assertEqual(st, 200)
        self.assertIn("lane-&lt;b&gt;x&lt;/b&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body)
        st, body, _ = self.h.req("GET", "/route/api/state")
        s = json.loads(body)
        self.assertEqual([l["name"] for l in s["lanes"]], ["lane-<b>x</b>"])
        self.assertEqual(s["log"][0]["id"], "Q7")

    def test_url_file_is_private_and_matches(self):
        path = os.path.join(self.state, "ui.url")
        self.assertEqual(open(path).read().strip(), self.h.url)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        p = self.run_fleet("ui", "url", "--tab", "learn")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "http://127.0.0.1:%d/learn/?t=%s" % (self.h.port, self.h.token))

    def test_loopback_only(self):
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(self.h.p.pid), "-iTCP", "-sTCP:LISTEN"],
                             stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("127.0.0.1:%d" % self.h.port, out)
        self.assertNotIn("*:%d" % self.h.port, out)

    def test_old_serve_commands_print_the_hub_url(self):
        for desk, tab, word in (("decisions", "decide", "DECISIONS"), ("learn", "learn", "LEARN"),
                                ("watch", "watch", "WATCH")):
            p = self.run_fleet(desk, "serve")
            self.assertEqual(p.returncode, 0, (desk, p.stderr))
            self.assertEqual(p.stdout.splitlines()[0], "%s UI: http://127.0.0.1:%d/%s/?t=%s" % (
                word, self.h.port, tab, self.h.token))
        # a second hub was not started: still one listener
        self.assertEqual(self.h.p.poll(), None)

    def test_url_file_goes_away_on_exit(self):
        self.h.stop()
        self.assertFalse(os.path.exists(os.path.join(self.state, "ui.url")))
        p = self.run_fleet("ui", "url")
        self.assertEqual(p.returncode, 3)
        self.h = Hub(self)  # for tearDown


class TestGuards(Base):
    def test_lane_roles_are_refused(self):
        # decide and watch could serve their own page alone; the hub's one token opens every tab, so no role serves it
        for role in ("worker", "decide", "watch", "learn", "route", "probe"):
            for argv in (["ui", "serve", "--port", "0"], ["ui", "url"], ["decisions", "serve", "--port", "0"],
                         ["learn", "serve", "--port", "0"], ["watch", "serve", "--port", "0"]):
                p = self.run_fleet(*argv, role=role)
                self.assertEqual(p.returncode, 4, (role, argv, p.stdout, p.stderr))
                self.assertIn("FAIL role %s" % role, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ui.url")))
        # control: the human's shell serves it
        h = Hub(self)
        h.stop()

    def test_non_loopback_bind_refused(self):
        p = self.run_fleet("ui", "serve", "--port", "0", "--bind", "0.0.0.0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("loopback only", p.stderr)

    def test_alias_starts_the_hub_when_none_answers(self):
        h = Hub(self, "learn", "serve", "--port", "0")
        try:
            self.assertTrue(h.first.startswith("LEARN UI: http://127.0.0.1:%d/learn/?t=%s" % (h.port, h.token)),
                            h.first)
            self.assertEqual(h.req("GET", "/learn/api/state")[0], 200)
            self.assertEqual(h.req("GET", "/decide/api/state")[0], 200)  # the whole hub, not only LEARN
        finally:
            h.stop()

    def test_port_in_use_is_a_one_line_error(self):
        h = Hub(self)
        try:
            os.remove(os.path.join(self.state, "ui.url"))  # so the alias path does not short-circuit
            p = self.run_fleet("ui", "serve", "--port", str(h.port))
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("can't listen", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
        finally:
            h.stop()


class TestAppPointsAtHub(Base):
    def test_desk_urls_are_hub_tabs(self):
        h = Hub(self)
        try:
            p = self.run_fleet("app", "urls", "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            rows = {r["name"]: r for r in json.loads(p.stdout)}
            for name, tab in (("decisions", "decide"), ("learn", "learn"), ("watch", "watch"), ("route", "route")):
                self.assertTrue(rows[name]["alive"], rows[name])
                self.assertEqual(rows[name]["url"], "http://127.0.0.1:%d/%s/?t=%s" % (h.port, tab, h.token))
            self.assertEqual(rows["lesson"]["url"], rows["learn"]["url"] + "#lesson")
            t = self.run_fleet("app", "tunnel", "me@mac").stdout.splitlines()[0]
            self.assertEqual(t, "ssh -N -L 127.0.0.1:%d:127.0.0.1:%d me@mac" % (h.port, h.port))
        finally:
            h.stop()


if __name__ == "__main__":
    unittest.main()
