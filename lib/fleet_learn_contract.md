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
  reviews.jsonl   append-only review results: the human's record (kept by rebuild, re-keyed onto new ids)
  seen.jsonl      append-only {"lid", "at", "status", "via": "lesson|session|digest|page", "topic", "key"}: the
                  human saw that row's lesson while it had that status; also the human's record, kept by rebuild
  inputs.jsonl    append-only journal of desk input with no other source: {"verb": "add|confirm|drop", ...}, keyed
                  by "key" / "target" = "<source type>|<source ref>|<normalized quote>", never by lid. Sync rows
                  (by sync or watch) are not journaled; they re-derive from their sources
  archive/<stamp>/  copies of the files above, made by rebuild() before it re-derives; never deleted by the kit
  proof.json      derived cache per lid; rebuildable from reviews.jsonl + seen.jsonl (rebuild_proof())
  cursors.json    sync cursors {"watch-events": <lines read>, "goals": {lane: "achieved"}}; decisions-log.jsonl is
                  re-read whole each sync (small; idempotent by source ref, so the latest answer per id wins)
  learn.json      optional config {"scheduler": "fsrs"|"fixed" (default fsrs), "cardFormats": [...], "learn": {"minScore": 1}, "fsrs": {...},
                  "teachFirst": true|false (default true), "briefSelector": {"kind": "command", "run": CMD, "timeoutSec": 20}}

Re-derivable: everything except reviews.jsonl and seen.jsonl comes back from `rebuild()` (sources:
`$FLEET_STATE/decisions-log.jsonl`, `$FLEET_STATE/watch/events.jsonl`, achieved lanes' goal files from the roster,
then inputs.jsonl). A row's identity across rebuilds is its key (source type, ref, normalized quote).
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
| `kind` | `decision`, `change`, `closed`, `doc`, `shipped` (a goal or ROUTE done line) |
| `origin` | `fleet` or `outside`; required, never inferred |
| `status` | `confirmed`, `heard`, `superseded`, `dropped`. `heard` = unconfirmed (a `decided-on-call` row); it becomes `confirmed` only through `confirm()` with a source other than its own, or a sync where a desk answer quotes it |
| `source` | object, `type` and `ref` required. Types in use: `decisions-log`, `watch`, `goal`, `route-done`, `decide-learn-this`, `manual`. Rows from `decisions-log`, `goal` and `route-done` with no `title` pass `teachable()`: fleet plumbing (lane lifecycle, desk or kit setup, routing, launch or park, tooling) and answerless lines (bare option letters, step names) are skipped by sync and `add`, and their cards retire. `learn: teach` / `learn: skip` in the line, or `"learn"` in the lane's goal file, overrides |
| `quote` | required, non-empty, verbatim from the source. Every grade is computed against it |
| `topic` | grouping key (same PR, call or lane share one); defaults to the first tag, else the source ref |
| `supersedes` | lid of the row this one replaces (a newer answer to the same call), or null |
| `by` | who wrote it: `sync`, `manual`, `decide`, `watch`, `route` |
| `confirmedBy` | `{source, quote}` when a heard row was confirmed |

## Card (4.3), `cards.jsonl`

```json
{"cid": "C88", "lid": "L42", "format": "recall|choice|source|explain|apply",
 "prompt": "...", "answer": "...", "options": ["...", "..."],
 "grounding": {"quote": "a: cap at 3, jittered", "source": "Q12"},
 "sched": {"due": "2026-11-23T09:00:00Z", "reps": 1, "lapses": 0, "lastReview": "2026-11-22T09:00:00Z",
           "fixedStep": 1,
           "state": "review", "step": null, "stability": 2.4, "difficulty": 5.1}}
```

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
pass. firstSeen = the earlier of the lid's first review and its first seen.jsonl row with status confirmed (the
lesson starts the 24 h clock, Q695); only lids with a review get an entry. `rebuild_proof()` recomputes it from
`reviews.jsonl` and `seen.jsonl`; the cached file must equal the rebuild.

## Lesson first (seen.jsonl)

With `teachFirst` on (the default; `learn.json {"teachFirst": false}` or `FLEET_LEARN_TEACH_FIRST=0` turns it off),
`due()` returns a card only when its row has a seen row with status confirmed. `lessons_due()` lists live teachable
rows not yet seen as their current status. `mark_seen()`, `review()` and `quiz()` raise `RoleRefused` (a `Refused`;
CLI exit 4) under any `FLEET_ROLE`: only the human marks a lesson seen or answers a card. This is a norm, not a
security boundary.

## Python API (`lib/fleet_learn.py`)

All functions take an optional `now=` (a `datetime`, UTC) so tests inject the clock. Errors raise `Refused`.

```python
add_item(*, kind, origin, quote, source, title="", what="", why="", status="confirmed",
         links=None, tags=None, topic=None, by="manual") -> dict   # the new ledger row
# DECIDE "learn this":  add_item(kind="decision", origin="fleet", quote=<human's verbatim words>,
#                                source={"type": "decide-learn-this", "ref": "Q12"}, by="decide")
# WATCH LEARN-ITEM:     add_learn_item(event) -> dict | None     (event = a watch/events.jsonl row; origin outside;
#                       dedupes on event eid; quote = event's quote field; returns None for a dupe)
# ROUTE "done" copy:    add_item(kind="shipped", origin="fleet", quote=<done line>, source={"type": "route-done",
#                                "ref": <lane>}, by="route")
confirm(lid, *, source, quote, by="manual") -> dict   # heard -> confirmed
supersede(lid, *, quote, source, by="sync") -> dict   # new row, old one status superseded
drop(lid, *, reason) -> dict
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
teach_first() -> bool           # learn.json teachFirst (default True); FLEET_LEARN_TEACH_FIRST=0|1 overrides
exposed() -> set                # lids seen as confirmed
lessons_due(now=None, topic=None) -> list   # live teachable rows whose lesson is due, oldest first
mark_seen(lids, via="lesson", topic=None, now=None) -> list   # human only; appends seen.jsonl; newly seen lids
rebuild(now=None) -> dict       # archive, re-derive from sources + inputs.jsonl, re-key reviews and seen, replay
                                # card schedules; {"archive", "rows", "cards", "inputsReplayed", "reviewsKept",
                                # "reviewsLeftOut", "seenKept", "seenLeftOut", ...}
source_key(row) -> str          # "<type>|<ref>|<normalized quote>": a row's identity across rebuilds
snapshot(now=None) -> dict      # below; read-only, for the phase 7 page and status lines
export(to=None) -> list[str]
```

## snapshot() shape

```json
{"at": "...", "dir": "$FLEET_STATE/learn",
 "byOrigin": {"fleet":   {"items": 15, "confirmed": 12, "heard": 3, "learned": 12, "due": 2},
              "outside": {"items": 20, "confirmed": 20, "heard": 0, "learned": 4,  "due": 5}},
 "topics": [{"topic": "widget", "origin": "fleet", "items": 3, "learned": 2, "pct": 67}],
 "due": [{"cid": "C88", "lid": "L42", "format": "recall", "prompt": "...", "origin": "fleet"}],
 "heard": [{"lid": "L7", "quote": "...", "source": {...}}],
 "lessonsDue": [{"lid": "L43", "topic": "widget", "origin": "fleet", "status": "confirmed"}],
 "recent": [{"cid": "C88", "at": "...", "grade": "pass"}]}
```

Origins are never blended: no top-level total.

## CLI (`bin/fleet-learn`, `fleet learn` once the lander adds the dispatch line)

`add`, `list`, `show`, `confirm`, `drop`, `sync`, `due`, `quiz`, `review`, `proof`, `snapshot`, `export`,
`rebuild`, `serve` (dispatched to `lib/fleet_learn_serve.py` main(argv) before argparse; phase 7), and `lesson`,
`session`, `brief`, `digest` (dispatched to `lib/fleet_learn_lesson.py`). The page adds `GET /api/lessons[?topic=T]`
(the pane's `lesson()`) and `POST /api/seen {"lids": [...], "topic"}` (`mark_seen(via="page")`). `fleet-learn -h` is the
reference. `FLEET_LEARN_NOW=<ISO UTC>` pins the clock for every verb.
