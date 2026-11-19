# tmux-agent-fleet-kit

Run a small fleet of coding-agent sessions in tmux: an **ORCHESTRATOR** that routes work and never asks questions, a **DECISIONS** desk that is the only place the human answers, and **goal lanes** that each start inside a goal locked to a living goal file. Works with Claude Code, Codex CLI and OpenCode v1, alone or mixed.

Needs: bash 3.2+, tmux 3.0+, jq, python3 (stdlib only), and the CLI you launch. The outer terminal does not matter: the kit only talks to tmux.

## The layout

```
tmux session "fleet"   (attach from any terminal: fleet attach)
  window 0: ORCHESTRATOR      routes work, never asks the human
    right split: DECISIONS    the only place the human answers; relays answers back
  window: lane-a  (/goal)     status reports -> ORCHESTRATOR
  window: lane-b  (/goal)     questions      -> DECISIONS
  more lanes, each claude | codex | opencode
```

- `bin/fleet` launches and lists lanes (`fleet launch`, `fleet ls`, `fleet attach`, `fleet layout`, `fleet stop`).
- `bin/fleet-goal` creates, validates and turns a living goal file into a `/goal` condition.
- `lib/fleet_mesh.py` is a stdlib-only stdio MCP server (`peer_list`, `peer_send`, `peer_inbox`, `decision_open`, `decision_list`, `decision_answer`) for CLIs without native messaging.
- `rules/fleet-rules.md` is the operating rules block the installers put into each CLI's global instructions file.
- `skills/` has four Claude Code skills: running the decisions desk, launching a goal lane, checking a peer before relaying, retiring the fleet into handoffs.

## Quick start

Common first steps: clone the repo, then run the installer for your CLI. Every installer is idempotent, takes `--dry-run` (prints every change, writes nothing), `--prefix DIR` (default `$HOME/.local`; symlinks `bin/fleet` and `bin/fleet-goal` into `DIR/bin`, so put that on PATH) and `--trust DIR` where the CLI needs it. Placeholders in the rules come from `FLEET_HUMAN`, `FLEET_DESK` and `FLEET_ORCHESTRATOR`.

### Claude Code
```bash
install/claude.sh --trust ~/work/project          # skills, rules in ~/.claude/CLAUDE.md, bin symlinks
install/claude.sh --mesh                          # (roles need no install on Claude) only for mixed fleets: also registers fleet-mesh
fleet launch --cli claude ORCHESTRATOR prompt "You are ORCHESTRATOR. Follow the fleet rules."
fleet launch --cli claude --split-right-of ORCHESTRATOR DECISIONS desk
fleet-goal new /tmp/lane-a.goal.json --goal "..." --report-to ORCHESTRATOR
fleet-goal condition /tmp/lane-a.goal.json --report-to ORCHESTRATOR > /tmp/lane-a.cond.txt
fleet launch --cli claude lane-a goal /tmp/lane-a.cond.txt
```

### Codex CLI
```bash
install/codex.sh --trust ~/work/project           # rules in $CODEX_HOME/AGENTS.md (default ~/.codex), codex mcp add fleet, trust entry, role profiles
fleet launch --cli codex lane-b goal /tmp/lane-b.cond.txt
```

### OpenCode v1
```bash
install/opencode.sh --bypass                      # goal plugin clone, plugin + mcp + role agents in opencode.json/tui.json, rules
fleet launch --cli opencode lane-c goal /tmp/lane-c.cond.txt
```
`--bypass` sets `"permission": "allow"` so unattended lanes are never blocked on a prompt; leave it off if you want prompts. Plugin override env: `FLEET_OC_GOAL_PLUGIN_URL`, `FLEET_OC_GOAL_PLUGIN_REF` (default tag `v1.5.3`).

Try anything first with `fleet launch --dry-run ...` (prints the window script, first prompt and tmux command, touches nothing).

## Roles

`fleet launch --role orchestrator|desk|listener|worker` (default `worker`; `desk` mode defaults to `desk`). The role is exported as `FLEET_ROLE` in the lane's window and stored in the roster.

| Role | Does | Denied |
|---|---|---|
| orchestrator | owns launch, park, reopen, cleanup; routes work; reads the decisions log; never edits | Edit, Write, NotebookEdit, `git commit`, `git push` |
| desk | the only place the human answers; relays each answer to the asker and logs it | the orchestrator's denies plus `fleet launch`, `park`, `stop`, `reopen` |
| listener | watches and reports | same as desk |
| worker | runs one goal, reports to the orchestrator, asks the desk | `fleet launch`, `park`, `stop`, `reopen` |

Every role denies AskUserQuestion. Role files live in `roles/` (`<role>.md`, `<role>.claude.json`, `<role>.codex.toml`, `<role>.opencode.json`). Separately, `fleet launch|park|stop|reopen|cleanup --apply` refuse with `FAIL role <r> can't <verb>` when `FLEET_ROLE` is set to anything but `orchestrator`; a human shell with `FLEET_ROLE` unset is allowed.

### How each CLI enforces a role

Tags: **PROVEN** = run locally; **DOC** = vendor docs; **UNVERIFIED** = not proven on the versions below.

| | Claude Code | Codex CLI | OpenCode v1 |
|---|---|---|---|
| Launch wiring | `--settings roles/<role>.claude.json --append-system-prompt-file roles/<role>.md` | `-p fleet-<role>` with `$CODEX_HOME/fleet-<role>.config.toml` (installed by `install/codex.sh`) | `--agent fleet-<role>` with `agent."fleet-<role>"` in `opencode.json` (installed by `install/opencode.sh`) |
| Install step | none (passed per launch) | `install/codex.sh` | `install/opencode.sh` |
| Enforcement | DOC "Deny rules block in every mode, including bypassPermissions ... Allow rules have no effect in bypassPermissions", so roles use `permissions.deny` only | PROVEN `-p fleet-worker` layers the profile's `developer_instructions`: asked to run `fleet park NOLANE`, the lane declined ("ORCHESTRATOR owns fleet parking") while the same prompt without `-p` ran it; `enabled_tools` behaviour is UNVERIFIED; the shell cannot be limited in bypass mode, so a codex role is guidance plus the MCP tool list, not a hard fence | PROVEN `opencode debug config` resolves the installed `fleet-worker` agent (prompt loaded from `{file:...}`, bash deny map present); with a neutral prompt and the same permission block, OpenCode logged `pattern="fleet park NOLANE" ... action=deny` and blocked the call even with global `permission: "allow"`, while `echo` ran; `tools` behaviour is UNVERIFIED |

The launcher warns once and launches without the role when the Codex profile or OpenCode agent is not installed. The `fleet` role guard is an accident guard, not a security boundary: it trusts `FLEET_ROLE`, which any shell can unset. The per-CLI deny lists (Claude, OpenCode) add a second layer; Codex has only the role text.

## Lifecycle

The orchestrator keeps the fleet small by parking lanes that are finished and bringing them back when needed. `fleet tick` (read-only) shows who is due.

| Tier | Meaning | Roster `status` |
|---|---|---|
| LIVE | pane alive, recently active | `live` |
| IDLE | LIVE with nothing happening for 10 minutes; only a label in `fleet tick`/`fleet ls`, nothing is stopped | `live` |
| PARKED | compacted, then CLI stopped (C-c twice, SIGTERM after `FLEET_PARK_WAIT`), window gone, entry and session kept, resume command saved | `parked` |
| DONE / archived | finished for good; kept in the roster file and in `archive/roster-<ts>.json` copies | `done` / `archived` |

- **Park** a worker when its goal is achieved, or its goal was stopped for more than 10 minutes. Never park the orchestrator, desk or listener. A worker with no goal is parked only by the orchestrator's decision. An unread inbox message younger than 1 hour blocks a park.
- **Compact before park.** `fleet park` types `/compact` into the lane and waits (up to `FLEET_COMPACT_WAIT`, 600 s) for the CLI's own record of a finished compaction: a `compact_boundary` line in the Claude transcript, a `compacted` line in the Codex rollout, a completed `summary` message in the OpenCode session. Only then does it stop the CLI, so a reopen resumes the compacted context. If no record appears, or the transcript can't be read, it refuses to park and leaves the lane running; `fleet park NAME --no-compact` parks without compacting. The roster keeps `compactedAt` next to `parkedAt`. PROVEN on all three CLIs: after a compacted park, `fleet send` reopened the same session id and the lane named the word it had written before the compact.
- **Reopen** (`fleet reopen NAME`) resumes the same session id by default. `--fresh` starts a new lane from a handoff, and is for a reason only: context too big, transcript missing, or stale. If `$FLEET_STATE/handoff/NAME.md` is absent it is generated from the transcript (`fleet handoff NAME`).
- **Cleanup** (`fleet cleanup`, dry run) reports goal-less workers quiet for more than 2 hours, tmux windows with no roster lane, roster entries whose pane or pid is gone, and CLI processes naming a lane with no pane. `--apply` parks and marks the first and third and archives the roster first; `--kill-orphans` is needed to kill the others.

## Routing

- Messages go through `fleet send` / `peer_send` (fleet-mesh). Delivery always appends to the target's inbox, then wakes the lane. When the target is parked or done, delivery first runs `fleet reopen <to>` (timeout 180 s, `$FLEET_BIN` or `bin/fleet`), re-reads the roster, and wakes as usual; the result says `reopened`. A failed reopen still leaves the inbox line and says so.
- Decision answers go straight to the asker, not through the orchestrator. Each answer (from `decision_answer` or `desk.sh answer`) is appended to `$FLEET_STATE/decisions-log.jsonl` as `{id, at, asker, verbatim, relayedTo}`, which the orchestrator reads.
- New work, scope changes and launch or park requests go through the orchestrator.

## Per-CLI matrix

Tags: **PROVEN** = run locally on 2026-11-18 (see Proven on below); **DOC** = current vendor docs; **UNVERIFIED** = not proven on these versions.

| | Claude Code | Codex CLI | OpenCode v1 |
|---|---|---|---|
| Goal at launch | DOC first prompt `/goal <cond>`; the launcher waits for `Goal set:` in the transcript | PROVEN `codex --dangerously-bypass-approvals-and-sandbox "/goal ..."` set a goal; TUI showed Goal achieved; the `~/.codex/goals_1.sqlite` `thread_goals` row went `complete` | PROVEN `opencode --port P --prompt "/goal ... --max-turns N"`: plugin `goal_set` ok:true, title starts with a marker, state in `.opencode/goals/.../state.json` |
| Name | `-n NAME` | PROVEN no `--name`; the launcher types `/rename NAME` + Enter via `tmux send-keys` | roster entry maps name to session id + port |
| Send / list | native `SendMessage` / `ListAgents` | PROVEN `codex queue --thread NAME --message T` delivered to a live idle TUI and started a turn; list via fleet-mesh | fleet-mesh MCP only (no native messaging; DOC v1 docs) |
| Wake when idle | native | `codex queue` (PROVEN) | PROVEN `POST localhost:P/session/ID/prompt_async` returns 204 and the lane did the work |
| Bypass | `--dangerously-skip-permissions` + the skip env var | `--dangerously-bypass-approvals-and-sandbox` | `"permission":"allow"` in its config (installer: `--bypass`) |
| Trust step | needed for `/goal`; installer pre-trusts the dir (UNVERIFIED key name `hasTrustDialogAccepted`), else accept once | PROVEN the prompt appears in an untrusted dir; a `-c projects...trust_level` override did NOT skip it; installer writes `[projects."DIR"] trust_level = "trusted"` | none seen |
| Install adds | rules + skills, nothing else (mixed fleets: + fleet-mesh via `--mesh`) | `codex mcp add fleet -- python3 .../fleet_mesh.py` + AGENTS.md rules | `opencode.json` `"mcp":{"fleet":{...}}` + goal plugin clone (public repo, pinned tag `v1.5.3`) + rules |

**Proven on:** Claude Code 2.1.327, codex-cli 0.166.0, OpenCode 1.19.0, tmux 3.6b, macOS. Linux: all test suites pass on Ubuntu (bash 5.3, tmux 3.6); live lanes there are UNVERIFIED (the Linux paths use `/proc/<pid>/environ`).

Claude-to-Claude never touches fleet-mesh. A Claude lane in a mixed fleet uses `peer_send` to reach non-Claude peers.

### Waking a Claude lane from a non-Claude peer
fleet-mesh types one line into the Claude pane with `tmux send-keys`: `[fleet] message from X: run peer_inbox to read it`. It arrives as typed input, so it carries a pointer only, never the message body, and the rules tell lanes to treat it as a pointer. This is the chosen default; a development channel (blocked on managed org plans per the docs) and "no wake, check `peer_inbox` at phase ends" are the alternatives. UNVERIFIED end to end on a live Claude lane.

## Environment

All variables are optional. The full table (`FLEET_TMUX_SESSION`, `FLEET_TMUX_SOCKET`, `FLEET_STATE`, `FLEET_DIR`, `FLEET_HUMAN`, `FLEET_DESK`, `FLEET_ORCHESTRATOR`, `FLEET_NAME`, `FLEET_CLAUDE_BIN`, `FLEET_CODEX_BIN`, `FLEET_OPENCODE_BIN`) and the state layout are in [INTERFACES.md](INTERFACES.md).

Installer-only: `FLEET_OC_GOAL_PLUGIN_URL`, `FLEET_OC_GOAL_PLUGIN_REF`, `FLEET_OC_CONFIG_DIR`, `FLEET_PREFIX`, `FLEET_ROLES_DIR` (default `<repo>/roles`), `CODEX_HOME` (default `~/.codex`). Runtime: `FLEET_BIN` (the `fleet` script fleet-mesh runs to reopen a parked lane; default `<repo>/bin/fleet`).

## Goal files

```
fleet-goal new <goal.json> --goal "<text>" [--report-to NAME]
fleet-goal check <goal.json>        first line OK or FAIL <path>: <why>; exit 0/1
fleet-goal condition <goal.json> [--report-to NAME] [--turns N]
```
`check` needs `goal`, `phases[{id,title}]`, `nextAction`, `done[]`, `openForHuman[{id,question,status}]`, `decisions[{id,decision}]`, and ISO-8601 `at` stamps where present. `condition` prints a `/goal` line under 4000 characters that names the file, says the file wins, requires one `GATE <id> PASS` line per `gates` key plus the `fleet-goal check` OK line after the last edit, has a `GOAL HALT: <reason>` exit, a turn bound, and ends the turn when only `openForHuman` items remain.

## Tests

```bash
python3 -m unittest test/test_mesh.py   # fleet-mesh delivery, reopen, decisions log
bash test/test_goal.sh        # fleet-goal and the desk helper (incl. decisions log), with a broken-file control
bash test/test_installs.sh    # installers against a throwaway HOME with stubbed CLIs
bash test/test_privacy.sh     # no personal data in the repo (FLEET_PRIVACY_TERMS=a|b adds names)
```

## Limits

- Needs tmux; no Windows support. macOS: test suites plus live lanes proven (bash 3.2, tmux, Claude Code 2.1.327, Codex 0.166.0, OpenCode 1.19.0). Linux: every test suite passes on Ubuntu (bash 5.3, tmux 3.6); live CLI lanes on Linux are UNVERIFIED.
- Installers edit JSON with jq, so a JSONC `opencode.json` (comments) is refused with a message instead of being rewritten. Existing files are backed up as `<file>.fleet-bak.<timestamp>` before any change.
- Proofs the launcher runs (pid/env check, goal active) depend on CLI internals (session transcripts, a sqlite file, the OpenCode HTTP API) that can change between versions; `--no-proof` skips them.
- The OpenCode goal plugin is cloned as-is at the pinned tag; whether it needs a build or install step on your machine is UNVERIFIED here. If the plugin dir already holds an install from the plugin's own installer (a `package.json`, no `.git`), the installer reuses it and never clones over it; a non-empty dir with neither fails loud.
- Claude trust pre-seeding (`install/claude.sh --trust DIR --trust-only`) is PROVEN on Claude Code 2.1.327: lanes stuck at the trust dialog launched cleanly after it. The launcher fails loud (`FAIL ... workspace-trust dialog`) if a pane still sits at that dialog.
- Claude's Bypass Permissions accept dialog appeared on 2 of 12 lane starts in testing even with `skipDangerousModePermissionPrompt` set (cause UNVERIFIED). The launcher detects it and fails loud (`FAIL ... Bypass Permissions accept dialog`), including on `resume`; accept it in the pane (`fleet attach`) or stop and relaunch.
- `opencode export` (1.19) truncates its JSON when stdout is a pipe, so `fleet handoff` writes it to a temp file first.
- Waking an idle Claude lane from a non-Claude peer relies on typed input into the pane (see above).
- Nothing is pushed or sent anywhere by the kit; the only network call is the plugin `git clone` in `install/opencode.sh`.

## License

MIT, see [LICENSE](LICENSE).
