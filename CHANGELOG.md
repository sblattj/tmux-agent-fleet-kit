# Changelog

## Unreleased

- The mesh delivers a message for the retired desk names `DECISIONS` and `ORCHESTRATOR` to `DECIDE` and `ROUTE`, so `decision_open` and `peer_send` from a lane launched before the rename (its env still says `FLEET_DECIDE=DECISIONS FLEET_ROUTE=ORCHESTRATOR`) reach the live desks instead of an inbox nobody reads. It maps only when the roster has the new desk and no lane under the old name, so a fleet that still runs its desks under the old names, or has no desks yet, is unchanged.

## v0.4.0 (2026-11-23)

Side runs: work a lane starts outside the fleet's panes (codex, agy, gemini, an ach run) is tracked as a child of that lane.

- `fleet side start|end|ls|show`: a side-run store at `$FLEET_STATE/sides.jsonl`, written under an exclusive lock (ids `S<n>`, pid, log, result, bound, status, tokens, helper count). Fixes #11.
- A side run cannot open or answer decisions, and its `peer_send` goes only to its parent, signed `<parent>/<id>`. Fixes #12.
- Usage accounting: tokens come from the result file or the codex session (its helper threads included), plus a child-process count and a one-line summary. Fixes #13.
- A detached watcher per run marks it done, failed or timeout, kills the process tree once the bound passes (TERM, then KILL), and sends the parent exactly one inbox message with wall time, tokens and helper count. A watcher whose id was reused after a store reset leaves the new run alone. Fixes #14.
- `fleet side run --host H [--bound 30m] -- CMD`: starts CMD detached with no tty, sets `FLEET_SIDE`/`FLEET_PARENT`/`FLEET_SIDE_RESULT`, and records its log, result, exit code and tokens. Fixes #15.
- MCP tools `side_start`, `side_end`, `side_list` on the fleet serve. Fixes #16.
- Running side runs show as `  -> S1 codex gpt-x 7m/30m 2.1M tok` lines under their parent in `peer_list`, `fleet tick` and `fleet ls`, ending in ORPHAN when the parent is parked or gone. Fixes #17.
- `fleet park` and `fleet stop` kill the parent's side runs (`--keep-sides` keeps them and marks them orphan instead); `fleet cleanup` lists `ORPHAN-SIDE` lines, and `--apply --kill-orphans` kills them. Fixes #18.
- ach runs register themselves: `fleet side adopt-ach` (and `fleet tick --adopt`) read ach's run registry and attach each run to the lane that started it, matched by process ancestry, with no change to ach. Fixes #19.

Also in this release:

- `fleet desks up` always leaves window `deskA` at tmux index 0 and `deskB` at 1, live desks included: a desk window elsewhere is moved, or swapped with whatever window holds its index, with no pane or window id change and the current window kept. Before, a fresh `desks up` left them at 1 and 2 (the `home` window took 0), and lanes took any free index. Refs #9.
- `fleet launch` opens a lane window at the first free index from 2, so 0 and 1 stay for the desks even before `desks up` has run.

## v0.3.2 (2026-11-23)

- Fixed: a typed wake into a Claude Code lane in vim NORMAL mode lost its first characters. NORMAL keys ran as vim commands, so `[fleet] mes` was eaten and `sage from ...` was left in the box (seen on a live lane). Claude Code shows `-- INSERT --` while inserting and nothing in NORMAL mode, so the old `-- NORMAL --` check never fired. Now, when `editorMode` is `vim` in `~/.claude.json` (or `$FLEET_CLAUDE_JSON`), a Claude prompt with no `-- INSERT --` in the footer counts as busy, and the wake defers instead of typing.

## v0.3.1 (2026-11-23)

- Fixed: a guarded type of a slash command into Claude Code (`fleet park`'s `/compact`, `fleet_mesh.py type <pane> /mcp`) never sent Enter, so `fleet park` refused every live Claude lane. Claude Code draws a typed slash command in colour, and the post-type check read coloured cells as placeholder (an empty box). The check now also accepts the box when every cell, placeholder styling included, reads exactly the typed text; the pre-type emptiness check is unchanged. PROVEN on a live Claude Code 2.1.330 lane: guarded `/mcp` opened the menu, and `fleet park` compacted and parked.

## v0.3.0 (2026-11-23)

- Fixed: a fleet wake typed into a Claude, Kiro or agy pane spliced into whatever the human was typing there (`good morni[fleet] message from ...` submitted as one line). `wake()` typed the pointer and Enter without looking (lib/fleet_mesh.py, `wake`). Now a typed wake goes only into an empty, quiet prompt (allow-list read of the cursor row with `capture-pane -e`, plus recent client keys and copy mode); a busy pane gets a detached retry with backoff (`FLEET_WAKE_DEFER_MAX`, default 900 s) and then a tmux notice and `@fleet_unread` instead of typing. Enter is sent only once the box reads exactly the pointer. Residual race documented in INTERFACES.md, Typed wakes.
- Claude to Claude: `peer_send` (and the decision tools) from a live fleet Claude lane to another live Claude lane types nothing; it asks the sender to `SendMessage` the same text (delivered natively in the background on 2.1.330), the mirror hook links it to the inbox line, and a typed fallback runs only after `FLEET_NATIVE_GRACE` (120 s). Parked targets keep reopen plus the guarded typed wake (native messages to a stopped session are not queued, measured). `crossSessionInbound` is left unset: `accept` would open lanes to any same-user process.
- Typed wakes run under a per-pane lock, so two senders never type into one box at once, and a pointer left unsent by an earlier timeout gets its Enter on the next wake instead of blocking the lane.
- `fleet park` types `/compact` through the same guard and refuses to park (`FAIL can't type /compact into NAME (busy ...)`) while the lane's box holds text.
- `decision_open` and `decision_answer` called by a Claude lane toward a Claude lane go native like `peer_send`; the WATCH, probe and DECISIONS engines keep the guarded typed wake.
- `fleet_mesh.py wake-later <name>`, `fleet_mesh.py pane-state <pane>` and `fleet_mesh.py type <pane> <text>`.
- `fleet launch --split right|left|above|below --of NAME [--size PCT]`: open a lane as a pane on any side of another lane's pane (for example a call-following pane above ROUTE). `--split-right-of NAME` stays as an alias for `--split right --of NAME`. Closes #6.

## v0.2.0 (2026-11-22)

Phases 2 to 7 of the four desks (`docs/design/four-desks.md`).

- `fleet watch`, `fleet learn` and `fleet probe` verbs, with the WATCH and probe contracts (`lib/fleet_watch_contract.py`, `lib/fleet_probe_contract.py`) every engine builds against.
- `fleet learn`: the LEARN desk's engine (ledger, sync from the decisions log, WATCH and finished goal files, recall, multiple choice and source-check quizzes, 24 h spaced proof of learning, snapshot, markdown export). roles/learn.md now names its verbs.
- LEARN: FSRS scheduling (`lib/fleet_fsrs.py`, stdlib, from py-fsrs, MIT) replaces the fixed intervals and is the default; `"scheduler": "fixed"` in `$FLEET_STATE/learn/learn.json` opts out. Intervals match py-fsrs's published test values (e40a5a7).
- LEARN: explain back and apply cards, graded by a `command` grader (ungrounded lines dropped) or by self-grading when no grader is configured (`lib/fleet_learn_explain.py`).
- `fleet learn serve`: the LEARN page (loopback + token), proof per origin, score history per topic, and the same quiz as `fleet learn quiz` through the same `review()` verb.
- WATCH sources: Azure DevOps Boards (`ado`, two-step WIQL poll, basic-auth webhooks) and GitLab / Gitea (`gitlab`, one module, signed webhooks), each tested on recorded fixtures.
- `fleet watch`: the WATCH desk engine. `command` and `call` sources, normalize > scrub > dedupe > filters > route,
  asks to DECIDE as decisions, task candidates and fyi to DECIDE batched, LEARN-ITEMs to LEARN, never ROUTE.
  Credentials are env:/keychain:/cmd: references only; `fleet watch check` exits 1 on a literal. Terminal status
  pane, `snapshot()`, per-rule drop counts, quiet hours, a per-source token bucket (`ratePerMin`, default 30).
- DECIDE: WATCH source badges and links on cards, `fleet decisions intake|forward|learn`, task calls reach ROUTE only
  through the human's yes (once), fyi forwarded alone with a `decide:probe` log row; `decision_answer` records `by`.
- DECIDE probes (phase 4): `fleet probe start|due|nuked|resume|stats|fyi`, read-only probe lanes over a throwaway
  shallow clone (`lib/fleet_probe.py`, `roles/probe.*`), every finding a yes / no / nuke call, nuked repos skipped
  until resumed, a probe silent 15 min parked as stalled by `fleet cleanup --apply`. Probe hosts are allow-listed
  by live lockdown test (`test/test_probe_live.sh`, 2026-11-22): claude (Claude Code 2.1.330, 15/0), opencode
  (OpenCode 1.19.0, 17/0) and kiro (kiro-cli 3.5.0, 17/0), and through ach 0.32.0 (`test/test_probe_ach.sh`,
  2026-11-23): 51/0/0 across the three; codex and agy refused. Limit: the probe's shell and write tools were absent
  on all three, so a refusal was never exercised; "can't, while a worker can" is proven, "refused when tried" is not.
- Fixed: on OpenCode the fleet-probe MCP verbs were offered to every agent, fleet-worker included, because the global
  `permission` overrides the global `tools["fleet-probe_*"]=false`. The installer now denies `fleet-probe_*` in each
  agent's own permission except fleet-probe's (`test/test_probe_opencode_tools.sh`: worker offered 0, probe 8, the
  pre-fix worker 8). Rerun `install/opencode.sh` to apply it.
- `fleet watch serve`: WATCH page on loopback with a per-launch token, on the same snapshot() and verbs as the status pane.
- WATCH source: GitHub (`github`, repo events with ETag and `X-Poll-Interval`, notifications with a token, 429/403 backoff, webhook mapping).
- WATCH source: Jira (`jira`, Cloud v3 and DC v2, relative JQL window, changelog per issue, HMAC webhooks).
- WATCH source: Bitbucket Cloud (`bitbucket`, PR list by updated time, activity log, HMAC webhooks; pushes arrive at poll latency).
- WATCH source: Confluence (`confluence`, CQL poll with a time-zone overlap and resumable pages).
- WATCH: optional loopback webhook receiver (`fleet watch webhook serve`, port 7349) with GitHub, generic HMAC and basic-auth checks; webhook plus poll dedupe to one event.
- `fleet forget NAME [--force]`: archives one roster row, its inbox and cursor under `archive/forgotten/` and drops it; a lane whose pane is alive is refused without `--force`. The retired orchestrator, desk and listener roles are no longer standing: `fleet tick` marks their rows `forget` once the pane is dead (`test/test_forget.sh`).
- `fleet ls` shows `mesh=yes|no` per lane (from the launch record or the CLI's MCP config). For a mesh=no lane (started outside `fleet launch`, no peer_inbox tool) the wake line typed into its pane also names its inbox file: `... run peer_inbox to read it (no peer_inbox tool? read $FLEET_STATE/inbox/<name>.jsonl)`. A reopened claude lane gets the fleet MCP through `fleet launch` resume (`test/test_fleet.sh` checks every claude mode).
- Installers, upgrading from v0.1.x: the renamed roles' leftovers (desk, orchestrator, listener, now decide, route, watch) are retired. `install/opencode.sh` drops their `opencode.json` agents, whose `{file:...}` prompts point at deleted files and make OpenCode reject the whole config, so every OpenCode lane exited 1. kiro, agy and codex move their old `fleet-<role>` files aside as `.fleet-bak` copies. `install/opencode.sh` now fails, naming the agent and path, when a `{file:...}` prompt it writes or leaves on a `fleet-*` agent does not exist. A run that edits one file several times keeps the pre-run backup instead of overwriting it with an intermediate copy.

## v0.1.1 (2026-11-22)

Docs and wording now match the four desks.

- README Quick start (Claude Code): `fleet desks up` replaces the hand-launched ORCHESTRATOR and DECISIONS sessions, and the goal examples report to ROUTE.
- Fleet rules (`rules/fleet-rules.md`): the placeholders are now `{{ROUTE}}` and `{{DECIDE}}` (the installers fill them the same way, from `FLEET_ROUTE` and `FLEET_DECIDE`); the rules start the desks with `fleet desks up` and list the standing desks as ROUTE, DECIDE, WATCH and LEARN.
- The listener pane is now the WATCH UI: pane title `WATCH: <title>`, page title, `--help` text and docs. Wire names stay: `fleet listen`, `listener.json`, the `[listener]` message prefix and `OK listener` output.
- Worker role text, the README, INTERFACES.md and all four fleet skills say ROUTE and DECIDE instead of "the orchestrator"; the decisions desk skill restarts DECIDE with `fleet desks up` (or resumes it with `--role decide --split-right-of ROUTE`), and the retire skill starts the next ROUTE with `fleet desks up`.
- `docs/design/four-desks.md`: the status line now says phase 1 shipped in v0.1.0 and the rest is a design record.

## v0.1.0 (2026-11-22)

First tagged release.

- Four desks in two split windows: `fleet desks up` builds `deskA` (ROUTE | DECIDE) and `deskB` (WATCH | LEARN), plus a non-LLM `fleet watch status` pane under WATCH. The older ORCHESTRATOR, DECISIONS and LISTENER desks are retired.
- Goal lanes for five CLIs: Claude Code, Codex CLI, OpenCode v1, Kiro CLI (v3 engine) and Antigravity CLI (`fleet launch --cli agy`), alone or mixed, each started inside a goal locked to a living goal file.
- fleet-mesh MCP server: `peer_send`, `peer_inbox`, `peer_list`, and the decisions desk calls (`decision_open` and friends), with wake-on-message for idle lanes.
- Decisions UI (`lib/fleet_decisions.py`): a local web page over the desk's own state for answering calls without typing into the desk pane.
- Listener (`lib/fleet_listen.py`): parses live call transcripts, routes asks to the desks and drops any line that carries a secret-shaped string.
- Lane lifecycle (`bin/fleet-lifecycle`): tick, park, reopen, handoff and cleanup.
- Per-CLI installers (`install/*.sh`) and role files for every desk and the worker.
- Test suites on private tmux sockets with stub CLIs, plus a privacy scan (`test/test_privacy.sh`).
