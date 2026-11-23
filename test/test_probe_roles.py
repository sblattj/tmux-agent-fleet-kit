#!/usr/bin/env python3
"""Static deny-list tests for roles/probe.* on every CLI (docs/design/four-desks.md 2.5, layer 1: the tool allowlist).

Each positive check runs the same predicate on a role that must fail it (worker or watch), so a predicate that
passes everything is caught. The live proof (a probe lane really can't write or comment) is test_probe_live.sh.
"""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = os.path.join(ROOT, "roles")
sys.path.insert(0, os.path.join(ROOT, "lib"))
import fleet_probe as P  # noqa: E402

SHELLS = ("Bash", "Edit", "Write", "NotebookEdit", "MultiEdit")
MESH_WRITES = ("mcp__fleet__peer_send", "mcp__fleet__decision_open", "mcp__fleet__decision_answer",
               "mcp__fleet__decision_report")


def j(name):
    with open(os.path.join(R, name)) as f:
        return json.load(f)


def text(name):
    with open(os.path.join(R, name)) as f:
        return f.read()


def claude_blocks_writes(d):
    deny = set(d.get("permissions", {}).get("deny", []))
    return all(x in deny for x in SHELLS + MESH_WRITES + ("AskUserQuestion", "SendMessage"))


def opencode_blocks_writes(d):
    t = d.get("tools", {})
    return (t.get("*") is False and all(t.get(k) is False for k in ("bash", "write", "edit", "patch"))
            and not [k for k, v in t.items() if v is True and re.search(r"bash|write|edit|patch|shell", k)]
            and d.get("permission", {}).get("bash") in ({"*": "deny"}, "deny")
            and d.get("permission", {}).get("edit") == "deny")


def kiro_blocks_writes(d):
    tools = d.get("tools", [])
    rules = d.get("permissions", {}).get("rules", [])
    deny = {(r["capability"], tuple(r["match"])) for r in rules if r.get("effect") == "deny"}
    return ("*" not in tools and not [t for t in tools if t in ("shell", "fs_write", "execute_bash", "use_aws")]
            and ("shell", ("*",)) in deny and ("fs_write", ("**",)) in deny and d.get("includeMcpJson") is False
            and not [t for t in tools if t.startswith("@") and t not in ("@fleet-probe", "@fleet/peer_inbox")])


def agy_blocks_writes(d, md):
    deny = set(d.get("permissions", {}).get("deny", []))
    m = re.search(r"^tools: \[(.*)\]$", md, re.M)
    tools = [x.strip() for x in m.group(1).split(",")] if m else ["?"]
    return ({"command(*)", "write_file(*)"} <= deny and "run_command" not in tools and "write_file" not in tools
            and re.search(r"^inheritMcp: false$", md, re.M) is not None)


def codex_toml(name):
    s = text(name)
    m = re.match(r'developer_instructions = """\n(.*?)\n"""\n', s, re.S)
    tools = re.search(r'\[mcp_servers\.fleet\]\nenabled_tools = \[(.*?)\]', s)
    return (m.group(1) if m else None), (re.findall(r'"([^"]+)"', tools.group(1)) if tools else None), s


class TestProbeRoleFiles(unittest.TestCase):
    def test_every_cli_has_a_probe_file(self):
        for f in ("probe.md", "probe.claude.json", "probe.codex.toml", "probe.kiro.json", "probe.opencode.json",
                  "probe.agy.json", "probe.agy.md"):
            self.assertTrue(os.path.isfile(os.path.join(R, f)), f)

    def test_md_shape(self):
        md = text("probe.md")
        n = md.count("\n")
        self.assertTrue(15 <= n <= 40, n)
        self.assertNotIn("\u2014", md)  # no em dash
        self.assertIsNone(re.search(r"happy|squarely", md, re.I))
        self.assertTrue(md.startswith("# Role: probe\n"))
        for verb in P.MCP_TOOLS:
            self.assertIn(verb[0], md)

    def test_claude_denies_shell_writes_and_mesh_writes(self):
        self.assertTrue(claude_blocks_writes(j("probe.claude.json")))
        self.assertEqual(set(j("probe.claude.json")), {"permissions"})  # no hooks: SendMessage is denied outright
        for r in ("worker", "watch", "decide"):  # controls
            self.assertFalse(claude_blocks_writes(j(r + ".claude.json")), r)

    def test_opencode_allowlist(self):
        d = j("probe.opencode.json")
        self.assertTrue(opencode_blocks_writes(d))
        self.assertEqual(d["prompt"], "{file:./roles/probe.md}")
        self.assertEqual(d["mode"], "primary")
        for r in ("worker", "watch"):
            self.assertFalse(opencode_blocks_writes(j(r + ".opencode.json")), r)

    def test_kiro_allowlist(self):
        d = j("probe.kiro.json")
        self.assertTrue(kiro_blocks_writes(d))
        self.assertEqual(d["name"], "fleet-probe")
        for r in ("worker", "watch"):
            self.assertFalse(kiro_blocks_writes(j(r + ".kiro.json")), r)

    def test_agy_blocks_shell_and_mcp_inheritance(self):
        self.assertTrue(agy_blocks_writes(j("probe.agy.json"), text("probe.agy.md")))
        self.assertIn("REFUSED", text("probe.agy.md"))
        self.assertFalse(agy_blocks_writes(j("watch.agy.json"), text("watch.agy.md")))

    def test_codex_toml_matches_md_and_disables_shell(self):
        body, tools, raw = codex_toml("probe.codex.toml")
        self.assertEqual(body.rstrip("\n"), text("probe.md").rstrip("\n"))
        self.assertEqual(tools, ["peer_inbox"])
        self.assertIn("shell_tool = false", raw)
        self.assertIn("unified_exec = false", raw)
        _, wtools, wraw = codex_toml("worker.codex.toml")  # control
        self.assertNotIn("shell_tool = false", wraw)

    def test_can_host_matches_the_files(self):
        """A CLI the kit lets host a probe must have a blocking role file; refused ones say REFUSED."""
        checks = {"claude": lambda: claude_blocks_writes(j("probe.claude.json")),
                  "opencode": lambda: opencode_blocks_writes(j("probe.opencode.json")),
                  "kiro": lambda: kiro_blocks_writes(j("probe.kiro.json"))}
        for cli, (ok, _) in P.PROBE_HOSTS.items():
            if ok:
                self.assertTrue(checks[cli](), cli)
        self.assertEqual(sorted(c for c, (ok, _) in P.PROBE_HOSTS.items() if not ok), ["agy", "codex"])
        self.assertIn("REFUSED", text("probe.codex.toml"))
        self.assertFalse(P.can_host("gemini")[0])  # control: unknown CLIs fail closed

    def test_no_private_strings(self):
        terms = [t for t in re.split(r"[|,]", os.environ.get("FLEET_PRIVACY_TERMS") or "") if t]  # test_privacy.sh: pipes
        names = [f for f in os.listdir(R) if f.startswith("probe.")]
        for f in names:
            s = text(f)
            self.assertIsNone(re.search("/" + "Users/|/" + "home/[a-z]", s), f)  # no home paths
            for t in terms:
                self.assertNotIn(t.lower(), s.lower(), f)


class TestLaunchGuard(unittest.TestCase):
    """bin/fleet's probe launch guard (LANDER-NOTES patch 1) shells out to `fleet probe can-host <cli>`."""

    def test_can_host_exit_codes(self):
        fp = os.path.join(ROOT, "bin", "fleet-probe")
        env = {k: v for k, v in os.environ.items() if k != "FLEET_PROBE_ALLOW_UNVERIFIED"}
        for cli, want in (("claude", 0), ("kiro", 0), ("opencode", 0), ("codex", 1), ("agy", 1), ("", 1)):
            p = subprocess.run([fp, "can-host", cli], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            self.assertEqual(p.returncode, want, cli)


if __name__ == "__main__":
    unittest.main(verbosity=2)
