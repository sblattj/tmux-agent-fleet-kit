# LEARN teach-first: design notes

Built on `feature/learn-teach-first-opsx`, with the raw build's source plumbing ported in (Q704 69a: local whosaid
engine, the drain, WATCH reading `sources.json`, WATCH's event steps on LEARN's polls, a review() guard test). The
specs live in `openspec/specs/` only; the change and archive copies were dropped. This file records the design choices, the answered calls, the assumptions and the
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
| Q703 kept records | "b" | Only `reviews.jsonl` and `seen.jsonl` survive a rebuild. No inputs journal: a manual `add`, `confirm`, `drop` or `curate` is a message, not a source, so a rebuild drops it and its seen rows show as orphaned (tested: `orphaned 0 review(s), 1 seen`). ROUTE done lines come back only from the lanes' goal files. |
| Q697 entity layer | "a" | Typed links (part-of, ships-in, blocked-by, decided-by, owner) in `entities.jsonl`, derived from row topics and the adapters' `fields.edges`; `fleet learn entity list|tree`; a topic rolls up to its subtree. A test deletes the derived files, rebuilds and compares `entity list --json`. No `entity add` (it would not survive a rebuild). Deferred: distractors skipping rows under the same root. |
| Q702 sources | "a" | LEARN rebuilds from the real sources through WATCH's adapter registry; one shared config with refs-only creds; new `whosaid` and `linear` adapters. See "Where rebuild reads its sources". |

## Assumptions (research page rec (a), not answered yet)

| Call | Assumed | Built here? |
|---|---|---|
| Q698 desk UI rule | Answered: "the ui is supposed to do at least what a pane can do and more via being able to render UI", then "yes" to: the UI is a superset of the pane, every action exists in both, and the pane keeps a full text version (the phone over mosh). Replaces the assumed "TUI first, pages only mirror". | Built: `fleet_learn_serve.PANE_ACTIONS` maps every `fleet learn` verb (`fleet_learn.cli_verbs()`) to a page route. Own routes for the verbs that read stdin or teach: `/api/lessons?topic=` (lesson for any topic) + `/api/seen`, `/api/session?topic=` (lesson, quiz on that topic only, the same result line), `/api/review` + `/api/reveal` (quiz). Every other verb runs through `POST /api/cli`, the same `fleet_learn.main()` the pane calls, its text shown in the page. Graphics on top: score sparklines, the term map, waiting lessons per topic as bars. `TestPaneParity` lists every pane verb and fails when one has no page route, the page never calls it, or `fleet learn help` lacks its text. |
| Q699 desk app | Answered "b": the desk UI as a Tauri Mac app. | Built: `app/desk-ui` (Tauri v2) is a shell with no copy of any desk page. It runs `fleet app` verbs (`lib/fleet_app.py`) and points its one window at the live 127.0.0.1 page (top-level navigation; the pages forbid framing). Q698 holds for the app: each Tauri command maps to a `fleet app` verb (`COMMANDS`), each Desks menu entry has a pane command (`DESKS`), and `test_app.py` checks main.rs against both. Notifications dedupe in Python by call id and lesson id. The phone gets the same pages over `ssh -N -L` on the same ports, bound to 127.0.0.1, with the Host check unchanged. |
| Q699 web vs Tauri | Web, loopback page; no Tauri in the kit. | Built: the Lesson tab is in `fleet learn serve`. |
| Q700 hub | One `fleet ui serve` hub later. | Design only: not built in this branch; the Lesson tab lives in today's LEARN page. |
| Q701 graphics | Deterministic SVG from the ledger and entity graph; pane shows a text tree. | Built: the tree SVG is laid out from depth and order alone. Lane-attached SVG figures are design only. |

## Where rebuild reads its sources

`fleet learn rebuild` (and `sync`) read these. Rebuild polls every configured source into memory first; a failing
source aborts with exit 1, every file byte-identical and no `rebuild-prev/`.

| Source | Path or config | Rows it gives |
|---|---|---|
| Decisions log (DECIDE answers, decided-on-call lines) | `$FLEET_STATE/decisions-log.jsonl` (+ `decisions.jsonl` for question text) | decision rows, heard rows |
| WATCH events already fetched | `$FLEET_STATE/watch/events.jsonl` | change, closed, doc rows |
| Achieved goal files (ROUTE's done lines live here) | `$FLEET_STATE/roster` + `$FLEET_STATE/launch/<lane>.txt` naming each goal file | done and decision rows |
| Configured sources (Q702) | `$FLEET_STATE/sources.json`, else `watch.json`; example `docs/learn-sources.example.json` | per adapter: git hosts (github, gitlab, ...), trackers (`linear`, jira, ado, ...), `whosaid` call decisions (heard) |

- Credentials are `env:`, `keychain:` or `cmd:` references only; `fleet learn sources` exits 1 on a literal. No
  config, no polling. Secret-shaped event text is dropped before it reaches the ledger.
- `whosaid` runs the local `whosaid` CLI on the configured workspace with `--engine local` always (a workspace
  `whosaid.toml` may name a cloud summarizer), bounded by a timeout; nothing leaves the machine.
- Drain: every sync and rebuild polls each source again from its new cursor until it has nothing more (at most 50
  polls and 300 s per source; whosaid reads 50 meetings per poll and never splits a meeting). Every poll goes through
  WATCH's rate gate (`ratePerMin` token bucket, one per source per drain): short waits are slept, a deferral that
  still fits in the 300 s is waited out and the same cursor polled again, a longer one stops the drain. A rebuild that
  stops early at any bound fails closed (rc 1, names the bound, polls and events read, nothing changed); a sync
  keeps what it read, prints `sources: PARTIAL <name>: <why>` and reads on from the cursor next time.
- LEARN's polled events go through WATCH's `normalize`, `has_secret` and `drop` filters, then `cap`, the same steps
  WATCH runs. WATCH reads `sources.json` when it has no `watch.json`.
- `linear` is GraphQL, read-only, paged by cursor (at most 5 pages of 50 per poll), honours `retry_after`, and reports entity links (project or parent as part-of, blockers, assignee as owner, cycle as
  ships-in).
- History depth: whosaid and Linear read from the start, so a rebuild sees everything they hold. The GitHub events
  feed is short (about 90 days, 300 events), so an old PR comes back only through a goal file, the decisions log or
  a tracker that links it.

Kept as the human's record: `learn/reviews.jsonl`, `learn/seen.jsonl` (Q703 b). Derived (rebuild regenerates them,
the old copies move to `learn/rebuild-prev/`): `ledger.jsonl`, `cards.jsonl`, `entities.jsonl`, `proof.json`,
`cursors.json`. The digest cursor is the latest `via: digest` row in `seen.jsonl`, not a stored key.

## Existing tests touched

| Test | Change | Why |
|---|---|---|
| `test/test_learn.sh` | `--kind lesson` call sites become `--kind done`; a `seen_all` helper (and `mark_seen` in the python steps) before every step that expects a card due; the `-h` verb list gains the new verbs | Q694 rename; teach before test means no card is due before its lesson is seen |
| `test/test_learn_serve.py` | fixture kind `lesson` becomes `done`; `fixture()` shows every lesson; new `TestLessonPage` (3 tests: `/api/lessons` equals the pane and records nothing, `/api/seen` starts the quiz for those rows only, bad or tokenless seen requests write nothing) | same; the Lesson tab |
| `test/test_contracts.py` | `test_registry_names` lists the new `whosaid` and `linear` adapters | Q702 |

New: `test/test_learn_lesson.sh` (lesson, session, digest, brief, guard, seen gate, entities, rebuild) and
`test/test_learn_sources.py` (whosaid and linear adapters on fakes, refs-only config, rebuild from sources). A
mutation that ignores seen in `due()` turns both `test_learn_lesson.sh` and `TestLessonPage` red.
