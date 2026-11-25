# Proposal

## Why

LEARN goes straight to retrieval practice: `make_cards` sets a card's `sched.due` to its creation time, so the
human meets new work for the first time inside a quiz, and `firstSeen` in proof is the first review, not the first
look. The live ledger also has almost no `what` or `why` text, so a lesson built today is mostly a title and a
quote. LEARN should teach before it tests: a short, grounded lesson first, then the quiz on what was just taught,
with the first exposure never counted as learned.

## What Changes

- New `fleet learn lesson [--topic T]` (Q694 answered: the verb is `lesson`; the ledger kind `lesson` is renamed
  `done`, with no compatibility for old rows): prints the lessons that are due (rows the human has not seen yet) and records
  them as seen. A lesson follows the human's model: term definitions first, then each row as an example of the
  term in context. Every lesson line carries a verbatim quote and its source; heard rows show as "not decided yet".
- New seen record (`$FLEET_STATE/learn/seen.jsonl`, append-only). Only the human writes it (any `FLEET_ROLE` is
  refused; `--preview` shows a lesson without recording it).
- **BREAKING** (scheduling): a card is due only after its row's lesson was seen. The 24 h proof clock starts at the
  seen time. No migration: Q694 answered that old LEARN data is thrown away and re-derived from its sources.
- New `fleet learn rebuild`: everything LEARN holds is re-derivable. It regenerates the ledger, entities, cards,
  schedules and proof from the sources (decisions log, WATCH events, achieved goal files, and the inputs journal of
  ROUTE, DECIDE and LEARN messages), keeping only the human's own results (reviews and seen), which re-attach to the
  rebuilt rows by source key.
- **BREAKING**: ledger kind `lesson` (a goal done line) is renamed `done`.
- New `fleet learn session --topic T`: the lesson for T, then a quiz on only T's cards, then one result line.
- New `fleet learn curate <lid> --what W --why Y [--part-of E]`: LEARN fills a row's definition and reason. Lesson
  lines from `what`/`why` must be backed by the row's quote or they are dropped.
- New entity layer: `fleet learn entity add|list|tree` with typed edges (part-of, ships-in, blocked-by, decided-by,
  owner) in `entities.jsonl`. A topic rolls up to its entity subtree; the pane shows a text tree.
- New `fleet learn brief --for X`: a read-only brief through a pluggable selector command (`learn.json`
  `brief.selector`), with a no-plugin fallback (rows on topic X, else the most recent rows).
- New `fleet learn digest [--since WHEN]`: teaches the rows newer than a last-looked cursor (or `--since`).
- `fleet learn quiz` and `fleet learn review` get the human-only guard the page already has.
- `fleet learn serve` gets a Lesson tab: topic list, a deterministic SVG tree, term and context cards, and a "mark
  seen and quiz this topic" button that calls the same seen verb as the pane.
- `roles/learn.md`: LEARN teaches before it tests (lesson, then quiz), curates what/why, and never takes or grades lessons itself.

## Capabilities

### New Capabilities
- `learn-lessons`: lesson content and its grounding rules, the seen record, `lesson`, and `curate`.
- `learn-teach-first-scheduling`: cards come due only after their lesson was seen; the proof clock starts at seen.
- `learn-rebuild`: regenerate all derived LEARN state from sources, keeping the human's results.
- `learn-session`: one topic's lesson, its quiz, and the result line.
- `learn-entities`: entities with typed edges, topic rollup and the pane tree.
- `learn-brief`: the `--for` brief, its selector contract and its fallback.
- `learn-digest`: lessons since a cursor or a given time.
- `learn-lesson-page`: the Lesson tab in `fleet learn serve`.
- `learn-desk-role`: the LEARN role text for teach-first.

### Modified Capabilities
- none (the repo has no OpenSpec specs before this change)

## Impact

- Code: `lib/fleet_learn.py` (new verbs, seen gate in `due()`, `rebuild_proof()` reads seen), new
  `lib/fleet_learn_lesson.py` (lesson builder, entities, brief, digest, rebuild), `lib/fleet_learn_serve.py` and
  `lib/learn-ui.html` (Lesson tab, `/api/lessons`, `/api/seen`), `roles/learn.md`, `lib/fleet_learn_contract.md`,
  `README.md`, `DESIGN-NOTES.md`.
- State: new `learn/seen.jsonl` and `learn/inputs.jsonl` (append-only), derived `learn/entities.jsonl`;
  `cursors.json` gets a `digest` key; `learn.json` gets an optional `brief` section. No migration (Q694): the live
  state is wiped and rebuilt only when the chosen branch lands, never from this worktree.
- Tests: new `test/test_learn_lesson.sh`, new cases in `test/test_learn_serve.py`; existing tests that expect a card
  due right after `add` get a lesson step in their setup, and `--kind lesson` becomes `--kind done`.
- No new dependencies (Python stdlib only).
