## 1. Core: keys, seen, gate, guard

- [x] 1.1 Add `skey`/`ckey`, rename kind `lesson` to `done` (refuse old name), seed choice on skey; verify: test_learn.sh green after updating `--kind lesson` call sites
- [x] 1.2 Add `seen.jsonl`, `mark_seen()`, `seen()`, `lessons()`; gate `due()`; `firstSeen` from seen; verify: new test shows 0 due before seen and due after
- [x] 1.3 Move `human_only` into fleet_learn.py; guard `quiz`, `review()`, seen writers; verify: FLEET_ROLE=learn review and lesson exit 4 and write nothing

## 2. Lessons and session

- [x] 2.1 New `lib/fleet_learn_lesson.py`: lesson model (terms, context, grounding, dropped count), text and JSON render; verify: backed line kept, unbacked dropped, heard shows "not decided yet"
- [x] 2.2 `fleet learn lesson [--topic T] [--max N] [--preview] [--json]` and `fleet learn lessons`; verify: second run prints `no new lessons`, preview writes nothing
- [x] 2.3 `fleet learn session --topic T`; verify: lesson, topic-only quiz, result line with `0 learned`
- [x] 2.4 `fleet learn curate`; verify: show --json has what, lesson shows it

## 3. Rebuild

- [x] 3.1 Sources (Q702 a): `sources.json` (else `watch.json`), refs-only creds, whosaid and linear adapters through WATCH's registry; verify: test_learn_sources.py on fakes (inputs.jsonl dropped by Q27 b)
- [x] 3.2 `fleet learn rebuild`; reviews and seen resolve by key; verify: delete derived files, rebuild, rows, entities, cards, due times and proof equal; a manual add is orphaned; a failing source changes nothing

## 4. Entities, brief, digest

- [x] 4.1 Entities derived from topics and adapter links, `entity list|tree` (Q697 a); subtree rollup in on_topic; verify: repo topic includes its PR rows, tree indents
- [x] 4.2 `fleet learn brief --for X` with selector and fallback; verify: selector picks, failing selector falls back, worker role allowed, writes nothing
- [x] 4.3 `fleet learn digest [--since]` with cursor from seen; verify: since 2d filters, second run says nothing new, role preview allowed

## 5. Page, role, docs

- [x] 5.1 `/api/lessons`, `/api/seen`, Lesson tab with deterministic SVG; verify: test_learn_serve.py cases for token, seen and JSON equal to pane
- [x] 5.2 roles/learn.md (+ codex/agy variants, route kind line), contract, README, `-h` docstring; verify: role grep test, `-h` loop covers new verbs
- [ ] 5.3 Full suite against the 3e306bd baseline, leak lint on a clean export; verify: same failures as baseline only
