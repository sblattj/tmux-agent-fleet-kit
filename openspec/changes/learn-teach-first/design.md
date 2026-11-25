# Design

## Context

`lib/fleet_learn.py` keeps an append-only ledger (latest row per lid wins), cards rewritten in place, reviews as
the record, and a proof cache equal to `rebuild_proof()`. Today a card is due at creation and `firstSeen` is the
first review, so the human meets new work inside a quiz. Q694 (answered) threw the old data away and asked that
everything LEARN holds be re-derivable from sources. Q695 (answered) made "seen" the start of the proof clock,
marked by the human, with a role guard that is a norm and not a security boundary. Q696 (answered) made lesson
text verbatim from sources, with no model rewording by default.

## Goals / Non-Goals

Goals: lesson before quiz; quote and source on every lesson line; seen-gated scheduling; a session per topic; a
brief with a pluggable selector; a digest; a Lesson tab; the role text; a tested rebuild from sources.

Non-goals: migration of old rows (Q694); hardening the role guard against a determined agent (Q695); a Tauri app
(Q699 rec a, web page stays); a cross-machine hub (Q700 rec a); picture generation beyond a deterministic SVG
(Q701 rec a).

## Decisions

1. **Lesson model: a term, then the term in context (the human's model).** A lesson groups unseen rows by topic.
   Terms come first: each work subject (entity) the rows touch, with a definition backed by a quote (an entity's
   own `--quote`, else the earliest row on it). Then In context: each row's verbatim quote and source, then its
   grounded `what` and `why` lines. A lesson and its cards form a tuple through the lid: lesson (seen) then quiz.
   Rejected: a lesson as a card format (it would land in reviews and proof and count as exposure graded as a pass);
   title plus quote only (no definitions, so nothing to learn the term from).
2. **Grounding (Q696 answered "a").** LEARN writes definitions and examples from sources while it builds or
   rebuilds: verbatim quotes only, no model rewording by default. A `what` or `why` line shows only when it is a
   normalized substring of the row quote. `learn.json` `lesson.allowRewording: true` also allows `text | quote: X`
   with X a normalized substring. Anything else is dropped and counted.
3. **Stable keys.** `skey` = `type:ref#h8` where h8 is the first 8 hex of sha1 of the normalized quote, so a
   superseded answer with the same ref is a new key. `ckey` = `skey/format`. Seen and review rows carry them.
4. **Seen record.** `seen.jsonl` rows `{lid, skey, at, via}` (`via` = lesson, session, digest, page). Writing seen
   calls `write_proof()`. `firstSeen` = earliest of first seen and first review.
5. **Gate.** `due()` lists a card only when its row has a seen record (resolved by skey). `lessons()` lists live
   teachable rows with no seen record. Heard rows are shown in lessons as "not decided yet" and never get cards.
6. **Backlog cap.** After the clean slate every row is unseen, so a bare `lesson` teaches only the first topic with
   unseen rows, up to `--max` (8) rows, and prints how many lessons remain. Digest has the same cap.
7. **Human-only guard (Q695).** `human_only(verb)` moves into `fleet_learn.py`; `lesson`, `session`, `digest`,
   `quiz`, `review()` and the page's seen and review calls refuse under any `FLEET_ROLE` (exit 4). `--preview`,
   `lessons`, `brief`, `due` and `snapshot` stay open. The human runs the quiz from a shell without `FLEET_ROLE` or
   on the page.
8. **Rebuild from sources (Q694, Q27 b, Q702 a).** Kept records: only `reviews.jsonl` and `seen.jsonl` (the
   human's own results). Messages LEARN was handed (`add`, `confirm`, `drop`, `curate`) are not kept; ROUTE done
   lines come back from the goal files. Everything else is derived: `ledger.jsonl`, `cards.jsonl`,
   `entities.jsonl`, `proof.json`, `cursors.json`. `rebuild` first polls every configured source into memory (a
   failure aborts with nothing changed), then moves the derived files to `rebuild-prev/`, runs `sync` from the
   local files plus the polled events, makes cards, then replays reviews in order through the scheduler to restore
   each card's schedule. Reviews and seen resolve to the current lid and cid through skey and ckey at read time,
   so the records are never rewritten. Orphans (key no longer derived) are counted. The digest cursor is derived
   from `seen.jsonl` (latest `via: digest`), not stored. Choice distractors seed on skey, so they are the same
   after a rebuild.
9. **Entities (Q697 a).** Derived from row topics at sync (a `owner/repo#N` topic gives a `pr` part-of
   `owner/repo`; a ticket id gives `ticket`; a release gives `release`) plus the links the adapters report in
   `fields.edges` (Linear: project, blockers, assignee, cycle; call decisions: decided-by the meeting). Five edge types only.
   `fleet learn entity list|tree` reads them; there is no `entity add`, since it would not survive a rebuild.
   `on_topic` also matches rows whose topic's part-of chain reaches T.
10. **Brief selector.** Reuses `fleet_learn_explain.run_bounded`: JSON `{for, max, rows}` on stdin, lid lines out,
    unknown lids dropped, a failure falls back and says so.
11. **Page.** `GET /api/lessons`, `POST /api/seen` with the existing token, Host and Origin checks, calling the same
    `mark_seen()` as the pane. The tree SVG is laid out from depth and order alone.
12. **Kind rename.** Ledger kind `lesson` (a goal done line) becomes `done`; `--kind lesson` is refused naming
    `done`. ROUTE and LEARN role texts update their `add` line.
13. **Sources (Q702 a).** `$FLEET_STATE/sources.json`, else `watch.json`, with `env:`/`keychain:`/`cmd:` refs only,
    polled through WATCH's adapter registry (`lib/fleet_learn_sources.py`). New adapters: `whosaid` (call
    decisions from a local workspace through the `whosaid` CLI, bounded, rows arrive `heard`) and `linear`
    (GraphQL, read-only, cursor paging, `retry_after`). Secret-shaped event text is dropped. Tests use fakes.

## Risks / Trade-offs

- A manual `fleet learn add` or `curate` is a message, not a source, so a rebuild drops it (Q27 b); its seen
  rows show as orphaned. ROUTE done lines survive because they come from the goal files.
- Source history depth: whosaid and Linear read from the start (Linear up to 5 pages of 50 per poll); the GitHub
  events feed is short, so an old PR may not come back from a rebuild unless it is in a goal file or the log.
- The role guard is a norm (Q695): an agent can run `env -u FLEET_ROLE`. Accepted.
- Existing tests expect cards due right after `add`; each gets a seen step in setup (listed in DESIGN-NOTES.md).

## Migration Plan

None (Q694). When the chosen branch lands, the live `$FLEET_STATE/learn/` is wiped and `fleet learn rebuild` runs.
This worktree never touches the live state; tests use temp state only.
