"""Issue #62: the asking lane refutes its own claims before opening; refuted calls bounce back to the asker.

decision_open takes verified: [{claim, how, result: held|refuted|unchecked}]. A refuted row makes it refuse the call
before anything is written, so the call goes back to the asker as the tool's error. Claims the pass left unsettled show
as unchecked on the DECISIONS card.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "lib", "fleet_mesh.py")
DECISIONS = os.path.join(ROOT, "lib", "fleet_decisions.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mesh = _load("fleet_mesh", MESH)
FAKE = "#!/bin/sh\nexit 0\n"

Q = "Tag the release now or wait for the second fix?"
CLAIMS = [{"id": "c1", "text": "the first fix is merged", "source": "git log", "options": ["a"], "refs": []},
          {"id": "c2", "text": "no other fix is pending", "source": None, "options": ["a"], "refs": ["acme/widget#12"]},
          {"id": "c3", "text": "nobody else waits on the tag", "source": None, "options": ["a"], "refs": []}]
HELD = {"claim": "c1", "how": "git log --oneline origin/main | grep 'fix A'", "result": "held"}
REFUTED = {"claim": "c2", "how": "gh pr list shows acme/widget#12 open with a fix", "result": "refuted"}
UNCHECKED = {"claim": "c3", "result": "unchecked"}


class RefuteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refute-")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        for b in ("tmux", "codex", "fleet"):
            p = os.path.join(self.bin, b)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.env = {"FLEET_STATE": self.state, "FLEET_DESK_GOAL": os.path.join(self.state, "desk.goal.json"),
                    "FLEET_TMUX_SOCKET": "refute-none", "FLEET_CODEX_BIN": os.path.join(self.bin, "codex"),
                    "FLEET_BIN": os.path.join(self.bin, "fleet"), "FLEET_DECIDE": "DECISIONS",
                    "FLEET_ROUTE": "ORCHESTRATOR", "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self._saved = {k: os.environ.get(k) for k in list(self.env) + ["FLEET_NAME", "FLEET_SIDE", "CLAUDECODE"]}
        os.environ.update(self.env)
        for k in ("FLEET_NAME", "FLEET_SIDE", "CLAUDECODE"):
            os.environ.pop(k, None)
        with open(os.path.join(self.state, "roster.json"), "w") as f:
            json.dump({"lane-a": {"name": "lane-a", "cli": "codex", "mode": "goal", "pane": "%3", "session_id": "t"},
                       "DECISIONS": {"name": "DECISIONS", "cli": "codex", "mode": "desk", "pane": "%9", "session_id": "d"}}, f)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        p = os.path.join(self.state, "decisions.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def inbox(self, name):
        p = os.path.join(self.state, "inbox", name + ".jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def open_call(self, verified, claims=CLAIMS, **kw):
        args = {"from": "lane-a", "question": Q, "options": ["tag now", "wait"]}
        if claims is not None:
            args["claims"] = claims
        if verified is not None:
            args["verified"] = verified
        args.update(kw)
        return mesh.call_tool("decision_open", args)


class TestRefuteBeforeOpen(RefuteBase):
    def test_a_refuted_claim_bounces_the_call_back_to_the_asker(self):
        r = self.open_call([HELD, REFUTED])
        self.assertTrue(r["isError"], r)
        text = r["content"][0]["text"]
        self.assertIn("refused: 1 claim(s) refuted by your own check, so this call goes back to you", text)
        self.assertIn("REFUTED c2 'no other fix is pending' (how: gh pr list shows acme/widget#12 open with a fix)",
                      text)
        self.assertEqual(self.rows(), [], "a refuted call must not reach decisions.jsonl")
        self.assertEqual(self.inbox("DECISIONS"), [], "a refuted call must not reach the desk")

    def test_after_fixing_the_call_it_opens_and_keeps_verified(self):
        self.assertTrue(self.open_call([REFUTED])["isError"])
        fixed = [c for c in CLAIMS if c["id"] != "c2"]
        r = self.open_call([HELD, dict(UNCHECKED, how="no source to ask")], claims=fixed)
        self.assertFalse(r["isError"], r)
        self.assertIn("opened Q1", r["content"][0]["text"], "the refused try must not use up an id")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verified"], [HELD, {"claim": "c3", "result": "unchecked", "how": "no source to ask"}])

    def test_claim_text_is_accepted_and_stored_as_its_id(self):
        r = self.open_call([{"claim": "the first fix is merged", "how": "git log", "result": "held"}])
        self.assertFalse(r["isError"], r)
        self.assertEqual(self.rows()[0]["verified"][0]["claim"], "c1")

    def test_an_unknown_claim_id_is_refused(self):
        r = self.open_call([{"claim": "c9", "how": "x", "result": "held"}])
        self.assertTrue(r["isError"])
        self.assertIn("`verified[0].claim` 'c9' matches no claim on this call (ids: c1, c2, c3)", r["content"][0]["text"])
        self.assertEqual(self.rows(), [])

    def test_without_claims_any_claim_text_is_accepted(self):
        r = self.open_call([{"claim": "the build is green", "how": "ci status", "result": "held"}], claims=None)
        self.assertFalse(r["isError"], r)
        self.assertEqual(self.rows()[0]["verified"][0]["claim"], "the build is green")
        r = self.open_call([{"claim": "the build is red", "how": "ci status", "result": "refuted"}], claims=None)
        self.assertTrue(r["isError"])
        self.assertIn("REFUTED the build is red 'the build is red'", r["content"][0]["text"])

    def test_a_call_without_verified_is_unchanged(self):
        """Preference calls skip the refute pass: no verified key on the row."""
        r = self.open_call(None, claims=None)
        self.assertFalse(r["isError"], r)
        self.assertNotIn("verified", self.rows()[0])
        self.assertTrue(self.inbox("DECISIONS")[0]["text"].startswith("[decision Q1 from lane-a] " + Q))
        self.assertFalse(self.open_call([], claims=None)["isError"])
        self.assertNotIn("verified", self.rows()[1], "an empty list is the same as none")

    def test_bad_verified_shapes_are_refused_before_any_write(self):
        bad = [
            ("not a list", "`verified` must be a list"),
            (["x"], "`verified[0]` must be an object"),
            ([{"how": "cmd", "result": "held"}], "`verified[0].claim` is required"),
            ([{"claim": "c1", "how": "cmd", "result": "maybe"}],
             "`verified[0].result` must be one of held|refuted|unchecked"),
            ([{"claim": "c1", "result": "held"}], "`verified[0].how` is required for a held claim"),
            ([{"claim": "c1", "how": "  ", "result": "refuted"}], "`verified[0].how` is required for a refuted claim"),
        ]
        for v, msg in bad:
            r = self.open_call(v)
            self.assertTrue(r["isError"], (v, r))
            self.assertIn(msg, r["content"][0]["text"], v)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.inbox("DECISIONS"), [])

    def test_refusal_is_a_value_error_subclass(self):
        self.assertTrue(issubclass(mesh.Refuted, ValueError))
        with self.assertRaises(mesh.Refuted):
            mesh._clean_verified([REFUTED], {"claims": CLAIMS})

    def test_schema_advertises_verified(self):
        tool = [t for t in mesh.TOOLS if t[0] == "decision_open"][0]
        props = tool[2]["properties"]["verified"]
        self.assertEqual(props["items"]["properties"]["result"]["enum"], ["held", "refuted", "unchecked"])
        self.assertEqual(props["items"]["required"], ["claim", "result"])


class TestUncheckedClaims(unittest.TestCase):
    def test_claims_with_no_settling_row_are_unchecked(self):
        row = {"claims": CLAIMS, "verified": [HELD, UNCHECKED]}
        self.assertEqual(mesh.unchecked_claims(row), ["no other fix is pending", "nobody else waits on the tag"])

    def test_all_settled_is_empty(self):
        row = {"claims": CLAIMS[:1], "verified": [HELD]}
        self.assertEqual(mesh.unchecked_claims(row), [])

    def test_no_claims_no_verified_is_empty(self):
        self.assertEqual(mesh.unchecked_claims({}), [])

    def test_unchecked_rows_without_claims_still_show(self):
        row = {"verified": [{"claim": "the docs say so", "result": "unchecked"}]}
        self.assertEqual(mesh.unchecked_claims(row), ["the docs say so"])


class TestCardShowsUnchecked(RefuteBase):
    def test_open_card_lists_unchecked_claims(self):
        self.assertFalse(self.open_call([HELD, UNCHECKED])["isError"])
        dec = _load("fleet_decisions", DECISIONS)
        cards, _ = dec.open_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["unchecked"], ["no other fix is pending", "nobody else waits on the tag"])

    def test_card_without_claims_has_no_unchecked(self):
        self.assertFalse(self.open_call(None, claims=None)["isError"])
        dec = _load("fleet_decisions", DECISIONS)
        cards, _ = dec.open_cards()
        self.assertEqual(cards[0]["unchecked"], [])

    def test_ui_renders_unchecked_lines(self):
        with open(os.path.join(ROOT, "lib", "decisions-ui.html")) as f:
            page = f.read()
        self.assertIn("for (const u of c.unchecked || [])", page)
        self.assertIn('"unchecked: " + u', page)


class TestWorkerRoleStep(unittest.TestCase):
    def test_worker_roles_carry_the_refute_step(self):
        for name in ("worker.md", "worker.codex.toml"):
            with open(os.path.join(ROOT, "roles", name)) as f:
                s = f.read()
            self.assertIn("try to refute each claim first", s, name)
            self.assertIn("`verified: [{claim, how, result: held|refuted|unchecked}]`", s, name)
            self.assertIn("refuting-claims-before-opening-a-call", s, name)

    def test_skill_has_frontmatter_and_the_subagent_brief(self):
        with open(os.path.join(ROOT, "skills", "refuting-claims-before-opening-a-call", "SKILL.md")) as f:
            s = f.read()
        self.assertTrue(s.startswith("---\nname: refuting-claims-before-opening-a-call\ndescription: "))
        self.assertIn("Try to refute each claim", s)
        self.assertIn("verified: [{claim: \"c1\", how, result}]", s)


if __name__ == "__main__":
    unittest.main()
