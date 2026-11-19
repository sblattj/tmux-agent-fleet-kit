# Frozen interfaces (contract between bin/fleet, lib/fleet_mesh.py, bin/fleet-goal, installs)

Portability: every script is `#!/usr/bin/env bash` and must run on bash 3.2 (macOS /bin/bash) and bash 5 (Linux).
No associative arrays, no `mapfile`, no `${x,,}`, no GNU-only flags (`sed -i` without suffix, `date -d`, `grep -P`).
Python: 3.8+ stdlib only. Runtime deps: bash, tmux >= 3.0, jq, python3, and the CLI being launched.
No personal data anywhere: no person names, emails, home-directory paths, account names, employer names.
The human is `$FLEET_HUMAN` (default `the human`); desk/orchestrator names are `$FLEET_DESK` (default `DECISIONS`)
and `$FLEET_ORCHESTRATOR` (default `ORCHESTRATOR`).

## Environment (all optional)

| Var | Default | Meaning |
|---|---|---|
| FLEET_TMUX_SESSION | `fleet` | tmux session every window is created in (created detached if missing) |
| FLEET_TMUX_SOCKET | empty | if set, every tmux call gets `-L $FLEET_TMUX_SOCKET` (tests use a private server) |
| FLEET_STATE | `${XDG_STATE_HOME:-$HOME/.local/state}/fleet` | roster, inboxes, launch scripts |
| FLEET_DIR | `$PWD` | working dir for a launched lane (`--dir` overrides) |
| FLEET_HUMAN | `the human` | who answers DECISIONS calls |
| FLEET_DESK / FLEET_ORCHESTRATOR | `DECISIONS` / `ORCHESTRATOR` | role names |
| FLEET_NAME | set by the launcher inside each window | the lane's own name (fleet_mesh.py reads it as the sender) |
| FLEET_CLAUDE_BIN / FLEET_CODEX_BIN / FLEET_OPENCODE_BIN | `claude` / `codex` / `opencode` on PATH | binaries (tests point them at stubs) |

## State layout ($FLEET_STATE)

```
roster.json            {"<name>": {"name","cli","mode","pane","session_id","port","dir","model","pid","at"}}
                       cli: claude|codex|opencode ; pane: tmux pane id like "%12" ; port: int or null ;
                       session_id: claude uuid | codex thread uuid | opencode "ses_..." | null until known
inbox/<name>.jsonl     one JSON per line: {"id","at","from","to","text"}
inbox/<name>.cursor    integer: number of lines already returned by peer_inbox
decisions.jsonl        {"id":"Q<n>","at","from","question","options":[...],"status":"open|answered","answer","answeredAt"}
launch/<name>.sh       the window script ; launch/<name>.txt the first prompt ; launch/<name>.launch.json
log/fleet.log          one line per launch/exit/delivery
```
Writers update roster.json atomically (write temp file in the same dir, then `mv`).

## bin/fleet

```
fleet launch [--cli claude|codex|opencode] [--model M] [--dir D] [--split-right-of NAME] [--dry-run] [--no-proof]
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
- Every successful launch writes the roster entry and prints exactly one line starting `OK <name> cli=<cli> mode=<mode> pane=<id>`
  plus `session=<id>` and `goal=active|n/a`. Failure prints `FAIL <reason>` to stderr and exits 1.
- `--dry-run` prints the window script, the first prompt and the tmux command, touches nothing (no tmux, no state).
- `--no-proof` skips the pid/goal waits (used by tests with stub CLIs) but still writes the roster.

## lib/fleet_mesh.py

```
python3 lib/fleet_mesh.py serve                 stdio MCP server (JSON-RPC 2.0, newline-delimited, protocol 2025-06-18)
python3 lib/fleet_mesh.py send <from> <to> <text>    CLI delivery (same code path as peer_send)
python3 lib/fleet_mesh.py inbox <name>          print unread, advance cursor
python3 lib/fleet_mesh.py roster                print roster.json
```
MCP tools: `peer_list {}`, `peer_send {to, text, from?}`, `peer_inbox {name?, all?}`,
`decision_open {question, options?: [str], from?}` (appends decisions.jsonl and delivers to $FLEET_DESK),
`decision_list {status?}`, `decision_answer {id, answer}` (marks answered, delivers to the asker and $FLEET_ORCHESTRATOR).
Sender = `from` arg, else `$FLEET_NAME`, else error.
Delivery (`deliver(frm, to, text)`): always append to inbox/<to>.jsonl, then wake by the target's roster `cli`:
- codex: `$FLEET_CODEX_BIN queue --thread <to> --message "[fleet message from <frm>] <text>"` (PROVEN wakes an idle TUI)
- opencode: `POST http://127.0.0.1:<port>/session/<session_id>/prompt_async` JSON `{"parts":[{"type":"text","text":"[fleet message from <frm>] <text>"}]}` (PROVEN, 204)
- claude: `tmux [-L sock] send-keys -t <pane> -l "[fleet] message from <frm>: run peer_inbox to read it"` then `send-keys -t <pane> Enter`
  (never the message body itself: typed input is treated as the human's)
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
- `fleet launch --role orchestrator|desk|listener|worker` (default `worker`; `desk` mode defaults to role `desk`).
  The window script exports `FLEET_ROLE=<role>`; the roster entry gets `"role"`.
- Files: `roles/<role>.md` (role context), `roles/<role>.claude.json` (a Claude `--settings` file with
  `permissions.deny` only: allow rules do nothing in bypass mode), `roles/<role>.codex.toml` (Codex profile body:
  `developer_instructions` from the role file, `[mcp_servers.fleet] enabled_tools` list),
  `roles/<role>.opencode.json` (an OpenCode v1 agent object: `prompt`, `tools`, `permission`).
- Launch wiring: claude `--settings <repo>/roles/<role>.claude.json --append-system-prompt-file <repo>/roles/<role>.md`;
  codex `-p fleet-<role>` (installer writes `$CODEX_HOME/fleet-<role>.config.toml`); opencode `--agent fleet-<role>`
  (installer adds `agent.fleet-<role>` to opencode.json). If the profile/agent is not installed the launcher
  warns once and launches without it.
- Deny lists: orchestrator denies Edit, Write, NotebookEdit, `Bash(git commit:*)`, `Bash(git push:*)`;
  desk denies Edit, Write, NotebookEdit, `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`,
  `Bash(fleet reopen:*)`, `Bash(git commit:*)`, `Bash(git push:*)`; listener denies Edit, Write, NotebookEdit,
  `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`, `Bash(fleet reopen:*)`, `Bash(git commit:*)`,
  `Bash(git push:*)`; worker denies `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`,
  `Bash(fleet reopen:*)`. Every role denies AskUserQuestion.
- Role guard: `fleet launch|park|stop|reopen|cleanup --apply` refuse with `FAIL role <r> can't <verb>` when
  `FLEET_ROLE` is set and is not `orchestrator`. Unset `FLEET_ROLE` (the human's own shell) is allowed.

### Lifecycle (bin/fleet-lifecycle; bin/fleet dispatches `tick|park|reopen|cleanup` to it)
- Roster additions: `status` (`live|parked|done|archived`, default live), `parkedAt`, `goalState`
  (`active|stopped|achieved|none`), `goalStoppedAt`, `lastActive` (epoch secs), `summary`, `openItems`, `resume`.
- Goal state per CLI: claude = transcript jsonl records `{"type":"attachment","attachment":{"type":"goal_status","met":bool,...}}`
  (PROVEN on 2.1.327): last one `met:true` -> achieved; a `Goal set:` line with no later met:true -> active, unless a
  later `Goal cleared` / `Goal paused` text appears -> stopped (those two strings are from the docs, UNVERIFIED in jsonl); codex = `thread_goals.status` (active -> active; complete -> achieved; else stopped);
  opencode = `GET /session` `.metadata.goal.state` (active -> active; complete|achieved -> achieved; else stopped).
  No goal ever set -> `none`.
- `fleet tick [--json]`: read-only. Prints one row per lane: NAME CLI ROLE TIER LAST-ACTIVE GOAL UNREAD ACTION,
  where ACTION is `park` when: role is worker (never orchestrator/desk/listener) AND (goal achieved OR goal stopped
  > 600 s) AND no unread inbox line younger than 3600 s. Goal `none` workers: ACTION `orchestrator-decides`.
- `fleet park NAME [--no-compact]`: first types `/compact` and waits <= FLEET_COMPACT_WAIT (600 s) for the CLI's
  finished-compaction record (claude `compact_boundary`, codex `compacted`, opencode completed `summary` message),
  refusing to park when it never appears or the transcript is unreadable; roster gets `compactedAt`. Then it stops the CLI cleanly (claude/codex: send `C-c` twice then wait <= 20 s for the pid to exit,
  then `kill`; opencode: same), `tmux kill-window`/`kill-pane`, sets status parked, parkedAt, resume command.
- `fleet reopen NAME`: for parked/done entries: default resume the same id
  (claude `--resume <id>` with the same name; codex `codex resume <id>`; opencode `-s <id>`) via `fleet launch ... resume`.
  `--fresh` instead starts a new lane from `$FLEET_STATE/handoff/NAME.md`; if that file is missing, generate it first
  with `fleet handoff NAME` from the transcript (claude jsonl, codex rollout jsonl, opencode `GET /session/<id>/message`
  or `opencode export <id>` if the server is gone): last user goal, last 20 assistant texts, files touched.
- `fleet cleanup [--apply]`: dry run by default. Reports: (1) goal-less workers quiet > 2 h, (2) tmux windows in the
  fleet session with no roster lane, (3) roster live entries whose pane or pid is gone (-> mark parked),
  (4) claude/codex/opencode processes whose argv names a roster lane but no pane. `--apply` parks (1), marks (3),
  kills (2) and (4) only with `--kill-orphans`. Copy the roster to `$FLEET_STATE/archive/roster-<ts>.json` before any apply.

### Messaging routing
- `fleet_mesh.deliver` to a roster entry with status parked: run `fleet reopen <to>` (subprocess, timeout 180 s),
  then deliver. Result text says `reopened`.
- Shared decisions log `$FLEET_STATE/decisions-log.jsonl`, append-only: `{"id","at","asker","verbatim","relayedTo"}`.
  `decision_answer` appends to it and delivers only to the asker (no orchestrator message). desk.sh `answer` appends too.
- Installers: codex.sh writes `$CODEX_HOME/fleet-<role>.config.toml` from roles/*.codex.toml; opencode.sh merges
  roles/*.opencode.json into `agent.fleet-<role>`; claude.sh needs nothing (roles are passed per launch).
