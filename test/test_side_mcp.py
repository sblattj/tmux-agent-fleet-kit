#!/usr/bin/env python3
"""Side runs over MCP: side_start / side_end / side_list (issue #16). Stdlib only, temp FLEET_STATE.

Drives the real entry point (`lib/fleet_mesh.py serve`) over stdio JSON-RPC, as test/test_mesh.py does.
Stub children are `sleep` processes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "lib", "fleet_mesh.py")


class SideBase(unittest.TestCase):
    def setUp(self, fleet_name="L"):
        self.state = tempfile.mkdtemp(prefix="fleet-side-mcp-")
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"L": {"name": "L", "cli": "claude", "pane": "%1"},
                       "M": {"name": "M", "cli": "codex", "pane": "%2"}}, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        env.update(FLEET_STATE=self.state, FLEET_WAKE_SPAWN="0", FLEET_TMUX_SOCKET="side-mcp-test")
        if fleet_name:
            env["FLEET_NAME"] = fleet_name
        self.child = subprocess.Popen(["sleep", "120"])
        self.p = subprocess.Popen([sys.executable, MESH, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, universal_newlines=True, env=env)
        self.n = 0
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "fleet-mesh")

    def tearDown(self):
        self.p.stdin.close()
        self.p.stdout.read()
        self.p.stderr.read()
        self.p.wait(timeout=10)
        self.p.stdout.close()
        self.p.stderr.close()
        self.child.kill()
        self.child.wait()
        shutil.rmtree(self.state, ignore_errors=True)

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
                           + "\n")
        self.p.stdin.flush()
        r = json.loads(self.p.stdout.readline())
        self.assertEqual(r["id"], self.n)
        return r

    def call(self, name, args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return res["isError"], res["content"][0]["text"]

    def rows(self):
        with open(os.path.join(self.state, "sides.jsonl")) as f:
            return [json.loads(x) for x in f if x.strip()]


class SideMcp(SideBase):
    def test_tools_list_has_side_tools(self):
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        for name in ("side_start", "side_end", "side_list"):
            self.assertIn(name, tools)
            self.assertEqual(tools[name]["inputSchema"]["type"], "object")
            self.assertTrue(tools[name]["description"])
        self.assertEqual(sorted(tools["side_start"]["inputSchema"]["required"]), ["host", "pid"])
        self.assertEqual(tools["side_end"]["inputSchema"]["required"], ["id"])

    def test_start_list_end(self):
        err, text = self.call("side_start", {"host": "codex", "model": "gpt-x", "pid": self.child.pid,
                                             "bound": "30m", "log": "/tmp/side-mcp.log"})
        self.assertFalse(err, text)
        self.assertEqual(text, "S1")
        row = self.rows()[0]
        self.assertEqual((row["parent"], row["source"], row["boundSec"], row["status"]), ("L", "mcp", 1800, "running"))
        err, text = self.call("side_list", {})
        self.assertFalse(err, text)
        self.assertEqual(text.splitlines(), ["S1 running codex gpt-x 0m/30m ? tok"])
        # control: another parent's list is empty
        err, text = self.call("side_list", {"parent": "M"})
        self.assertEqual((err, text), (False, "no side runs"))
        err, text = self.call("side_end", {"id": "S1", "status": "failed", "exit": 3})
        self.assertFalse(err, text)
        self.assertTrue(text.startswith("S1 failed endedAt="), text)
        self.assertEqual((self.rows()[0]["status"], self.rows()[0]["exit"]), ("failed", 3))
        # an ended run drops out of the default list; all=true brings it back
        self.assertEqual(self.call("side_list", {}), (False, "no side runs"))
        err, text = self.call("side_list", {"all": True})
        self.assertTrue(text.startswith("S1 failed codex gpt-x "), text)
        err, text = self.call("side_list", {"parent": "*", "all": True})
        self.assertTrue(text.startswith("S1 failed "), text)

    def test_refusals_are_is_error(self):
        err, text = self.call("side_start", {"host": "codex", "pid": self.child.pid, "parent": "NOPE"})
        self.assertTrue(err)
        self.assertEqual(text, "error: no lane NOPE in the roster")
        self.assertFalse(os.path.exists(os.path.join(self.state, "sides.jsonl")) and self.rows())
        err, text = self.call("side_start", {"host": "codex", "pid": self.child.pid, "bound": "13h"})
        self.assertTrue(err, text)
        err, text = self.call("side_end", {"id": "S9"})
        self.assertEqual((err, text), (True, "error: no side run S9"))
        err, text = self.call("side_end", {"id": "S9", "status": "running"})
        self.assertTrue(err, text)
        # the server is still up after the errors
        self.assertIn("side_list", [t["name"] for t in self.rpc("tools/list")["result"]["tools"]])


class SideMcpNoName(SideBase):
    """No FLEET_NAME and no `parent`: side_start refuses (the _sender rule); side_list lists every lane."""

    def setUp(self):
        super().setUp(fleet_name=None)

    def test_no_parent(self):
        err, text = self.call("side_start", {"host": "codex", "pid": self.child.pid})
        self.assertEqual((err, text), (True, "error: no parent (pass --parent or set FLEET_NAME)"))
        err, text = self.call("side_start", {"host": "codex", "pid": self.child.pid, "parent": "M"})
        self.assertEqual((err, text), (False, "S1"))
        err, text = self.call("side_list", {})
        self.assertTrue(text.startswith("S1 running codex "), text)


if __name__ == "__main__":
    unittest.main()
