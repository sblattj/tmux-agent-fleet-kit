# Design notes: LEARN teaches before it tests

Branch `feature/learn-teach-first-raw`, built from `3e306bd`. Scope: `fleet learn lesson`, `fleet learn session
--topic T`, lesson-first scheduling, `fleet learn brief --for`, `fleet learn digest --since`, a Lesson tab in
`fleet learn serve`, the role text in `roles/learn.md`, and (from the human's answers) `fleet learn rebuild`, LEARN
reading the real sources through WATCH's adapters, two new adapters (Linear, whosaid), typed links and an entities
file.

## The problem

A card was due the moment its row existed (`make_cards` sets `sched.due` to now), so the human met new work for the
first time inside a quiz. And most rows are thin: a title plus a quote, with no `what` and no `why`. So a lesson
cannot lean on model-written prose; it has to be built from what the row already proves.

## The lesson model (a design question, worked out here)

The human's starting point, verbatim: "in my mind, learning starts with term definitions, then examples of how a
term is used in context. e.g. CSS means Cascading Style Sheets, example of how it's used is to style an html doc
with body { background: red }. a card couple be a tuple basically lesson->quiz, let's think about it."

### Choice: a lesson is a derived view of a ledger row, paired with that row's cards

The unit stays the ledger row. Each row has two halves, `(lesson, cards)`, joined by a **seen** record:

```
row L7 ──lesson──> seen.jsonl {lid: L7, at, status} ──unlocks──> cards C19..C21 come due
```

A lesson for a topic reads in the order the human described: terms first, then use in context.

1. **Terms.** The term is the row's work subject (a ticket, PR or release id) or its title. The definition is the
   row's `what` when that line is grounded in the quote, else the title. One line per row.
2. **In context.** The verbatim quote, with where it came from (`source type:ref`). This is the "body { background:
   red }" half: the words the decision or change was actually made in.
3. **Why**, when the row has a grounded `why`.
4. **Not decided yet.** A `heard` row shows its quote under that heading and gets no cards.

Every lesson line ends with the same suffix, `| quote: "..." | source: type:ref`, for example
`PROJ-12  retries cap at 3 | quote: "a: cap at 3, jittered" | source: decisions-log:Q12`.
Nothing in a lesson is new text: the term, title and what or why were written by a person or a desk and are shown
only when backed by the quote, and the quote is the source's own words.

**Why this choice.** It fits the thin ledger (a lesson degrades to title plus quote, which is still a true lesson),
it adds no second store of text that could drift from the quote, and it keeps the proof math intact: seeing a
lesson is not a review, so `reviews.jsonl` stays the record of retrieval.

### Alternatives rejected

- **A stored lesson record with its own text** (a lesson file per topic, written once). Rejected: a second copy of
  the facts that can drift from the quote, with no grounding check, and it would not survive a rebuild from sources.
- **The lesson as a new card format** (a "teach" card in `cards.jsonl` with its own schedule). Rejected: a view
  would land in `reviews.jsonl` and count as an exposure-with-grade, so the 24 h proof rule would have to special
  case it, and a lesson card that is "due again" is just a quiz.
- **A glossary across rows built by hand** (define a term once, then every row that uses it). Rejected: Q697 chose
  typed links plus a derived entities file instead (below), which gives the same "one term, many rows" view without
  a hand-kept store.
- **Model-written definitions.** Rejected by Q696: no model rewording; a line is backed by a quote or dropped.
- **Literal tuple cards** (each card holds its own mini lesson in the prompt). Rejected: it shows the answer inside
  the question, so a pass right after reading proves nothing, which is exactly what the 24 h rule guards against.

## Scheduling: lessons are due before cards

- `$FLEET_STATE/learn/seen.jsonl` is append-only: `{"lid", "at", "status", "via": "lesson|session|digest|page",
  "topic", "key"}`. Written only through `fleet_learn.mark_seen()`, which the pane verbs and the page both call.
- A row is **exposed** when it has a seen record made while it was `confirmed`. There is no carve-out for rows
  reviewed before this change (Q694: no compat; old data is rebuilt).
- `due()` returns a card only when its row is exposed and the card's `sched.due` has passed. `lessons_due()` lists
  live rows not yet seen in their current status, so a heard row seen as "not decided yet" and confirmed later is due
  as a lesson again.
- Opt-out: `learn.json {"teachFirst": false}`; `FLEET_LEARN_TEACH_FIRST=0|1` overrides it. The older suites that test
  the card scheduler itself (`test_learn.sh`, `test_learn_serve.py`, `test_explain.py`) set the env override to 0;
  the new tests cover the default.

## The human's answers, and how each was built

### Q694: the name, and the old data

Answer: "nuke the old the old is useless" and "both they can be re-derived that's the whole point". Built:

- The verb is `fleet learn lesson` (not `teach`). The ledger kind that meant "a lane's done line" is renamed from
  `lesson` to `shipped`, with no migration or compat shim.
- `fleet learn rebuild` re-derives everything from sources. It copies the old files to `learn/archive/<stamp>/`
  first (never deleted), removes the derived files, re-syncs, makes cards, re-keys the human's reviews and seen rows
  onto the new lids by `source_key` (`<type>|<ref>|<normalized quote>`), replays card schedules from the kept reviews
  and rewrites proof.json. A review or seen row whose row no longer derives is left out of the live file (it stays
  in the archive) and counted. Tests use temp state only; the live `learn/` data was not touched.

### Q695: what starts the proof clock, and who may start it

Answer: "an agent can fake it. thats fine, but it wouldnt be the usual behavior." then "a". Built:

- First exposure is the earlier of the first confirmed seen record and the first review. `rebuild_proof()` reads
  `seen.jsonl` for that, so `proof --check` still holds. A `session` on a new topic with every answer a pass is never
  learned: its passes sit inside 24 h of the lesson.
- `mark_seen`, `review`, `quiz` and `session` refuse any `FLEET_ROLE` (exit 4 from the CLI). `lesson`, `digest` and
  `brief` still print under a role, as a preview that records nothing. This is a norm, not a security boundary: an
  agent can unset the variable, and per the answer that is accepted; no further hardening.

### Q696: thin rows

Answer: "a". Every lesson line is backed by a verbatim quote or dropped, with no model rewording. Built: a `what` or
`why` line is kept only when at least half of its content words (3 or more letters, not a stop word) appear in the
row's quote or title; otherwise it is dropped and the lesson says how many lines it dropped. A test checks that a
line without a source quote never appears.

### Q702: rebuild straight from the real sources

Answer: "a". Built:

- **Where rebuild reads its sources.** `$FLEET_STATE/decisions-log.jsonl` (desk answers, decided-on-call lines),
  `$FLEET_STATE/watch/events.jsonl` (WATCH LEARN-ITEMs), achieved lanes' goal files from the roster, and the sources
  named in `$FLEET_STATE/sources.json` (else `watch.json`).
- **One shared config.** `sources.json` has the watch.json shape and passes the same validator; credentials are
  `env:`, `keychain:` or `cmd:` references, and a literal fails validation so nothing is polled. WATCH reads
  `sources.json` when it has no `watch.json`, so one file names the repos and trackers for both desks. A generic
  example is `docs/examples/sources.example.json`.
- **The registry is reused, not forked.** `lib/fleet_learn_sources.py` calls `fleet_watch_contract.load_source(kind)`,
  resolves auth through `fleet_watch_creds.resolve_auth`, and runs the events through WATCH's own `normalize`,
  `has_secret`, `match_drop` and `cap`. LEARN keeps its own cursors, so a LEARN poll never moves WATCH's.
- **New adapters.** `linear` (GraphQL over `urlopen`, read-only query, a personal key or `bearer` OAuth token) and
  `whosaid` (runs `whosaid decisions <ws> <meeting> --json --engine local`, bounded, in its own process group; no
  model, no network). Both are in `SOURCES` and `SOURCE_REQUIRED`.
- **Rows.** One per item ref; a new quote for the same ref supersedes the row, so it holds the item's latest state.
  whosaid `proposed` and `contested` decisions are heard; the rest are confirmed.
- Tests use fixtures and fakes only: a fake `urlopen` for Linear, a fake whosaid CLI that refuses any engine but
  local, and a `command` source that cats a recorded file. Names are generic (`PROJ-1`, `Dev One`, `acme/widget`).

### Q703: what a rebuild keeps

Answer: "b". A rebuild keeps only `reviews.jsonl` and `seen.jsonl`. Built: the earlier inputs journal was removed;
there is no `inputs.jsonl`. A row added by hand (`fleet learn add`, a ROUTE done line) has no source outside
`learn/` and does not survive a rebuild; ROUTE's done lines reach the ledger from goal files. A test wipes `learn/`,
puts back only those two files, rebuilds, and gets the same lessons and cards; another checks a hand-added row is
gone.

### Q697: the entity layer

Answer: "a". Built:

- **Typed links** `{"rel", "type", "ref"}` with rel one of `part-of`, `ships-in`, `blocked-by`, `decided-by`, `owner`
  (`fleet_watch_contract.LINK_RELS`). Adapters supply them in `Event.fields["links"]`: Linear gives part-of (parent
  issue, else team), ships-in (project, cycle), blocked-by (a "blocks" relation) and owner (assignee); whosaid gives
  part-of the meeting and decided-by the speaker. Decision rows get `decided-by desk:DECIDE` and `part-of
  lane:<asker>`; goal rows get `ships-in` (shipped) or `part-of` their lane. An unknown rel is left out.
- **`learn/entities.jsonl`**, rewritten by every sync: one entry per link target with the rows that point at it and
  the rel. It is derived, never kept state. The lesson tree (pane and page) shows each row's links with their rel.
- A test checks that links and entities come back the same after a rebuild, including from an empty state plus the
  two kept files.

## brief and digest

- `fleet learn brief --for TEXT [--max N]` is meeting prep: rows that matter for TEXT, as lesson lines. Selector:
  `learn.json {"briefSelector": {"kind": "command", "run": "...", "timeoutSec": 20}}` gets one JSON on stdin
  (`{"for", "max", "rows": [{lid, title, topic, quote, status, origin}]}`) and prints lids, one per line. Only lids of
  live rows are kept; any other line is ignored and counted; its text is never shown. The command runs bounded (its
  process group is killed on timeout). Fallback with no plugin, or when it fails or returns nothing: rows on topic
  for TEXT, else rows ranked by shared words with TEXT, else the most recent N. The JSON says which in `how`
  (`command|topic|words|recent`). Brief is read-only: it marks nothing seen.
- `fleet learn digest [--since ISO|24h|7d|last] [--all]` teaches the rows added since then. `last` (the default)
  reads a `digest` cursor in `cursors.json`; a human digest moves the cursor and marks the rows seen, a role preview
  moves nothing.

## Lesson tab in `fleet learn serve`

The page gets a Lesson tab: topics with new lessons first, then the lesson lines (quote and source on every line,
heard rows under "Not decided yet"), a small tree SVG of the topic, and "Mark seen and quiz this topic". It reads
`GET /api/lessons` (built from the same `lesson()` the pane prints) and writes only through `POST /api/seen`, which
calls `mark_seen(via="page")`. So a lesson seen in the page and one seen in the pane end in the same state, the same
rule as the quiz.

## Calls not answered on this branch: defaults taken (rec a)

| Call | Default taken here | Effect on this branch |
|---|---|---|
| Q698 desk UI rule | TUI first: the page calls the pane's verbs, never one of its own | The Lesson tab uses `lesson()` and `mark_seen()` only |
| Q699 web vs Tauri | web, loopback page | Nothing native added |
| Q700 hub | one `fleet ui serve` hub later | Not built: the Lesson tab lives in `fleet learn serve`, which a hub would mount |
| Q701 graphics | deterministic SVGs from the ledger, a text tree in the pane | The pane prints a text tree, the page draws the same tree as an SVG from the same data |

## Test changes to existing files

- `test/test_learn.sh`, `test/test_learn_serve.py` and `test/test_explain.py` set `FLEET_LEARN_TEACH_FIRST=0`, since
  those checks are about the card scheduler and the quiz, where cards were due at creation; their `--kind lesson`
  rows became `shipped` (Q694).
- `test/test_learn.sh` #24 now reads only the untyped fold links (`type == "decisions-log"`), since decision rows
  also carry typed links (Q697).
- `test/test_contracts.py` lists the two new registry kinds.
- New: `test/test_learn_lesson.py`, `test/test_learn_sources.py`, `test/test_watch_linear.py`.
