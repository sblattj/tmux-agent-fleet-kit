# LEARN teach-first: design notes

Branch `feature/learn-teach-first-opsx`. The OpenSpec change is `openspec/changes/learn-teach-first/` (archived into
`openspec/specs/` at the end). This file records the design choices, the answered calls, the assumptions and the
existing tests this change touched.

## The lesson model (the design question)

The human's words, verbatim: "in my mind, learning starts with term definitions, then examples of how a term is
used in context. e.g. CSS means Cascading Style Sheets, example of how it's used is to style an html doc with body {
background: red }. a card couple be a tuple basically lesson->quiz, let's think about it."

**Chosen: a lesson is a term, then the term in context, and it pairs with its cards through the row id.**

- **Terms first.** The lesson for a topic opens with the work subjects it touches (a repo, a PR, a ticket, a
  release, or an entity LEARN added), each with a definition. A definition is a verbatim quote: the entity's own
  quote if it has one, else the earliest row on that subject. Like "CSS means Cascading Style Sheets".
- **Then in context.** Each row on the topic follows as an example of the term in use: its verbatim quote and its
  source, then its `what` and `why` when they are verbatim from that quote (Q696). Like "body { background: red }".
- **lesson -> quiz is a tuple per row.** The lesson half is done when the human marks it seen (`seen.jsonl`). Only
  then do that row's cards come due. The quiz half is the existing cards and proof. The tuple's key is the row's
  source key, so it survives a rebuild.
- Heard rows show in a lesson as "not decided yet" and never get cards.

**Rejected:**

- *A lesson as one more card format* (a "read" card in `cards.jsonl` graded in `reviews.jsonl`). Simple, but the
  lesson would sit in the proof record as a pass, so a first exposure could be graded as learned, which the task
  forbids. It also mixes reading time into review latency.
- *Title plus quote only.* No definitions, so the human gets an example with no term to hang it on.
- *Model-written explanations by default.* Q696 answered "a": verbatim only, rewording is opt-in
  (`learn.json` `{"lesson": {"allowRewording": true}}`) and still needs a `| quote:` backing.
- *A flashcard tuple stored as one object* (definition card plus usage card). It duplicates the ledger row and
  needs its own sync; the lid already ties lesson and cards together.

## Answered calls

| Call | Answer (verbatim, relayed by ROUTE) | What this branch does |
|---|---|---|
| Q694 name and old data | "nuke the old the old is useless"; "both  they can be re-derived  that's the whole point, it all comes from whosaid, git, task tracking sftw, etc" | Verb is `fleet learn lesson`. No migration or compat: ledger kind `lesson` is renamed `done` (`--kind lesson` is refused naming `done`). Everything is re-derivable by `fleet learn rebuild`. Tests use temp state; the live `$FLEET_STATE/learn` is never touched from this worktree. |
| Q695 proof clock | "an agent can fake it. thats fine, but it wouldnt be the usual behavior.", then "a" | Seen (the lesson half) starts the 24 h clock. The human marks seen in the pane (`lesson`, `session`, `digest`) or with the page button. `human_only` guards seen, `quiz` and `review()`; it is a norm, not a security boundary, so no extra hardening. |
| Q696 lesson text | "a" | Lesson definitions and examples are verbatim source quotes, written while LEARN builds or rebuilds. A line with no source quote is dropped and counted. No model rewording by default. |
| Q27 kept records (opened by this lane) | pending | Built as rec (a): keep `reviews.jsonl`, `seen.jsonl` and `inputs.jsonl`. |

## Assumptions (research page rec (a), not answered yet)

| Call | Assumed | Built here? |
|---|---|---|
| Q697 entity layer | Typed edges (part-of, ships-in, blocked-by, decided-by, owner) plus `entities.jsonl`; a topic rolls up to its subtree. | Built: `fleet learn entity add/list/tree`, entities derived from row topics plus journaled adds, subtree rollup in topic filters. Deferred: distractors skipping rows under the same root. |
| Q698 desk UI rule | TUI first; a page is a face on the pane's verbs, never a verb the pane lacks. | Built: `/api/lessons` and `/api/seen` call the same functions as `lesson`; the pane prints a text tree for the SVG. The `o` key is not built here (no LEARN TUI yet). |
| Q699 web vs Tauri | Web, loopback page; no Tauri in the kit. | Built: the Lesson tab is in `fleet learn serve`. |
| Q700 hub | One `fleet ui serve` hub later. | Design only: not built in this branch; the Lesson tab lives in today's LEARN page. |
| Q701 graphics | Deterministic SVG from the ledger and entity graph; pane shows a text tree. | Built: the tree SVG is laid out from depth and order alone. Lane-attached SVG figures are design only. |

## Where rebuild reads its sources

`fleet learn rebuild` (and `sync`) read only these, all local files, no adapters or source config (Q696 note):

| Source | Path | Rows it gives |
|---|---|---|
| Decisions log (DECIDE answers, decided-on-call lines from WATCH/whosaid calls) | `$FLEET_STATE/decisions-log.jsonl` (+ `decisions.jsonl` for question text) | decision rows, heard rows |
| WATCH events (git hosts and trackers, already fetched by WATCH's adapters) | `$FLEET_STATE/watch/events.jsonl` | change, closed, doc rows |
| Achieved goal files | `$FLEET_STATE/roster` + `$FLEET_STATE/launch/<lane>.txt` naming each goal file | done and decision rows |
| Inputs journal (messages LEARN was handed) | `$FLEET_STATE/learn/inputs.jsonl` | `add`, `confirm`, `drop`, `curate`, `entity add` |

Kept as the human's record: `learn/reviews.jsonl`, `learn/seen.jsonl`. Derived (rebuild regenerates them):
`ledger.jsonl`, `cards.jsonl`, `entities.jsonl`, `proof.json`, `cursors.json`. Which git host or which tracker is
WATCH's config (`watch.json`), not LEARN's.

## Existing tests touched

Filled in at the end of the apply phase.
