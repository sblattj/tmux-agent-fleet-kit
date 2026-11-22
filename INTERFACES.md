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
| FLEET_KIRO_BIN | `kiro-cli` | Kiro binary (tests point it at a stub) |
| KIRO_HOME | `~/.kiro` | Kiro's global dir (sessions, leases, agents, settings, steering); passed to kiro lanes when set; read by install/kiro.sh and bin/fleet-lifecycle |
| FLEET_KIRO_GOAL_MAX | `20` | kiro goal mode rewrites `/goal ` to `/goal --max N `; positive integer (Kiro's own default is 5, DOC) |

## State layout ($FLEET_STATE)

```
roster.json            {"<name>": {"name","cli","mode","pane","session_id","port","dir","model","pid","at"}}
                       cli: claude|codex|opencode|kiro ; pane: tmux pane id like "%12" ; port: int or null ;
                       session_id: claude uuid | codex thread uuid | opencode "ses_..." | kiro "sess_<uuid>" | null until known
inbox/<name>.jsonl     one JSON per line: {"id","at","from","to","text"}
inbox/<name>.cursor    integer: number of lines already returned by peer_inbox
decisions.jsonl        {"id":"Q<n>","at","from","question","options":[...],"status":"open|answered","answer","answeredAt"}
launch/<name>.sh       the window script ; launch/<name>.txt the first prompt ; launch/<name>.launch.json
log/fleet.log          one line per launch/exit/delivery
```
Writers update roster.json atomically (write temp file in the same dir, then `mv`).

## bin/fleet

```
fleet launch [--cli claude|codex|opencode|kiro] [--model M] [--dir D] [--split-right-of NAME] [--dry-run] [--no-proof]
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
- kiro: same typed pointer line and `Enter` as claude (PROVEN: starts a turn on an idle lane; the lane then runs `peer_inbox`)
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
  `roles/<role>.opencode.json` (an OpenCode v1 agent object: `prompt`, `tools`, `permission`),
  `roles/<role>.kiro.json` (a Kiro V3 agent: `tools`, `includeMcpJson`, `permissions.rules` with capability `shell`, effect `deny`).
- Launch wiring: claude `--settings <repo>/roles/<role>.claude.json --append-system-prompt-file <repo>/roles/<role>.md`;
  codex `-p fleet-<role>` (installer writes `$CODEX_HOME/fleet-<role>.config.toml`); opencode `--agent fleet-<role>`
  (installer adds `agent.fleet-<role>` to opencode.json); kiro `--agent fleet-<role>` (installer writes
  `$KIRO_HOME/agents/fleet-<role>.json`, prompt = inline text of roles/<role>.md because a `file://` prompt path is rejected
  at runtime and Kiro falls back to the default agent, dropping the denies; PROVEN). If the profile/agent is not installed the launcher
  warns once and launches without it.
- Deny lists: orchestrator denies Edit, Write, NotebookEdit, `Bash(git commit:*)`, `Bash(git push:*)`;
  desk denies Edit, Write, NotebookEdit, `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`,
  `Bash(fleet reopen:*)`, `Bash(git commit:*)`, `Bash(git push:*)`; listener denies Edit, Write, NotebookEdit,
  `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`, `Bash(fleet reopen:*)`, `Bash(git commit:*)`,
  `Bash(git push:*)`; worker denies `Bash(fleet launch:*)`, `Bash(fleet park:*)`, `Bash(fleet stop:*)`,
  `Bash(fleet reopen:*)`. Every role denies AskUserQuestion. Kiro has the same fleet/git denies as `shell` match globs
  (`fleet park*`); PROVEN enforced under trust-all but `sh -c "fleet park X"` is not caught; no AskUserQuestion equivalent (UNVERIFIED).
- Role guard: `fleet launch|park|stop|reopen|cleanup --apply` refuse with `FAIL role <r> can't <verb>` when
  `FLEET_ROLE` is set and is not `orchestrator`. Unset `FLEET_ROLE` (the human's own shell) is allowed.

### Lifecycle (bin/fleet-lifecycle; bin/fleet dispatches `tick|park|reopen|cleanup` to it)
- Roster additions: `status` (`live|parked|done|archived`, default live), `parkedAt`, `goalState`
  (`active|stopped|achieved|none`), `goalStoppedAt`, `lastActive` (epoch secs), `summary`, `openItems`, `resume`.
- Goal state per CLI: claude = transcript jsonl records `{"type":"attachment","attachment":{"type":"goal_status","met":bool,...}}`
  (PROVEN on 2.1.327): last one `met:true` -> achieved; a `Goal set:` line with no later met:true -> active, unless a
  later `Goal cleared` / `Goal paused` text appears -> stopped (those two strings are from the docs, UNVERIFIED in jsonl); codex = `thread_goals.status` (active -> active; complete -> achieved; else stopped);
  opencode = `GET /session` `.metadata.goal.state` (active -> active; complete|achieved -> achieved; else stopped);
  kiro = the `goal` workflow markers in messages.jsonl (`_kiro/workflow/run_start` -> active; `run_complete` with status
  `completed` -> achieved; any other status -> stopped; a goal stopped at max iterations: UNVERIFIED, never observed).
  No goal ever set -> `none`.
- `fleet tick [--json]`: read-only. Prints one row per lane: NAME CLI ROLE TIER LAST-ACTIVE GOAL UNREAD ACTION,
  where ACTION is `park` when: role is worker (never orchestrator/desk/listener) AND (goal achieved OR goal stopped
  > 600 s) AND no unread inbox line younger than 3600 s. Goal `none` workers: ACTION `orchestrator-decides`.
  After the table (text mode only; `--json` is unchanged) tick prints `chrome tabs: N (agent A, other B)`: N = targets
  of `type` `page` from a read-only `GET $FLEET_CDP_URL/json/list` (default `http://127.0.0.1:9222`, 1.5 s timeout);
  A = pages with a cdp-toolkit origin record `origin-chrome-<targetId>.json` in `$CDP_ARTIFACT_DIR` (default
  `/tmp/cdp-toolkit`), B = the rest. No such directory: just `chrome tabs: N`. N > `FLEET_TAB_WARN` (default 50) adds
  `WARN chrome tabs N > T (FLEET_TAB_WARN): close tabs you opened`. Chrome unreachable or bad JSON: `chrome tabs:
  unavailable`, exit status unchanged. Never opens or closes a tab.
- `fleet park NAME [--no-compact]`: first types `/compact` and waits <= FLEET_COMPACT_WAIT (600 s) for the CLI's
  finished-compaction record (claude `compact_boundary`, codex `compacted`, opencode completed `summary` message, kiro `tombstone` line with `kind` `summarization` in messages.jsonl),
  refusing to park when it never appears or the transcript is unreadable; roster gets `compactedAt`. Then it stops the CLI cleanly (claude/codex: send `C-c` twice then wait <= 20 s for the pid to exit,
  then `kill`; opencode and kiro: same; Kiro exits ~1 s, PROVEN), `tmux kill-window`/`kill-pane`, sets status parked, parkedAt, resume command.
- `fleet reopen NAME`: for parked/done entries: default resume the same id
  (claude `--resume <id>` with the same name; codex `codex resume <id>`; opencode `-s <id>`; kiro `kiro-cli chat --agent-engine v3 --trust-all-tools --trust-workspace --resume-id <id>`) via `fleet launch ... resume`.
  `--fresh` instead starts a new lane from `$FLEET_STATE/handoff/NAME.md`; if that file is missing, generate it first
  with `fleet handoff NAME` from the transcript (claude jsonl, codex rollout jsonl, opencode `GET /session/<id>/message`
  or `opencode export <id>` if the server is gone, kiro messages.jsonl): last user goal, last 20 assistant texts, files touched.
- `fleet cleanup [--apply]`: dry run by default. Reports: (1) goal-less workers quiet > 2 h, (2) tmux windows in the
  fleet session with no roster lane, (3) roster live entries whose pane or pid is gone (-> mark parked),
  (4) claude/codex/opencode/kiro processes whose argv names a roster lane but no pane. `--apply` parks (1), marks (3),
  kills (2) and (4) only with `--kill-orphans`. Copy the roster to `$FLEET_STATE/archive/roster-<ts>.json` before any apply.

### Messaging routing
- `fleet_mesh.deliver` to a roster entry with status parked: run `fleet reopen <to>` (subprocess, timeout 180 s),
  then deliver. Result text says `reopened`.
- Shared decisions log `$FLEET_STATE/decisions-log.jsonl`, append-only: `{"id","at","asker","verbatim","relayedTo"}`.
  `decision_answer` appends to it and delivers only to the asker (no orchestrator message). desk.sh `answer` appends too.
- Installers: codex.sh writes `$CODEX_HOME/fleet-<role>.config.toml` from roles/*.codex.toml; opencode.sh merges
  roles/*.opencode.json into `agent.fleet-<role>`; kiro.sh writes
  `$KIRO_HOME/agents/fleet-<role>.json` from roles/*.kiro.json, `settings/mcp.json`, `settings/cli.json`
  (`chat.disableTrustAllConfirmation` true) and `steering/fleet-rules.md`; claude.sh needs nothing (roles are passed per launch).
