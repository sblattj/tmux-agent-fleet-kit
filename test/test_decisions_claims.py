"""Tests for claims on a call (#61): decision_open's `claims`, the unsourced marker and the options lint.

Pure-function tests load lib/fleet_mesh.py directly; nothing touches a live fleet.
Run: python3 -m unittest test/test_decisions_claims.py
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS = os.path.join(ROOT, "lib", "fleet_decisions.py")
UI = os.path.join(ROOT, "lib", "decisions-ui.html")

spec = importlib.util.spec_from_file_location("fleet_decisions_claims", DECISIONS)
dec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dec)
mesh = dec.mesh  # the fleet_mesh the decisions module itself uses
FAKE = "#!/bin/sh\nexit 0\n"


def opts(*texts):
    return [{"letter": chr(ord("a") + i), "text": t} for i, t in enumerate(texts)]


class TestCleanClaims(unittest.TestCase):
    def test_absent_or_empty_is_none(self):
        self.assertIsNone(mesh._clean_claims(None, ["a"]))
        self.assertIsNone(mesh._clean_claims([], ["a"]))

    def test_shape_ids_and_unsourced(self):
        got = mesh._clean_claims([
            {"text": "  the tag   exists ", "source": "git tag -l 'v1*'", "options": ["A", "(b)"], "refs": ["acme/widget#12", "acme/widget#12", " "]},
            "the job runs nightly",
            {"id": "mine", "text": "docs say so", "source": "", "options": "a"},
        ], ["a", "b"])
        self.assertEqual(got, [
            {"id": "c1", "text": "the tag exists", "source": "git tag -l 'v1*'", "options": ["a", "b"], "refs": ["acme/widget#12"]},
            {"id": "c2", "text": "the job runs nightly", "source": None, "options": [], "refs": []},
            {"id": "c3", "text": "docs say so", "source": None, "options": ["a"], "refs": []},
        ])
        self.assertEqual(mesh.unsourced_claims(got), ["c2", "c3"])

    def test_refusals(self):
        bad = [
            ("not a list", "claims"),
            ([42], "must be an object"),
            ([{"source": "x"}], "needs a text"),
            ([{"text": "t", "options": ["c"]}], "cites option"),
            ([{"text": "t", "options": {"a": 1}}], "options must be a list"),
            ([{"text": "t", "refs": {"x": 1}}], "refs must be a list"),
            ([{"text": "x" * (mesh.CLAIM_TEXT_MAX + 1)}], "longer than"),
            ([{"text": "t", "source": 5}], "must be a string"),
            ([{"text": "t"}] * (mesh.CLAIMS_MAX + 1), "at most"),
        ]
        for claims, msg in bad:
            with self.subTest(claims=str(claims)[:40]):
                with self.assertRaises(ValueError) as cm:
                    mesh._clean_claims(claims, ["a", "b"])
                self.assertIn(msg, str(cm.exception))

    def test_refs_with_spaces_are_not_machine_ids(self):
        got = mesh._clean_claims([{"text": "t", "refs": ["PROJ-7", "some words", "https://example.com/x/1"]}], [])
        self.assertEqual(got[0]["refs"], ["PROJ-7", "https://example.com/x/1"])

    def test_letters_come_from_the_rows_stored_options(self):
        rec = {"options": [{"letter": "a", "text": "x"}, {"letter": "c", "text": "y"}]}
        self.assertEqual(mesh._clean_claims([{"text": "t", "options": ["c"]}], rec)[0]["options"], ["c"])
        with self.assertRaises(ValueError):
            mesh._clean_claims([{"text": "t", "options": ["b"]}], rec)


class TestLint(unittest.TestCase):
    FLAGGED = [
        ("there is no such tag", "asserts absence"),
        ("the module doesn't exist upstream", "asserts absence"),
        ("no tests cover this path", "asserts absence"),
        ("it's one ask", "says one step"),
        ("one step: rerun the job", "says one step"),
        ("only the docs change", "says only"),
        ("the fix already landed", "says already"),
        ("54 hotfixes get picked", "names a count"),
        ("two moments, not one", "names a count"),
        ("the 3 failing tests", "names a count"),
    ]
    CLEAN = ["yes", "no", "no, leave it", "no change", "I know the risk", "another round", "nobody minds",
             "merge #123", "bump to v0.11.2", "retry once", "quarantine it", "pick one of them",
             "cut it 2026-11-28", "use port 7340", "wait for review"]

    def test_flags_assertions(self):
        for text, why in self.FLAGGED:
            with self.subTest(text=text):
                self.assertEqual(mesh.claims_lint(opts(text), None), [{"letter": "a", "text": text, "why": why}])

    def test_leaves_preference_options_alone(self):
        for text in self.CLEAN:
            with self.subTest(text=text):
                self.assertEqual(mesh.claims_lint(opts(text), None), [])

    def test_a_claim_on_the_letter_or_the_whole_call_satisfies_it(self):
        o = opts("ship it, it's one step", "only the docs", "wait")
        self.assertEqual([x["letter"] for x in mesh.claims_lint(o, None)], ["a", "b"])
        on_a = mesh._clean_claims([{"text": "the deploy is one job", "options": ["a"]}], ["a", "b", "c"])
        self.assertEqual([x["letter"] for x in mesh.claims_lint(o, on_a)], ["b"])
        whole = mesh._clean_claims(["the docs are the only change"], ["a", "b", "c"])
        self.assertEqual(mesh.claims_lint(o, whole), [], "a claim citing no letter backs every option")

    def test_an_unsourced_claim_still_counts_for_the_lint_but_is_marked(self):
        o = opts("there is no such tag")
        c = mesh._clean_claims([{"text": "no tag matches", "options": ["a"]}], ["a"])
        self.assertEqual(mesh.claims_lint(o, c), [])
        self.assertEqual(mesh.claims_note(c, []), "unsourced claims: c1")

    def test_note(self):
        self.assertEqual(mesh.claims_note(None, []), "")
        lint = mesh.claims_lint(opts("only this"), None)
        self.assertEqual(mesh.claims_note(None, lint),
                         "lint: option a says only with no claim; add claims [{text, source, options: [a]}]")


class TestOpenAndCard(unittest.TestCase):
    """decision_open stores claims, refuses bad ones, reports the lint; the card, TUI and page show them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claimtest-")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        for b in ("tmux", "codex"):
            with open(os.path.join(self.bin, b), "w") as f:
                f.write(FAKE)
            os.chmod(os.path.join(self.bin, b), 0o755)
        self.env = {"FLEET_STATE": self.state, "FLEET_DESK_GOAL": os.path.join(self.state, "desk.goal.json"),
                    "FLEET_TMUX_SOCKET": "claimtest-none", "FLEET_CODEX_BIN": os.path.join(self.bin, "codex"),
                    "FLEET_DECIDE": "DECISIONS", "FLEET_ROUTE": "ROUTE", "FLEET_WAKE_SPAWN": "0",
                    "FLEET_WAKE_QUIET": "0", "CLAUDECODE": "",
                    "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self._saved = {k: os.environ.get(k) for k in list(self.env) + ["FLEET_NAME", "FLEET_ROLE", "FLEET_SIDE"]}
        os.environ.update(self.env)
        for k in ("FLEET_NAME", "FLEET_ROLE", "FLEET_SIDE"):
            os.environ.pop(k, None)
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"lane-a": {"name": "lane-a", "cli": "claude", "mode": "goal", "pane": "%3"},
                       "DECISIONS": {"name": "DECISIONS", "cli": "claude", "mode": "desk", "pane": "%9"}}, f)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        with open(os.path.join(self.state, "decisions.jsonl")) as f:
            return [json.loads(x) for x in f if x.strip()]

    def desk_inbox(self):
        with open(os.path.join(self.state, "inbox", "DECISIONS.jsonl")) as f:
            return [json.loads(x)["text"] for x in f if x.strip()]

    def open_call(self, **kw):
        args = {"from": "lane-a", "question": "Cut the docs release tonight?",
                "options": ["yes, the docs are the only change", "wait for the next train"]}
        args.update(kw)
        r = mesh.call_tool("decision_open", args)
        return r, r["content"][0]["text"]

    def test_claims_are_stored_and_delivered_after_the_options(self):
        r, out = self.open_call(claims=[
            {"text": "the diff touches docs/ only", "source": "git diff --stat main...HEAD", "options": ["a"],
             "refs": ["acme/widget#12"]},
            {"text": "the next train leaves Friday"}])
        self.assertFalse(r["isError"], out)
        row = self.rows()[-1]
        self.assertEqual(row["claims"], [
            {"id": "c1", "text": "the diff touches docs/ only", "source": "git diff --stat main...HEAD",
             "options": ["a"], "refs": ["acme/widget#12"]},
            {"id": "c2", "text": "the next train leaves Friday", "source": None, "options": [], "refs": []}])
        text = self.desk_inbox()[-1]
        self.assertIn(" options: a: yes, the docs are the only change | b: wait for the next train claims: "
                      "c1 (a) the diff touches docs/ only [source: git diff --stat main...HEAD] | "
                      "c2 the next train leaves Friday [unsourced]", text)
        self.assertIn("unsourced claims: c2", out)
        self.assertNotIn("lint:", out, "a claim citing no letter backs the whole call")

    def test_lint_is_reported_but_never_refuses(self):
        r, out = self.open_call()
        self.assertFalse(r["isError"], out)
        self.assertIn("lint: option a says only with no claim", out)
        self.assertIn("lint: option a says only", self.desk_inbox()[-1])
        self.assertNotIn("claims", self.rows()[-1], "no claims, no field")

    def test_a_preference_call_stays_quiet(self):
        r, out = self.open_call(question="Blue or green for the badge?", options=["blue", "green"])
        self.assertFalse(r["isError"], out)
        self.assertNotIn("lint", out)
        self.assertNotIn("unsourced", out)
        self.assertNotIn("claims:", self.desk_inbox()[-1])

    def test_a_claim_on_an_unknown_letter_refuses_the_call(self):
        r, out = self.open_call(claims=[{"text": "t", "source": "s", "options": ["d"]}])
        self.assertTrue(r["isError"])
        self.assertIn("cites option", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "decisions.jsonl")) and self.rows(),
                         "a refused call writes no row")

    def test_schema_offers_claims(self):
        tool = [t for t in mesh.TOOLS if t[0] == "decision_open"][0]
        props = tool[2]["properties"]
        self.assertEqual(props["claims"]["items"]["required"], ["text"])
        self.assertEqual(set(props["claims"]["items"]["properties"]), {"text", "source", "options", "refs"})

    def test_card_tui_and_page_show_claims_and_lint(self):
        self.open_call(claims=[{"text": "the diff touches docs/ only", "source": "git diff --stat", "options": ["a"]}])
        self.open_call(question="Close the stale branch?", options=["yes, there is no open PR on it", "keep it"])
        cards = {c["question"]: c for c in dec.open_cards()[0]}
        c1, c2 = cards["Cut the docs release tonight?"], cards["Close the stale branch?"]
        self.assertEqual([(k["id"], k["unsourced"]) for k in c1["claims"]], [("c1", False)])
        self.assertEqual(c1["lint"], [])
        self.assertEqual(c2["claims"], [])
        self.assertEqual([(x["letter"], x["why"]) for x in c2["lint"]], [("a", "asserts absence")])
        tui = dec.Tui(None)
        self.assertIn("    c1 (a) the diff touches docs/ only  [source: git diff --stat]", tui.render_card(c1, False))
        self.assertIn("    ? option a asserts absence, no claim behind it", tui.render_card(c2, False))
        with open(UI) as f:
            page = f.read()
        self.assertIn("for (const n of claimsBlock(c)) node.appendChild(n);", page)
        self.assertIn('"unsourced"', page)


if __name__ == "__main__":
    unittest.main()
