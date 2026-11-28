"""Tests for claims on a call (#61): decision_open's `claims`, the unsourced marker and the options lint.

Pure-function tests load lib/fleet_mesh.py directly; nothing touches a live fleet.
Run: python3 -m unittest test/test_decisions_claims.py
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "lib", "fleet_mesh.py")

spec = importlib.util.spec_from_file_location("fleet_mesh_claims", MESH)
mesh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mesh)


def opts(*texts):
    return [{"letter": chr(ord("a") + i), "text": t} for i, t in enumerate(texts)]


class TestCleanClaims(unittest.TestCase):
    def test_absent_or_empty_is_none(self):
        self.assertIsNone(mesh._clean_claims(None, ["a"]))
        self.assertIsNone(mesh._clean_claims([], ["a"]))

    def test_shape_ids_and_unsourced(self):
        got = mesh._clean_claims([
            {"text": "  the tag   exists ", "source": "git tag -l 'v1*'", "options": ["A", "(b)"], "refs": ["#12", "#12", " "]},
            "the job runs nightly",
            {"id": "mine", "text": "docs say so", "source": "", "options": "a"},
        ], ["a", "b"])
        self.assertEqual(got, [
            {"id": "c1", "text": "the tag exists", "source": "git tag -l 'v1*'", "options": ["a", "b"], "refs": ["#12"]},
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
        got = mesh._clean_claims([{"text": "t", "refs": ["#7", "some words", "https://example.com/x/1"]}], [])
        self.assertEqual(got[0]["refs"], ["#7", "https://example.com/x/1"])


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


if __name__ == "__main__":
    unittest.main()
