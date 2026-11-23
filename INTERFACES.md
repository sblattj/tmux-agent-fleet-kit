# Frozen interfaces (contract between bin/fleet, lib/fleet_mesh.py, bin/fleet-goal, installs)

Portability: every script is `#!/usr/bin/env bash` and must run on bash 3.2 (macOS /bin/bash) and bash 5 (Linux).
No associative arrays, no `mapfile`, no `${x,,}`, no GNU-only flags (`sed -i` without suffix, `date -d`, `grep -P`).
Python: 3.8+ stdlib only. Runtime deps: bash, tmux >= 3.0, jq, python3, and the CLI being launched.
No personal data anywhere: no person names, emails, home-directory paths, account names, employer names.
The human is `$FLEET_HUMAN` (default `the human`); desk names are `$FLEET_DECIDE` (default `DECIDE`)
and `$FLEET_ROUTE` (default `ROUTE`). There is no alias layer: a message goes to the lane it names.

## Environment (all optional)

| Var | Default | Meaning |
|---|---|---|
| FLEET_TMUX_SESSION | `fleet` | tmux session every window is created in (created detached if missing) |
| FLEET_TMUX_SOCKET | empty | if set, every tmux call gets `-L $FLEET_TMUX_SOCKET` (tests use a private server) |
| FLEET_STATE | `${XDG_STATE_HOME:-$HOME/.local/state}/fleet` | roster, inboxes, launch scripts |
| FLEET_DIR | `$PWD` | working dir for a launched lane (`--dir` overrides) |
| FLEET_HUMAN | `the human` | who answers DECIDE calls |
| FLEET_DECIDE / FLEET_ROUTE | `DECIDE` / `ROUTE` | desk lane names |
| FLEET_NAME | set by the launcher inside each window | the lane's own name (fleet_mesh.py reads it as the sender) |
| FLEET_CLAUDE_BIN / FLEET_CODEX_BIN / FLEET_OPENCODE_BIN | `claude` / `codex` / `opencode` on PATH | binaries (tests point them at stubs) |
| FLEET_KIRO_BIN | `kiro-cli` | Kiro binary (tests point it at a stub) |
| KIRO_HOME | `~/.kiro` | Kiro's global dir (sessions, leases, agents, settings, steering); passed to kiro lanes when set; read by install/kiro.sh and bin/fleet-lifecycle |
| FLEET_KIRO_GOAL_MAX | `20` | kiro goal mode rewrites `/goal ` to `/goal --max N `; positive integer (Kiro's own default is 5, DOC) |
| FLEET_AGY_BIN | `agy` | Antigravity CLI binary (tests point it at a stub) |
| FLEET_AGY_HOME | `~/.gemini` | where agy keeps `config/` and `antigravity-cli/` (agents, rules, mcp_config.json, brain/<conversation>/ transcripts); passed to agy lanes when set; read by install/agy.sh and bin/fleet-lifecycle. agy itself always uses `$HOME/.gemini` (PROVEN 1.5.13), so this must point there |

## State layout ($FLEET_STATE)

```
roster.json            {"<name>": {"name","cli","mode","pane","session_id","port","dir","model","pid","at"}}
                       cli: claude|codex|opencode|kiro|agy ; pane: tmux pane id like "%12" ; port: int or null ;
                       session_id: claude uuid | codex thread uuid | opencode "ses_..." | kiro "sess_<uuid>" | agy conversation uuid | null until known
inbox/<name>.jsonl     one JSON per line: {"id","at","from","to","text"}
inbox/<name>.cursor    integer: number of lines already returned by peer_inbox
decisions.jsonl        {"id":"Q<n>","at","from","question","options":[...],"status":"open|answered","answer","answeredAt"}
launch/<name>.sh       the window script ; launch/<name>.txt the first prompt ; launch/<name>.launch.json
log/fleet.log          one line per launch/exit/delivery
```
Writers update roster.json atomically (write temp file in the same dir, then `mv`).

## bin/fleet

```
fleet launch [--cli claude|codex|opencode|kiro|agy] [--role R] [--model M] [--dir D] [--split-right-of NAME] [--dry-run] [--no-proof]
             <name> goal <condition.txt> | prompt <text|file> | desk [text|file] | resume <session-id>
fleet ls                      roster + liveness (pane alive?) one line per lane
fleet attach                  tmux attach -t $FLEET_TMUX_SESSION
fleet layout                  print the tmux window/pane map
fleet stop <name>             kill that pane (asks nothing; used by tests and retire)
```
- `<name>` matches `^[A-Za-z0-9._-]+$`; refuse if roster has a live pane for it.
- goal mode condition lint (same as today's launcher): starts with `/goal `, <= 4000 chars, names a `*.json` goal file,
  contains `file wins`, contains `stop after`. desk mode refuses text starting with `/goal`.
- desk mode default text: `You are <name>, the standing decisions desk for $FLEET_HUMAN. Load the running-the-decisions-desk skill and run it. Never set a /goal.`
- Opening: `tmux new-window -d -P -F '#{pane_id}' -t <session>: -n <name> -c <dir> <script>`; with `--split-right-of X`:
  `tmux split-window -d -h -P -F '#{pane_id}' -t <pane of X from roster> -c <dir> <script>`. Create the session
  with `tmux new-session -d -s <session>` if `has-session` fails. Nothing else: no terminal-emulator calls.
- The window script (bash) unsets CLAUDE_CODE_CHILD_SESSION CLAUDECODE CLAUDE_CODE_SESSION_ID CLAUDE_CODE_AGENT
  CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_JOB_DIR CLAUDE_PID CLAUDE_CODE_ENTRYPOINT
  CLAUDE_CODE_SESSION_ATTENDED CLAUDE_CODE_EXECPATH, exports FLEET_NAME=<name> FLEET_STATE, logs start/exit.
- claude: `export CLAUDE_CODE_EVEN_MORE_DANGEROUSLY_SKIP_PERMISSIONS=1`;
  `claude --dangerously-skip-permissions -n <name> --model <m> --disallowed-tools=AskUserQuestion --session-id <uuid> -- "$(cat <txt>)"`
  (a worker lane: `--disallowed-tools=AskUserQuestion,Workflow,ScheduleWakeup,ReportFindings`, minus any tool in the goal JSON's `keepTools` or `--keep-tools`; roster fields `keepTools` and `blockedTools`)
  (resume: `--resume <id>` and no prompt). Proof: pid by `pgrep -f -- "-n <name> .*<uuid>"`; env from
  `/proc/<pid>/environ` (Linux) or `ps eww -o command= -p <pid>` (macOS) must contain the skip var and lack
  CLAUDE_CODE_CHILD_SESSION; goal mode waits <= 90 s for `Goal set:` in `~/.claude/projects/*/<uuid>.jsonl`.
- codex: `codex --dangerously-bypass-approvals-and-sandbox [-m <m>] [-C <dir>] "$(cat <txt>)"` (resume: `codex resume <id>`
  with the same bypass flag). After the TUI is up (pane shows a prompt; wait <= 30 s), the launcher runs
  `tmux send-keys -t <pane> -l "/rename <name>"`, sleeps 1, then sends `Enter` twice with a 1 s gap
  (PROVEN locally: the first Enter can be eaten by the slash-command popup). Session id: newest
  `~/.codex/sessions/**/rollout-*.jsonl` created after launch whose first line `.payload.cwd` equals the real path of
  dir -> `.payload.id`. Goal proof: python3 sqlite3 on `~/.codex/goals_1.sqlite`:
  `select status from thread_goals where thread_id=?` in (active, complete), wait <= 90 s.
  If the folder is untrusted Codex shows "Trust this folder?"; the launcher fails loud with the fix
  (`install/codex.sh --trust <dir>`), it never presses Enter for you.
- opencode: pick a free port (python3 socket bind 0), `opencode --port <p> [-m <m>] --prompt "$(cat <txt>)" <dir>`
  (resume: `opencode --port <p> -s <id> <dir>`). Session id: poll `GET http://127.0.0.1:<p>/session` (<= 60 s) for a
  session whose `directory` is the real path of dir, newest `time.created`. Goal proof (goal mode): that session's
  `.metadata.goal.state == "active"` or its title starts with `▶`, wait <= 90 s.
- kiro: `kiro-cli chat --agent-engine v3 --trust-all-tools --trust-workspace [--agent fleet-<role>] [--model <m>] "$(cat <txt>)"`
  (resume: `... --resume-id sess_<uuid>`, no prompt). Goal mode: the first prompt's leading `/goal ` becomes
  `/goal --max $FLEET_KIRO_GOAL_MAX ` (default 20; the `/goal ` prefix check runs on the original text, the length check on the rewritten text). Session id: poll (<= 60 s)
  `$KIRO_HOME/session-leases/<sid>.lease` (JSON with `pid`) for a pid that is a descendant of the pane pid (pane shell ->
  kiro-cli -> bun -> node, not the direct child); goal workflow child sessions (session.json `rootConversationId` set,
  `agentMode` `wf-*`) hold their own lease and are skipped. Goal proof: a `_kiro/workflow/run_start` line for workflow
  `goal` in the parent `$KIRO_HOME/sessions/*/<sid>/messages.jsonl`, wait <= 90 s. No `--name`: the roster maps name to
  session id. If the trust-all confirmation (default "No, exit") is on screen the launcher fails loud (run
  `install/kiro.sh`, then `fleet stop <name>` and relaunch). PROVEN on kiro-cli 3.5.0.
- agy: `agy --dangerously-skip-permissions --settings $FLEET_STATE/launch/<name>.agy-settings.json --log-file
  $FLEET_STATE/log/<name>.agy.log [--agent fleet-<role>] [--model <m>] -i "$(cat <txt>)"` (resume: `... --conversation <uuid>`,
  no prompt; a non-uuid resume id is refused). The settings file is generated per launch: `toolPermission` always-proceed,
  `trustedWorkspaces` [dir], merged with `roles/<role>.agy.json` (role keys win). The log is truncated before each start.
  Goal mode passes no `-i` (agy sends an `-i` prompt as plain text, never as a slash command: PROVEN); a background
  helper in the window waits <= 120 s for the idle footer `? for shortcuts`, types the `/goal ...` line with
  `send-keys -l`, waits 1 s, then sends Enter. Session id: poll (<= 60 s) the log for `Created conversation <uuid>`
  (resume: `found conversation <uuid>` naming the wanted id, then wait for `? for shortcuts` so a wake typed right after
  is not lost). Proof: a `USER_INPUT` row answered by a `PLANNER_RESPONSE` in
  `$FLEET_AGY_HOME/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript_full.jsonl`; goal mode also waits for
  a `USER_INPUT` whose metadata contains `/goal is a [Slash Command]`. Fails loud on agy's workspace-trust dialog,
  first-run screens, or a sign-in wait. Log, transcript and slash expansion PROVEN on agy 1.5.13 by hand; the launcher's
  own proof is stub-tested and PROVEN by the kit's live proof run (agy 1.5.13) for prompt and goal mode.
- Every successful launch writes the roster entry and prints exactly one line starting `OK <name> cli=<cli> mode=<mode> pane=<id>`
  plus `session=<id>` and `goal=active|n/a`. Failure prints `FAIL <reason>` to stderr and exits 1.
- `--dry-run` prints the window script, the first prompt and the tmux command, touches nothing (no tmux, no state).
- `--no-proof` skips the pid/goal waits (used by tests with stub CLIs) but still writes the roster.

## DECISIONS UI (lib/fleet_decisions.py; `fleet decisions ...`)

```
fleet decisions serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "DECISIONS UI: http://127.0.0.1:P/?t=TOKEN"
fleet decisions tui [--one] [--plain]                              --plain reads keys from stdin (one per line; "o <text>" = own answer)
fleet decisions pick <id> <letter|--text T|--edit T|--refuse [why]>   exit 0 relayed, 3 already answered, 4 role refused, 1 error
fleet decisions retry <id> | snooze <id> [minutes] | state [--json] | find "<text>" | report <id> ok|fail [note]
fleet decisions ask "<q>" [--options a,b] [--allow ...] [--wait] [--timeout 30m] [--new]   exit 0 picked, 1 refused/own/changes, 2 no answer
fleet decisions refresh [--json] | refresh choose <id> A|B | refresh confirm <id>   confirm needs a prior "choose B"
```
- A call = a `decisions.jsonl` row and/or a desk goal `openForHuman[]` item with the same id; open only while every store
  that has it says `open`. Open calls with the same normalized question (`fleet_mesh._norm`) form one card: the oldest id
  is primary, the rest are `dupIds`; askers = each id's `from` + `relayTo`, unique, in order. An id whose two stores
  hold different questions (normalized, neither containing the other) is not one call: the card carries `conflict`,
  offers no answer buttons, and `pick` refuses it (exit 1, "ambiguous"); answer it in the desk pane.
- Options come from `decision_open.options` or, for goal-file items, inline `(a, rec) x (b) y` markers. `(rec)` / `(a, rec)` /
  `[rec]` marks rec; `(downside: ...)` is split off. Rec options sort first; letters are assigned after sorting.
- `pick`, under `$FLEET_STATE/.decisions-pick.lock`: `fleet_mesh.mark_decision(id, verbatim, only_open=True)` for every
  mesh id of the card (the commit point; raises `AlreadyAnswered`), then `deliver()` "[answer <id>] <verbatim>" to each
  asker, plus "[DECISIONS UI copy] ..." to `$FLEET_ROUTE` when `copyOrchestrator` and it is not an asker, then the
  goal file through `desk.sh answer <id> <verbatim> <askers...>` with `FLEET_DECISION_EXTRA` (a JSON object merged
  into the log line). Ids absent from the goal file get the log line written directly. Ids with no `decisions.jsonl`
  row (goal-file-only calls) are first recorded in `decisions-ui-state.json` `closed`, so a failed `desk.sh answer`
  (reported as a `WARN` line, with the log line still written) cannot reopen the call for a second relay. `pick` and
  `snooze` enforce the card's `allow` (pick, own, edit, refuse, snooze) and refuse the rest (exit 1). One log line per id per pick:
  `{id, at, asker, verbatim, relayedTo, via:"ui", frontend, kind, copiedTo, delivery:{target: deliver() result}}`.
  Option picks record verbatim `"<letter>: <option text>"`.
- Badge = worst of the delivery results: `FAILED` anywhere -> failed, `queued` -> queued, `reopened` -> reopened,
  `delivered` -> relayed. `retry` re-wakes failed/queued targets (`fleet_mesh.wake`, reopening parked ones) without a
  second inbox line, and appends a new log line with `retry: true` and the new results.
- Log rows without an `id` (the WATCH UI's `kind: "decided-on-call"` rows) belong to no call: the UI, `find` and refresh
  skip them.
- `decisions-reports.jsonl`: `{id, at, from, ok, note}` from `decision_report`; the latest per id sets `acted`.
- `decisions-ui-state.json`: `{snoozed: {id: untilISO}, closed: {id: verbatim}}`. `decisions-refresh.json`: `{at, request, rows:[{id, from,
  question, b:{kind, quote, text, at, source}, choice, state}], ungrounded, errors}`.
- `decisions-ui.json` (config): `{port: 7340, notify, notifyCommand: [argv with {title} {body} {url} {id}],
  copyOrchestrator: true, refreshSources: [{kind: jsonl|file|command|candidates, path|run, label}]}`.
- HTTP: `GET /`, `/refresh`, `/api/state`, `/api/refresh`, `/events` (SSE, `event: state`) need `?t=` or `X-Fleet-Token`;
  `POST /api/pick {id, option|text, kind?, refuse?}`, `/api/retry {id}`, `/api/snooze {id, minutes}`, `/api/refresh`,
  `/api/refresh/choose {id, choice}`, `/api/refresh/confirm {id}` need the `X-Fleet-Token` header (a `?t=` query is not
  enough). Wrong or missing token, a Host other than `127.0.0.1:P` / `localhost:P` / `[::1]:P`, or a foreign `Origin` ->
  403. Already answered -> 409.
- Role guard: `serve`, `tui`, `pick`, `retry`, `snooze` and refresh run only with `FLEET_ROLE` unset or `desk`. It reads
  the environment, so it stops a lane that runs the command by mistake, not one that unsets its own role on purpose.
- `serve` on a busy port exits 1 with one `can't listen on ...` line. The curses TUI sends stderr (mesh delivery log
  lines) to `$FLEET_STATE/decisions-tui.log` while it runs, and its text prompts block until Enter.

## lib/fleet_mesh.py

```
python3 lib/fleet_mesh.py serve                 stdio MCP server (JSON-RPC 2.0, newline-delimited, protocol 2025-06-18)
python3 lib/fleet_mesh.py send <from> <to> <text>    CLI delivery (same code path as peer_send)
python3 lib/fleet_mesh.py inbox <name>          print unread, advance cursor
python3 lib/fleet_mesh.py roster                print roster.json
python3 lib/fleet_mesh.py mirror                PostToolUse hook: copy a native SendMessage into the mesh (stdin = hook JSON, exit 0)
```
MCP tools: `peer_list {}`, `peer_send {to, text, from?}`, `peer_inbox {name?, all?}`,
`decision_open {question, options?: [str], from?}` (appends decisions.jsonl and delivers to $FLEET_DECIDE),
`decision_list {status?}`, `decision_answer {id, answer}` (marks answered, delivers to the asker only; see Messaging routing),
`decision_report {id, ok, note?}`, `decision_find {text}` (see DECISIONS UI). `decision_open` also takes `allow?: [pick|edit|own|snooze|refuse]`.
Sender = `from` arg, else `$FLEET_NAME`, else error.
Delivery (`deliver(frm, to, text)`): always append to inbox/<to>.jsonl, then wake by the target's roster `cli`:
- codex: `$FLEET_CODEX_BIN queue --thread <to> --message "[fleet message from <frm>] <text>"` (PROVEN wakes an idle TUI)
- opencode: `POST http://127.0.0.1:<port>/session/<session_id>/prompt_async` JSON `{"parts":[{"type":"text","text":"[fleet message from <frm>] <text>"}]}` (PROVEN, 204)
- claude: `tmux [-L sock] send-keys -t <pane> -l "[fleet] message from <frm>: run peer_inbox to read it"` then `send-keys -t <pane> Enter`
  (never the message body itself: typed input is treated as the human's). PROVEN on Claude 2.1.330. Not the native socket:
  see Messaging model.
- kiro: same typed pointer line and `Enter` as claude (PROVEN: starts a turn on an idle lane; the lane then runs `peer_inbox`)
- agy: same typed pointer line and `Enter` as claude (PROVEN typed text + Enter starts a turn on an idle agy 1.5.13 TUI;
  the woken lane then runs `peer_inbox`: PROVEN by the kit's live proof run (agy 1.5.13)). No pane in the roster: inbox only and the result says the wake failed.
  Before typing, delivery reads the agy pane (`capture-pane -p`) and refuses to type when it shows agy's trust dialog,
  color-scheme or Terms of Service onboarding, or a sign-in wait (its Enter would answer that screen for the human; the
  Terms of Service screen has a data-sharing opt-in pre-checked), or when the pane can't be read. The message stays in
  the inbox and the result says the wake FAILED.
- target missing from roster: inbox only, result says `queued (no live lane)`.
Result text names the wake used and whether it succeeded; a failed wake still leaves the message in the inbox.

## bin/fleet-goal

```
fleet-goal new <goal.json> --goal "<text>" [--report-to NAME]   skeleton with goal, owner, reportTo, phases[], gates{}, mustNot[], nextAction, done[], openForHuman[], decisions[], changelog[]
fleet-goal condition <goal.json> [--report-to NAME] [--turns N]  prints a /goal condition (<= 4000 chars) that passes the fleet launch lint
fleet-goal check <goal.json>     first line `OK` or `FAIL <path>: <why>`; exit 0/1
```
Desk helper `skills/running-the-decisions-desk/desk.sh` uses `fleet-goal check` and field `openForHuman`.

## v2 addendum (roles, lifecycle, routing)

### Roles
- `fleet launch --role route|decide|watch|learn|worker` (default `worker`; `desk` mode defaults to role `decide`).
  `--role orchestrator|desk|listener` exits 1 with `FAIL role <old> is renamed <new>` in every mode, `resume`
  included, so `fleet reopen` of a lane whose roster role is an old name is refused; `probe` is refused as reserved.
  The window script exports `FLEET_ROLE=<role>`; the roster entry gets `"role"`.
- Files: `roles/<role>.md` (role context), `roles/<role>.claude.json` (a Claude `--settings` file with
  `permissions.deny` only: allow rules do nothing in bypass mode), `roles/<role>.codex.toml` (Codex profile body:
  `developer_instructions` from the role file, `[mcp_servers.fleet] enabled_tools` list),
  `roles/<role>.opencode.json` (an OpenCode v1 agent object: `prompt`, `tools`, `permission`),
  `roles/<role>.kiro.json` (a Kiro V3 agent: `tools`, `includeMcpJson`, `permissions.rules` with capability `shell`, effect `deny`),
  `roles/<role>.agy.json` (only `permissions.deny`, merged into the lane's `--settings`) and `roles/<role>.agy.md`
  (agent frontmatter: `name`, `description`, `mainAgent: true`, `inheritMcp: true`, `tools`; the installer appends roles/<role>.md as the body).
- Launch wiring: claude `--settings <repo>/roles/<role>.claude.json --append-system-prompt-file <repo>/roles/<role>.md`;
  codex `-p fleet-<role>` (installer writes `$CODEX_HOME/fleet-<role>.config.toml`); opencode `--agent fleet-<role>`
  (installer adds `agent.fleet-<role>` to opencode.json); kiro `--agent fleet-<role>` (installer writes
  `$KIRO_HOME/agents/fleet-<role>.json`, prompt = inline text of roles/<role>.md because a `file://` prompt path is rejected
  at runtime and Kiro falls back to the default agent, dropping the denies; PROVEN); agy `--agent fleet-<role>` (installer writes
  `$FLEET_AGY_HOME/config/agents/fleet-<role>.md`) plus the per-lane `--settings` carrying the role's deny rules. If the profile/agent is not installed the launcher
  warns once and launches without it (agy keeps its deny rules then).
- Deny lists: route denies Edit, Write, NotebookEdit, `Bash(git commit:*)`, `Bash(git push:*)`;
  decide, watch and learn deny Edit, Write, NotebookEdit, `Bash(fleet launch:*)`, `Bash(fleet park:*)`,
  `Bash(fleet stop:*)`, `Bash(fleet reopen:*)`, `Bash(git commit:*)`, `Bash(git push:*)`; worker denies `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`,
  `Bash(fleet reopen:*)`. Every role denies AskUserQuestion. Kiro has the same fleet/git denies as `shell` match globs
  (`fleet park*`); PROVEN enforced under trust-all but `sh -c "fleet park X"` is not caught; no AskUserQuestion equivalent (UNVERIFIED).
  agy: worker and decide/watch/learn deny `command(fleet launch)`, `command(fleet park)`, `command(fleet stop)`, `command(fleet reopen)`;
  route and decide/watch/learn deny `write_file(*)`, `command(git commit)`, `command(git push)` and their agent files list no
  write or edit tools. PROVEN a `command(fleet stop)` deny blocked `fleet stop nothing` under `--dangerously-skip-permissions`; whether `write_file(*)` also
  covers edit tools and artifacts, and whether `sh -c` wrappers are caught: UNVERIFIED.
- Role guard: `fleet launch|park|stop|reopen|cleanup --apply` refuse with `FAIL role <r> can't <verb>` when
  `FLEET_ROLE` is set and is not `route` (also `fleet desks`; `orchestrator` is refused like any other role). Unset `FLEET_ROLE` (the human's own shell) is allowed.

### fleet desks up (bin/fleet-desks)
- `fleet desks up [--cli C] [--model M] [--dir D] [--no-proof] [--dry-run]`, role-guarded like launch.
  Builds two split windows, all four desks in desk mode: `deskA` = ROUTE (role route, left) | DECIDE (role decide,
  split right of ROUTE), `deskB` = WATCH (role watch, left) | LEARN (role learn, split right of WATCH). No separate
  WATCH or LEARN window. Every desk pane is tagged `@fleet-desk <role>`. WATCH and LEARN get a read-only "look for
  work" first prompt. Drops the `home` window when it created the session. `--dry-run` prints
  `window deskA: ROUTE (left) | DECIDE (right)` and `window deskB: WATCH (left) | LEARN (right)` before the launches it
  would run. Last line: `OK desks up: deskA ROUTE=%N | DECIDE=%N, deskB WATCH=%N | LEARN=%N`.
- Idempotent: a desk whose roster pane is live is left alone (`<NAME> already live (pane %N)`); when all four are live
  nothing changes (no launch, rename or tag). A dead right desk is relaunched split right of the live left one; a dead
  left desk is split beside the live right one and swapped to the left. The window is renamed `deskA`/`deskB` only
  when something in it was launched.
- Beside a running fleet: other lanes, including pre-rename ORCHESTRATOR and DECISIONS sessions, keep their windows,
  panes, roster entries and inboxes; nothing is typed into them and no alias map is written.

### Lifecycle (bin/fleet-lifecycle; bin/fleet dispatches `tick|park|reopen|cleanup` to it)
- Roster additions: `status` (`live|parked|done|archived`, default live), `parkedAt`, `goalState`
  (`active|stopped|achieved|none`), `goalStoppedAt`, `lastActive` (epoch secs), `summary`, `openItems`, `resume`.
- Goal state per CLI: claude = transcript jsonl records `{"type":"attachment","attachment":{"type":"goal_status","met":bool,...}}`
  (PROVEN on 2.1.327): last one `met:true` -> achieved; a `Goal set:` line with no later met:true -> active, unless a
  later `Goal cleared` / `Goal paused` text appears -> stopped (those two strings are from the docs, UNVERIFIED in jsonl); codex = `thread_goals.status` (active -> active; complete -> achieved; else stopped);
  opencode = `GET /session` `.metadata.goal.state` (active -> active; complete|achieved -> achieved; else stopped);
  kiro = the `goal` workflow markers in messages.jsonl (`_kiro/workflow/run_start` -> active; `run_complete` with status
  `completed` -> achieved; any other status -> stopped; a goal stopped at max iterations: UNVERIFIED, never observed);
  agy = `transcript_full.jsonl` rows: a `USER_INPUT` with `/goal is a [Slash Command]` metadata -> active; a later
  `PLANNER_RESPONSE` containing `<!-- GOAL_COMPLETE -->` -> achieved, `<!-- GOAL_CANCELLED -->` -> stopped (since = that
  row's `created_at`). Only PLANNER_RESPONSE rows count: the USER_INPUT metadata quotes both markers (PROVEN 1.5.13).
  No goal ever set -> `none`.
- `fleet tick [--json]`: read-only. Prints one row per lane: NAME CLI ROLE TIER LAST-ACTIVE GOAL UNREAD ACTION,
  where ACTION is `park` when: role is worker (never a desk, nor a roster entry with a retired role) AND (goal achieved OR goal stopped
  > 600 s) AND no unread inbox line younger than 3600 s. Goal `none` workers: ACTION `orchestrator-decides`.
  After the table (text mode only; `--json` is unchanged) tick prints `chrome tabs: N (agent A, other B)`: N = targets
  of `type` `page` from a read-only `GET $FLEET_CDP_URL/json/list` (default `http://127.0.0.1:9222`, 1.5 s timeout);
  A = pages with a cdp-toolkit origin record `origin-chrome-<targetId>.json` in `$CDP_ARTIFACT_DIR` (default
  `/tmp/cdp-toolkit`), B = the rest. No such directory: just `chrome tabs: N`. N > `FLEET_TAB_WARN` (default 50) adds
  `WARN chrome tabs N > T (FLEET_TAB_WARN): close tabs you opened`. Chrome unreachable or bad JSON: `chrome tabs:
  unavailable`, exit status unchanged. Never opens or closes a tab.
- `fleet park NAME [--no-compact]`: first types `/compact` and waits <= FLEET_COMPACT_WAIT (600 s) for the CLI's
  finished-compaction record (claude `compact_boundary`, codex `compacted`, opencode completed `summary` message, kiro `tombstone` line with `kind` `summarization` in messages.jsonl, agy a
  `SYSTEM` `CHECKPOINT` row starting `# Resuming from a compaction` in transcript_full.jsonl, PROVEN written by `/compact` on 1.5.13),
  refusing to park when it never appears or the transcript is unreadable; roster gets `compactedAt`. Then it stops the CLI cleanly (claude/codex: send `C-c` twice then wait <= 20 s for the pid to exit,
  then `kill`; opencode, kiro and agy: same; Kiro exits ~1 s, PROVEN; agy exits on the double `C-c`, PROVEN), `tmux kill-window`/`kill-pane`, sets status parked, parkedAt, resume command.
- `fleet reopen NAME`: for parked/done entries: default resume the same id
  (claude `--resume <id>` with the same name; codex `codex resume <id>`; opencode `-s <id>`; kiro `kiro-cli chat --agent-engine v3 --trust-all-tools --trust-workspace --resume-id <id>`; agy `agy --dangerously-skip-permissions --conversation <uuid>`, PROVEN it resumes the conversation) via `fleet launch ... resume`.
  `--fresh` instead starts a new lane from `$FLEET_STATE/handoff/NAME.md`; if that file is missing, generate it first
  with `fleet handoff NAME` from the transcript (claude jsonl, codex rollout jsonl, opencode `GET /session/<id>/message`
  or `opencode export <id>` if the server is gone, kiro messages.jsonl, agy transcript_full.jsonl, else transcript.jsonl): last user goal, last 20 assistant texts, files touched.
- `fleet cleanup [--apply]`: dry run by default. Reports: (1) goal-less workers quiet > 2 h, (2) tmux windows in the
  fleet session with no roster lane, (3) roster live entries whose pane or pid is gone (-> mark parked),
  (4) claude/codex/opencode/kiro/agy processes whose argv names a roster lane but no pane. `--apply` parks (1), marks (3),
  kills (2) and (4) only with `--kill-orphans`. Copy the roster to `$FLEET_STATE/archive/roster-<ts>.json` before any apply.

### Messaging routing
- `fleet_mesh.deliver` to a roster entry with status parked: run `fleet reopen <to>` (subprocess, timeout 180 s),
  then deliver. Result text says `reopened`.
- Shared decisions log `$FLEET_STATE/decisions-log.jsonl`, append-only: `{"id","at","asker","verbatim","relayedTo"}`.
  `decision_answer` appends to it and delivers only to the asker (no ROUTE message). desk.sh `answer` appends too.
- Installers: codex.sh writes `$CODEX_HOME/fleet-<role>.config.toml` from roles/*.codex.toml; opencode.sh merges
  roles/*.opencode.json into `agent.fleet-<role>` and lists the goal and compact plugins in `plugin`
  (goal as `[ref, {"compactAtTokens": 0}]`, compact as `[ref, {"limit": N}]`, N from `FLEET_OC_COMPACT_LIMIT`, default
  200000: one compactor per lane, and the option beats `OPENCODE_COMPACT_AT_TOKENS`; tui.json gets only the goal plugin); kiro.sh writes
  `$KIRO_HOME/agents/fleet-<role>.json` from roles/*.kiro.json, `settings/mcp.json`, `settings/cli.json`
  (`chat.disableTrustAllConfirmation` true) and `steering/fleet-rules.md`; agy.sh writes `$FLEET_AGY_HOME/config/agents/fleet-<role>.md`
  from roles/*.agy.md + roles/<role>.md, `config/rules/fleet-rules.md` (`trigger: always_on`), `config/mcp_config.json`
  (`agy mcp add fleet python3 <repo>/lib/fleet_mesh.py serve`, or jq when `FLEET_AGY_HOME` is not `$HOME/.gemini` or agy is missing),
  `config/skills/<name>/`, and with `--trust DIR` the `antigravity-cli/settings.json` `trustedWorkspaces`; claude.sh needs nothing (roles are passed per launch).

## Messaging model

**Mesh is the mailbox, native is the doorbell.** `$FLEET_STATE/inbox/<lane>.jsonl` (lib/fleet_mesh.py) is the single
source of truth for every message to every lane. A native channel only wakes a lane, and a wake carries a pointer
("run peer_inbox"), never the body, except where the CLI's own queue is the wake (codex, opencode).

Claude lanes get the mesh per launch: `fleet launch --cli claude` adds
`--mcp-config '{"mcpServers":{"fleet":{"command":"python3","args":["<repo>/lib/fleet_mesh.py","serve"]}}}'` and exports
`FLEET_MESH=<repo>/lib/fleet_mesh.py`. Nothing is written to the user's Claude config (`install/claude.sh --mesh` stays optional;
having both a user-scope `fleet` server and `--mcp-config` is UNVERIFIED). The launcher argv is stub-tested; the same
flags were PROVEN on hand-launched Claude 2.1.330 lanes (a real Claude through `fleet launch`: UNVERIFIED).

| Target cli | Wake used by `deliver()` | Status |
|---|---|---|
| claude | typed pointer line + Enter (`tmux send-keys`) | PROVEN 2.1.330 (hand-launched lane, launcher flags): idle bypass lane ran `peer_inbox`, cursor advanced |
| kiro | typed pointer line + Enter | PROVEN kiro-cli 3.5.0 |
| agy | typed pointer line (`send-keys -l`) + Enter | PROVEN agy 1.5.13 that typed text + Enter starts a turn; the full `peer_inbox` round trip through `fleet launch`: PROVEN by the kit's live proof run (agy 1.5.13) |
| codex | `codex queue --thread` (body included) | PROVEN |
| opencode | `POST /session/<id>/prompt_async` (body included) | PROVEN |

Claude's native socket (`messagingSocketPath` in `~/.claude/sessions/<pid>.json`, e.g. `/tmp/cc-socks/<pid>.sock`)
is NOT used. Measured on 2.1.330 with an honest plain frame `{"type":"user","message":{"role":"user","content":"..."}}`
written by python3:

| Receiver permission mode | Result |
|---|---|
| bypass (every fleet lane) | HELD: session status goes `waiting`, a "Held message from another session" modal opens, nothing reaches Claude. The sender gets no reply on the socket |
| default (`--permission-mode default`) | delivered: status `busy` within 0.3 s and the lane answered |

The hold cause is `no-mode-asserted`: the receiver bypasses prompts and the sender did not attest a permission mode.
A non-Claude sender has no mode, and faking one in the message wrapper would forge the field Claude uses for that
safety gate, so the kit does not. A held message also breaks the typed fallback: a pointer typed while the modal is up
is consumed by the modal (Deny) and the lane stays asleep (observed). Hence the socket is never tried first.
The socket would only help a lane that is not bypass, or one whose user set `"crossSessionInbound": "accept"`; fleet
launches neither.

**Mirror rule.** A message one fleet lane sends another natively must also land in the mesh inbox. Mechanism: every
`roles/<role>.claude.json` has a `PostToolUse` hook, matcher `SendMessage`, running
`python3 "$FLEET_MESH" mirror` (exit 0 always). `mirror` reads the hook JSON (`tool_input.to`, `tool_input.message`),
maps `to` to a roster lane (a lane name, `name [ref]`, or `uds:<socket>` matched through
`~/.claude/sessions/<roster pid>.json` whose `sessionId` equals the roster `session_id`), and appends
`{id, at, from: $FLEET_NAME, to, text, via: "native"}` to `inbox/<to>.jsonl`. No wake and no reopen: the native
message already reached the lane. Skipped: no `FLEET_NAME`, target not on the roster, target is the sender.
`peer_inbox` shows these as `from X (native copy)`. Until the receiver reads them they count as unread
(`peer_list`, `fleet tick`). Lanes launched before this change get the hook and the MCP on their next relaunch or reopen. PROVEN on two live bypass lanes: `SendMessage to="LANEB"` and
`to="uds:/tmp/cc-socks/<pid>.sock"` both produced `via: "native"` lines, and LANEB also got the message natively.

**A2A: deferred.** A2A (agent cards, tasks, HTTP transport) is built for networked agents from different vendors
finding and calling each other. Fleet lanes are tmux panes on one Mac that already share a filesystem mailbox, so A2A
adds discovery and transport they do not need. If a remote or third-party agent ever has to join, the path is an A2A
gateway lane that turns incoming A2A tasks into `peer_send` and replies from `peer_inbox`, leaving the mesh unchanged.

## WATCH UI (lib/fleet_listen.py)

`fleet listen ...` execs `python3 lib/fleet_listen.py ...` (stdlib only). Flags: `--source file|stdin|command|replay`
`[ARG]`, `--speakers "A,B"`, `--config FILE`, `--speed N`, `--poll SEC`, `--title T`, `--name NAME` (default
`WATCH`), `--page`, `--port N`, `--pane-right-of NAME`, `--here`, `--plain`, `--exit-at-eof`. There is no host flag:
the page binds 127.0.0.1. Without `--here`/`--plain` (and not `stdin`) it opens a tmux pane (split right of the roster
pane of NAME, else a window in `$FLEET_TMUX_SESSION`, creating the session if needed), prints
`OK listener <name> pane=%N`, and runs itself there with `--here`, `FLEET_NAME=<name>`, `FLEET_ROLE=watch`. It does
not write the roster; it sets the tmux pane option `@fleet-desk watch`, and `fleet cleanup` skips windows holding a pane
with a non-empty `@fleet-desk` (the retired `@fleet-listener 1` tag is no longer skipped). SIGTERM and SIGHUP (kill-pane) flush the batch and stop the source like `q`. The speaker filter applies to what
is drawn and served (at draw time) as well as to routing.

- Input line: JSON `{"at": ISO-8601, "speaker", "text", "final"}`; aliases `completed` for `final`, `start`/`end`
  seconds or `clock` for `at`, `name`/`who` for `speaker`; missing `final` = true. Or text `[hh:mm:ss] Speaker: text`.
  Unparseable lines are skipped.
- Config `$FLEET_STATE/listener.json` keys: `source{kind,path|run,speed}`, `speakers[]`, `me`, `speakerAliases{}`,
  `route{actions}` (default `$FLEET_ROUTE`; asks always go to `$FLEET_DECIDE` via `decision_open`), `classifier{kind: rules|command, run,
  timeoutSec 10, maxTokens 300, minIntervalSec 5, tailChars 1800, rulesFile}`, `facts[{match[],say,source}]`,
  `factFiles[]` (globs), `glossary{}`, `watchers[{name,trigger,run,routeTo,timeoutSec}]`, `batchSec` (5), `title`.
- Classifier/watcher contract: stdin one JSON `{"utterances":[{clock,speaker,text}],"utterance","tail","speakers",
  "me","maxTokens","labels"}` (+ `"watcher"` for watchers); stdout lines `KIND: text [| key: value]... [| uncertain]`,
  KIND in `ASK ACTION SAY DECIDED NOTE`; anything else is counted as ignored. Non-zero exit or timeout = status-bar
  error and nothing from that run is routed; on timeout the process group is killed; the pane keeps running.
- Writes: ASK -> `decision_open` (`from` = the `--name` value, default `WATCH`; question `Heard on the call: <speaker> asked "<quote>"
  (<clock>). ...`; options from the item or `yes | no | not now`). ACTION / DECIDED / uncertain -> one
  `deliver(<name>, route.actions, text)` per batch window, lines `ACTION "<quote>" (<speaker>, <clock>) owner: <x>[, due: <y>]`,
  `DECIDED "<quote>" (...) decided-on-call; ...`, `<KIND>, marked uncertain: "<quote>" (...)`. DECIDED also appends
  `{"id": null, "at", "asker": <speaker>, "verbatim": <quote>, "relayedTo": [<route.actions>], "kind":
  "decided-on-call", "confirmed": false, "from", "clock"}` to `decisions-log.jsonl`. Nothing else is written.
- Never routed: non-final lines, speakers outside the filter (the `me` speaker is always classified, but its own ASKs
  are dropped), items whose quote is not a substring of a final transcript line (whitespace and case folded),
  repeats of a routed `(kind, quote)`, and any line or item matching the secret patterns (provider key prefixes,
  private key headers, JWTs, `password|token|api key is|:|= <value>`, long mixed-case-and-digit strings).
- Page routes: `/` (HTML), `/state.json` (`say`, `routed`, `marks`, `explainer`, `counts`, last 50 `lines`),
  `/lines.json?before=N&limit=M` (load more).

