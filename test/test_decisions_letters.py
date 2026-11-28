"""Issue #59: option letters are fixed when a call opens; the recommendation is a flag, never the order.

The letter the asker wrote is the letter the human sees (card, TUI, list) and the letter the relay names, always with
its text. Every test runs on a temp FLEET_STATE with stub binaries (the test_decisions.Base harness), so nothing
reaches a live fleet. Run: python3 -m unittest test/test_decisions_letters.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_decisions import DESK, GOAL, ROOT, Base, _load, mesh  # noqa: E402

learn = _load("fleet_learn", os.path.join(ROOT, "lib", "fleet_learn.py"))

QR = "Cap the upload retries at three, or retry forever?"
REC_B = ["(a) retry forever (downside: a dead vendor pins a worker)",
         "(b, rec) cap at three with jitter (downside: some uploads fail sooner)",
         "(c) cap at ten"]


class Letters(Base):
    def fixture(self):
        """One call whose recommended option is (b), not (a)."""
        mesh.call_tool("decision_open", {"from": "lane-b", "question": QR, "options": REC_B})
        self.run_ok([GOAL, "new", self.goal, "--goal", "Run the decisions desk"])
        self.reset_stub_calls()

    def card(self, cid):
        return next(c for c in self.state_json()["open"] if c["id"] == cid)


class TestStoredAtOpen(Letters):
    def test_row_keeps_the_asker_order_and_flags_rec(self):
        row = self.rows()["Q1"]
        self.assertEqual([(o["letter"], o["text"], o["rec"]) for o in row["options"]], [
            ("a", "retry forever", False), ("b", "cap at three with jitter", True), ("c", "cap at ten", False)])
        self.assertEqual(row["options"][1]["downside"], "some uploads fail sooner")

    def test_plain_strings_without_letters_are_lettered_in_order(self):
        mesh.call_tool("decision_open", {"from": "lane-a", "question": "Which region?",
                                         "options": ["west", "east (rec)", "north"]})
        self.assertEqual([(o["letter"], o["text"], o["rec"]) for o in self.rows()["Q2"]["options"]],
                         [("a", "west", False), ("b", "east", True), ("c", "north", False)])

    def test_the_askers_own_letters_are_kept(self):
        mesh.call_tool("decision_open", {"from": "lane-a", "question": "Which port?",
                                         "options": [{"letter": "x", "text": "8080"},
                                                     {"letter": "y", "text": "9090", "rec": True}]})
        self.assertEqual([(o["letter"], o["rec"]) for o in self.rows()["Q2"]["options"]],
                         [("x", False), ("y", True)])

    def test_repeated_or_partial_letters_fall_back_to_order(self):
        mesh.call_tool("decision_open", {"from": "lane-a", "question": "Which tag?",
                                         "options": ["(a) one", "(a) two", "three"]})
        self.assertEqual([o["letter"] for o in self.rows()["Q2"]["options"]], ["a", "b", "c"])

    def test_bad_options_are_refused(self):
        res = json.dumps(mesh.call_tool("decision_open", {"from": "lane-a", "question": "Bad?", "options": [3, 4]}))
        self.assertIn("must be a list of strings or {letter, text, rec} objects", res)
        self.assertNotIn("Q2", self.rows())

    def test_the_desk_message_and_list_name_the_same_letters(self):
        desk = [m["text"] for m in self.inbox("DECISIONS") if m["text"].startswith("[decision Q1")]
        self.assertEqual(len(desk), 1)
        self.assertIn("b: cap at three with jitter (rec)", desk[0])
        self.assertIn("a: retry forever |", desk[0])
        listed = mesh.call_tool("decision_list", {})
        listed = json.dumps(listed)
        self.assertIn("b: cap at three with jitter (rec)", listed)


class TestCardsKeepLetters(Letters):
    def test_card_rec_is_b_and_a_stays_a(self):
        c = self.card("Q1")
        self.assertEqual([(o["letter"], o["text"], o["rec"]) for o in c["options"]], [
            ("a", "retry forever", False), ("b", "cap at three with jitter", True), ("c", "cap at ten", False)])

    def test_old_plain_string_rows_read_the_same(self):
        row = {"options": ["quarantine (downside: stops guarding)", "retry once (rec)"]}
        self.assertEqual([(o["letter"], o["text"], o["rec"]) for o in mesh.option_list(row)],
                         [("a", "quarantine", False), ("b", "retry once", True)])

    def test_goal_only_inline_markers_keep_their_letters(self):
        self.run_ok([DESK, "add", "Q9", "lane-c", "Keep the cache? (a) drop it (b, rec) keep it", "lane-c"])
        c = self.card("Q9")
        self.assertEqual([(o["letter"], o["text"], o["rec"]) for o in c["options"]],
                         [("a", "drop it", False), ("b", "keep it", True)])

    def test_tui_card_shows_rec_on_b(self):
        p = self.fleet("tui", "--plain", stdin="q\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("[a] retry forever", p.stdout)
        self.assertIn("[b, rec] cap at three with jitter", p.stdout)


class TestRelayCarriesLetterAndText(Letters):
    def test_picking_the_rec_relays_b_with_its_text(self):
        p = self.fleet("pick", "Q1", "b")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.answers_in("lane-b"), ["[answer Q1] b: cap at three with jitter"])
        self.assertEqual(self.rows()["Q1"]["answer"], "b: cap at three with jitter")

    def test_picking_a_relays_the_askers_a(self):
        p = self.fleet("pick", "Q1", "a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.answers_in("lane-b"), ["[answer Q1] a: retry forever"])

    def test_answer_with_text_expands_only_a_known_bare_letter(self):
        row = self.rows()["Q1"]
        self.assertEqual(mesh.answer_with_text(row, "b"), "b: cap at three with jitter")
        self.assertEqual(mesh.answer_with_text(row, " C "), "c: cap at ten")
        self.assertEqual(mesh.answer_with_text(row, "z"), "z")
        self.assertEqual(mesh.answer_with_text(row, "b, but only for the nightly job"),
                         "b, but only for the nightly job")
        self.assertEqual(mesh.answer_with_text(None, "b"), "b")

    def test_learn_reads_a_bare_logged_letter_with_its_option_text(self):
        self.assertEqual(learn._with_option_text("Q1", "b", [None]), "b: cap at three with jitter")
        self.assertEqual(learn._with_option_text("Q404", "b", [None]), "b")
        long = "cap at three, and log every retry"
        self.assertEqual(learn._with_option_text("Q1", long, [None]), long)


class TestRolesDropTheRewrite(unittest.TestCase):
    def test_no_role_or_skill_asks_for_rec_first(self):
        for rel in ("roles/decide.md", "roles/decide.codex.toml"):
            with open(os.path.join(ROOT, rel)) as f:
                text = f.read()
            self.assertNotIn("recommended option first", text, rel)
            self.assertIn("never a bare letter", text, rel)
        # the SKILL.md intake line is #58's (desk.sh take, verbatim); #59 adds the Rules bullet
        with open(os.path.join(ROOT, "skills", "running-the-decisions-desk", "SKILL.md")) as f:
            self.assertIn("`(rec)` is a flag, never a position", f.read())


if __name__ == "__main__":
    unittest.main()
