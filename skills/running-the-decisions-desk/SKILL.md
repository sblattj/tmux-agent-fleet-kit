---
name: running-the-decisions-desk
description: "Run the decisions desk: take lane calls into the desk goal file, dedupe, ask the human one numbered list, map terse answers (1a2a3a) to item ids, relay verbatim to the asker only, record with desk.sh (which also appends the shared decisions log) and print the GATE line. Triggers on: calls?, what are the calls, 1a 2a 3a, new call for the human, record the answer, relay my answers, decisions desk, needs id and decision, answered it in ROUTE, one call at a time, what's the difference, relay failed not reachable."
---

# running-the-decisions-desk

One session (the desk, `$FLEET_DECIDE`, default DECIDE) is the only place the human (`$FLEET_HUMAN`) answers calls. This is its loop: intake, ask, map, relay, record. The desk never runs `/goal`, never edits repos and never launches, parks or reopens lanes: new work, scope and launch/park requests go through ROUTE. The desk is a standing role and is never parked.

## Setup
- Desk goal file: `export FLEET_DESK_GOAL=<path>` (default `$FLEET_STATE/desk.goal.json`). Create it once: `fleet-goal new "$FLEET_DESK_GOAL" --goal "Run the decisions desk" --report-to ROUTE`.
- The helper is `desk.sh` next to this file (`~/.claude/skills/running-the-decisions-desk/desk.sh` after install). It validates with `fleet-goal check` after every edit; set `FLEET_GOAL_BIN` if `fleet-goal` is not on PATH.

## Do this
- Intake a lane call: `desk.sh add Q<n> <from-session> "<question with (a, rec) first>" <from-session>`
- What is open: `desk.sh open`
- The human answered: message the asker directly with the typed text verbatim plus the item text (Claude: `SendMessage`; Codex/OpenCode lanes: `peer_send`; both reopen a parked asker through `fleet send`). Do not copy ROUTE. Then `desk.sh answer Q<n> "<their words>" <asker>`: it records the decision in the desk goal file and appends `{id, at, asker, verbatim, relayedTo}` to `$FLEET_STATE/decisions-log.jsonl`, the shared log ROUTE reads. For a call that arrived through the mesh, `decision_answer {id, answer}` delivers to the asker only and appends the same log line, so a call closed with both `decision_answer` and `desk.sh answer` shows two log lines with the same id; readers key on the id and take the latest.
- Answered in another session (ROUTE says so): `desk.sh elsewhere Q<n> "<answer> (typed in <session>)"`, drop it from the list, do not re-ask. Only text typed in this session counts as an answer of the desk's own.
- The human adds to an answer already given: `desk.sh amend Q<n> "<their words> = <meaning>"`, then relay the addendum.
- A follow-up that an earlier answer already settles: relay it as derived from that answer with the quote, close it with `elsewhere`, do not re-ask.
- An ambiguous answer that would trigger something hard to undo: ask one short either/or line first, and look up what the change actually does so the re-ask carries the deciding fact.
- Second asker of the same call: `desk.sh relayto Q<n> add <session>`. Asker stopped or unreachable: `desk.sh relayto Q<n> set ROUTE`.
- Facts changed the pick: `desk.sh revise Q<n> "<new text>"` and re-show the item with the line that changed it.
- The human may answer in the DECISIONS UI instead (the `/decide/` tab of `fleet ui serve`, which `fleet decisions serve` opens, or `fleet decisions tui`; see the README). A UI pick marks `decisions.jsonl`, relays to every asker, copies ROUTE, and runs `desk.sh answer` itself, so the item is already closed in your goal file: do not relay it again. `fleet decisions state` shows what is open and what the UI relayed.
- After each relay batch print `GATE G1-relayed PASS` and the full `fleet-goal check` output.
- Calls can also arrive through the mesh: a lane's `decision_open` lands in your inbox (`peer_inbox`) and in `decisions.jsonl`. Close those with `decision_answer {id, answer}` (marks `decisions.jsonl`, delivers to the asker) as well as `desk.sh answer` (goal file).

## Rules of the desk
- Option letters are fixed when a call opens, in the asker's order; `(rec)` is a flag, never a position. Show and relay a pick as the letter plus its text (`b: retry once`), never a bare letter.
- One call at a time, few words. Number calls continuously across the session (shown number = fixed offset from the Q id) instead of renumbering each list. Keep a map of shown number to Q id before mapping "1a2a3a"; a bare "3" followed by text is a free-form answer.
- "What's the difference", "are they approved", "check how X deploys first" mean look it up (gh, git, the repo's CI file), answer in a few lines, then re-ask the same call. Never answer for the human.
- Duplicates: ROUTE may forward a call the asker already sent. Match on content and say "already item N"; do not add a second id.
- "Do what they decided on the call": relay the call's decisions with timestamped quotes and your mapping marked as your reading.
- A lane's "it is staged / unstaged / merged" claim can be stale: check the real state before repeating it.
- Ambiguous ids (numbers that repeat across directories, PR vs ticket ids) go out with full paths in a follow-up note.
- When the asker is not reachable (session closed, not on the roster), send the answer plus item text to ROUTE, ask it to reopen the lane, and `relayto set ROUTE` on that lane's other open items. A parked asker needs nothing special: `fleet send` / `peer_send` reopens it.
- If the desk vanished from the roster: a fresh desk with `fleet desks up` (it relaunches only the dead desk, into the right half of `deskA`), or the same session with `fleet launch --role decide --split-right-of ROUTE DECIDE resume <session-id>` (ids are in `fleet ls` and `$FLEET_STATE/roster.json`). Never a `/goal`.
- Layout: the desk is DECIDE, the right pane of window `deskA` with ROUTE on its left; `fleet desks up` starts it.
- Shell note for zsh users: zsh does not word-split a variable, so list relay targets as separate words.
