# fleet learn contract (phase 3)

The LEARN ledger's on-disk layout, row schemas, `snapshot()` shape and the Python API other lanes call.
Source: docs/design/four-desks.md 4.1 to 4.5. Owner: the phase 3 lane (`lib/fleet_learn*.py`, `bin/fleet-learn`).
Python 3.8+ stdlib only. Everything lives under `$FLEET_STATE/learn/`; no other module writes there directly.
Other lanes call the API below (import `fleet_learn` from `lib/`) or the CLI; they never append to these files.

## Files (4.5)

```
$FLEET_STATE/learn/
  ledger.jsonl    append-only rows (below); a later row with the same lid supersedes the earlier one
  cards.jsonl     quiz cards with schedule, rewritten atomically
  reviews.jsonl   append-only review results: the record
  proof.json      derived cache per lid; rebuildable from reviews.jsonl + ledger + cards (rebuild_proof())
  manual.jsonl    append-only record of the human's manual card requests {at, skey, stack: manual|auto|dropped, row};
                  kept by rebuild (Q78), which re-marks the rows the sources bring back and re-adds the others
  cursors.json    sync cursors {"watch-events": <lines read>, "goals": {lane: "achieved"}}; decisions-log.jsonl is
                  re-read whole each sync (small; idempotent by source ref, so the latest answer per id wins)
  learn.json      optional config {"scheduler": "fsrs"|"fixed" (default fsrs), "cardFormats": [...], "learn": {"minScore": 1}, "fsrs": {...}}
  export/         default target of export(): one markdown file per topic
  .lock           flock for id allocation and rewrites
```

Every write resolves (realpath) inside `learn/`; a path that escapes, for example a symlinked `ledger.jsonl`, is
refused with `Refused`. `export(to=DIR)` is the one exception: the human may export anywhere; under
`FLEET_ROLE=learn` the target must also sit inside `learn/`.

## Ledger row (4.1)

```json
{"lid": "L42", "at": "2026-11-20T11:00:00Z", "kind": "decision",
 "origin": "fleet", "status": "confirmed",
 "title": "Retries cap at 3 with jittered backoff",
 "what": "Upload retries stop after 3 tries, 1 s to 8 s jittered.",
 "why": "A fourth try never succeeded in the logs; the cap keeps the queue short.",
 "source": {"type": "decisions-log", "ref": "Q12", "path": "$FLEET_STATE/decisions-log.jsonl", "line": 7},
 "quote": "a: cap at 3, jittered",
 "links": [{"type": "pr", "ref": "acme/widget#7", "eid": "github:acme/widget:7:merged"}],
 "tags": ["widget", "reliability"], "topic": "widget", "supersedes": null,
 "by": "sync", "confirmedBy": null}
```

| field | rule |
|---|---|
| `lid` | `L<n>`, allocated by the API under `.lock`; callers never choose it |
| `kind` | `decision`, `change`, `closed`, `doc`, `done` (a lane's goal-file `done[]` line; the old kind `lesson` is refused, Q694) |
| `origin` | `fleet` or `outside`; required, never inferred |
| `status` | `confirmed`, `heard`, `superseded`, `dropped`. `heard` = unconfirmed (a `decided-on-call` row); it becomes `confirmed` only through `confirm()` with a source other than its own, or a sync where a desk answer quotes it |
| `source` | object, `type` and `ref` required. Types in use: `decisions-log`, `watch`, `goal`, `route-done`, `decide-learn-this`, `manual`. Rows from `decisions-log`, `goal` and `route-done` with no `title` pass `teachable()`: fleet plumbing (lane lifecycle, desk or kit setup, routing, launch or park, tooling) and answerless lines (bare option letters, step names) are skipped by sync and `add`, and their cards retire. `learn: teach` / `learn: skip` in the line, or `"learn"` in the lane's goal file, overrides |
| `quote` | required, non-empty, verbatim from the source. Every grade is computed against it |
| `topic` | grouping key (same PR, call or lane share one); defaults to the first tag, else the source ref |
| `supersedes` | lid of the row this one replaces (a newer answer to the same call), or null |
| `by` | who wrote it: `sync`, `manual`, `decide`, `watch`, `route` |
| `confirmedBy` | `{source, quote}` when a heard row was confirmed |
| `stack` | `auto` (sync, sources, rebuild, a plain `add`) or `manual` (a card the human asked for: `add --manual`, `stack --manual`). Missing = `auto`. Manual rows are always teachable |
| `stackAt` | manual rows only: when the human asked for it (the request time that orders the manual stack) |

## Two stacks (manual ahead of auto)

Lessons (`lessons()`, a bare `lesson`, a bare `session`) and `due()` serve in this order: every manual row first,
grouped by topic, the topic with the newest `stackAt` first (the batch just asked for goes on top, an unshift), rows
in lid order inside it; then auto rows that share a topic or a work subject with a manual row (the topic signal);
then every other auto row, oldest first (lessons) or oldest due first (due). A topic with rows on both stacks is
listed once per stack, so the manual batch is taught on its own. Moving a row to manual again re-stamps `stackAt`.

Manual cards guide auto ones. `examples()` (`fleet learn examples`) lists the manual stack as few-shot examples
(prompt and quote), newest request first, for LEARN to title new auto rows the way the human asks. learn.json
`"autoPrompt": {"run": CMD, "timeoutSec": 10, "max": 10, "examples": 12, "maxTokens": 120}` (off by default, and off
while the manual stack is empty) runs CMD once per new untitled auto row, outside the lock, killed at `timeoutSec`:
stdin `{"row": {kind, quote, topic, what, why}, "examples": [...], "maxTokens": N}`, stdout one question. The first
line that ends in `?`, is 12 to 160 characters and names neither the source ref nor the lane becomes the card's
prompt (kept as `genPrompt` on the card); anything else leaves the template prompt. A human `--title` always wins.

## Card (4.3), `cards.jsonl`

```json
{"cid": "C88", "lid": "L42", "format": "recall|choice|source|explain|apply",
 "prompt": "...", "answer": "...", "options": ["...", "..."],
 "grounding": {"quote": "a: cap at 3, jittered", "source": "Q12"},
 "sched": {"due": "2026-11-23T09:00:00Z", "reps": 1, "lapses": 0, "lastReview": "2026-11-22T09:00:00Z",
           "fixedStep": 1,
           "state": "review", "step": null, "stability": 2.4, "difficulty": 5.1}}
```

Cards also carry `stack` (`auto`|`manual`, copied from the row and refreshed in place, cid and schedule kept) and,
when an autoPrompt command wrote the question, `genPrompt`.

`sched` keys. Shared: `due`, `reps`, `lapses`, `lastReview` (ISO or null). Phase 3 fixed intervals own
`fixedStep` (index into 1/3/7/21 d). FSRS (phase 5) owns `state` (`learning|review|relearning`), `step` (its
learning step), `stability`, `difficulty`. Each scheduler leaves the other's keys untouched.

Switching schedulers (fixed -> FSRS): no bulk migration. A card whose `sched` has no `stability` is treated by
the FSRS scheduler as a new card on its next review (first rating seeds stability and difficulty); its current
`due` stands until then. `reps`, `lapses` and `lastReview` carry over and may inform the seed. Switching back
ignores the FSRS keys and resumes from `fixedStep`.

Formats `explain` and `apply` (phase 5) share the card shape: `answer` is the rubric text from the row's `what`
(explain) or `why` (apply), `grounding.quote` is still the row quote. `make_cards(formats=[...])` or learn.json
`cardFormats` turns them on; phase 3 makes recall, source and choice only. `review()` refuses them without a `grade`.
Phase 3 formats: `recall` (self-rated, fixed intervals 1 d / 3 d / 7 d / 21 d), `choice` (1 of 4; distractors are
other ledger rows' text, never invented; fewer than 3 other rows means no choice card), `source` (the answer must
be a verbatim substring of the row's quote or source text). A card whose answer is not grounded in its quote is not
made. Cards for `heard` rows are never made as decided facts; `superseded` and `dropped` rows retire their cards.

## Review (`reviews.jsonl`)

```json
{"cid": "C88", "lid": "L42", "at": "...", "format": "recall", "answer": "...", "grade": "pass|fail",
 "rating": "again|hard|good|easy|null", "gradedBy": "self|exact|verbatim", "latencyMs": 4200, "quoteChecked": true}
```

Generative vs checkable: `recall`, `explain`, `apply` are generative; `choice`, `source` are checkable. A pass
counts by its `grade` alone; `gradedBy` (`self|exact|verbatim|command`) is recorded, never consulted by the proof
rules. So a self-graded explain back pass (the no-grader fallback, source shown) counts as a generative pass, the
same as a self-rated recall. A `command` grader must already return `grade: fail` when a core item is missing or
any item is wrong; `review()` takes that grade as given.

## proof.json (4.4)

`{"<lid>": {"learned": bool, "firstSeen": iso, "passes": [{"at", "format", "kind": "generative|checkable"}],
"lapsedAt": iso|null, "due": iso|null}}`. Learned = a generative pass (recall good/easy) AND a checkable pass
(choice correct or source verbatim) AND the latest pass at least 24 h after firstSeen AND no lapse since the last
pass. `rebuild_proof()` recomputes it from `reviews.jsonl`; the cached file must equal the rebuild.

## Python API (`lib/fleet_learn.py`)

All functions take an optional `now=` (a `datetime`, UTC) so tests inject the clock. Errors raise `Refused`.

```python
add_item(*, kind, origin, quote, source, title="", what="", why="", status="confirmed",
         links=None, tags=None, topic=None, by="manual") -> dict   # the new ledger row
# DECIDE "learn this":  add_item(kind="decision", origin="fleet", quote=<human's verbatim words>,
#                                source={"type": "decide-learn-this", "ref": "Q12"}, by="decide")
# WATCH LEARN-ITEM:     add_learn_item(event) -> dict | None     (event = a watch/events.jsonl row; origin outside;
#                       dedupes on event eid; quote = event's quote field; returns None for a dupe)
# ROUTE "done" copy:    add_item(kind="done", origin="fleet", quote=<done line>, source={"type": "route-done",
#                                "ref": <lane>}, by="route")   (not kept by rebuild(); done lines come from goal files)
# Sources (Q702):       sync() also polls sources.json (else watch.json) through WATCH's adapter registry and adds
#                       each event with add_learn_item(event, by="source"); whosaid call decisions are heard
# Teach first:          mark_seen(lids, via) (human only) starts the 24 h clock; due() lists only seen rows' cards
# Rebuild (Q703 b):      rebuild() regenerates ledger, cards, entities, proof, cursors from the sources; keeps only
#                       reviews.jsonl and seen.jsonl, which re-attach by source key (skey) and card key (ckey)
confirm(lid, *, source, quote, by="manual") -> dict   # heard -> confirmed
supersede(lid, *, quote, source, by="sync") -> dict   # new row, old one status superseded
drop(lid, *, reason) -> dict     # a manual row's drop is recorded in manual.jsonl, so a rebuild does not bring it back
set_stack(lids, stack, now=None) -> list   # auto|manual, a new row version per changed lid + a manual.jsonl record;
                                # LEARN or the human. add_item(..., stack="manual") makes a manual row directly
stack_of(row) -> "auto"|"manual"
serve_order(rows=None) -> key fn  # the two-stack sort key lessons and due() use
examples(topic=None, max_n=20) -> list   # [{lid, topic, kind, prompt, quote}] from the manual stack
items(*, origin=None, status=None, include_retired=False) -> list   # latest row per lid
item(lid) -> dict
sync(now=None) -> dict          # decisions-log.jsonl (absent log = no-op), watch/events.jsonl LEARN-ITEMs,
                                # achieved lanes' goal files; returns {"added": [...], "confirmed": [...], ...}
make_cards(now=None, formats=None) -> list   # new cards for rows without them; formats default to learn.json
                                # "cardFormats", else recall/source/choice. explain (answer = row what) and apply
                                # (answer = row why) are made only when asked for and only if the row has that field
reviews(lid=None) -> list       # reviews.jsonl rows, oldest first (score history for the phase 7 page)
due(now=None) -> list           # cards due
review(cid, answer, rating=None, now=None, grade=None, gradedBy=None, detail=None, latencyMs=None) -> dict
                                # grades, appends reviews.jsonl, updates sched + proof. The TUI and the phase 7
                                # page both call this, so a page answer and a TUI answer end in the same state.
                                # grade/gradedBy/detail: for externally graded formats (phase 5 explain/apply
                                # graders pass grade="pass|fail", gradedBy="command|self", detail={covered,missing,wrong})
scheduler() -> fn               # what review() calls, resolved before the review row is appended. Default fsrs
                                # (4.3: FSRS replaces fixed intervals): fleet_fsrs.schedule after fleet_fsrs.configured().
                                # learn.json {"scheduler": "fixed"} opts out to 1/3/7/21 d. fleet_fsrs missing, a bad
                                # fsrs section or an unknown name raises Refused; never a silent fallback
use_scheduler(fn)               # fn(sched: dict, rating: str, now: datetime) -> sched dict; pins it for this process
                                # (tests); use_scheduler(None) goes back to learn.json
rebuild_proof() -> dict
snapshot(now=None) -> dict      # below; read-only, for the phase 7 page and status lines
export(to=None) -> list[str]
```

## snapshot() shape

```json
{"at": "...", "dir": "$FLEET_STATE/learn",
 "byStack": {"manual": {"items": 18, "learned": 0, "due": 4}, "auto": {"items": 120, "learned": 9, "due": 3}},
 "byOrigin": {"fleet":   {"items": 15, "confirmed": 12, "heard": 3, "learned": 12, "due": 2},
              "outside": {"items": 20, "confirmed": 20, "heard": 0, "learned": 4,  "due": 5}},
 "topics": [{"topic": "widget", "origin": "fleet", "items": 3, "learned": 2, "pct": 67}],
 "due": [{"cid": "C88", "lid": "L42", "format": "recall", "prompt": "...", "origin": "fleet"}],
 "heard": [{"lid": "L7", "quote": "...", "source": {...}}],
 "recent": [{"cid": "C88", "at": "...", "grade": "pass"}]}
```

Origins are never blended: no top-level total.

## CLI (`bin/fleet-learn`, `fleet learn` once the lander adds the dispatch line)

`add` (`--manual`), `stack`, `examples`, `list`, `show`, `confirm`, `drop`, `sync`, `due`, `quiz`, `review`, `proof`, `snapshot`, `export`, and
`serve` (dispatched to `lib/fleet_learn_serve.py` main(argv) before argparse; phase 7). `fleet-learn -h` is the
reference. `FLEET_LEARN_NOW=<ISO UTC>` pins the clock for every verb.
