---
name: launching-a-goal-lane
description: "Start a new lane (any CLI) inside an active goal locked to a living goal file: write and validate the goal file, generate the /goal condition, launch with fleet launch, confirm the goal is active. Triggers on: start a lane, launch a peer, new session for this work, spin up a worker, start it in a goal, goal file for a lane, fleet launch, launch codex lane, launch opencode lane, hand this to a new session."
---

# launching-a-goal-lane

ROUTE owns launching: other roles ask it (`fleet launch` is refused for any `FLEET_ROLE` other than route). Default role is `worker`; pick one with `--role route|decide|watch|learn|worker`.

Every lane starts inside a goal whose condition points at a living goal JSON. The goal file is the state; the condition just binds the session to it.

## Do this
1. **Write the goal file.** `fleet-goal new <dir>/<lane>.goal.json --goal "<what must be true at the end>" --report-to ROUTE`. Then edit it: real `phases[]` (id + title), `gates{}` (one key per checkable milestone), `mustNot[]`, and a concrete `nextAction`. A handoff `.md` can sit next to it, but the JSON is the state.
2. **Validate.** `fleet-goal check <goal.json>` must print `OK` as its first line. Fix any `FAIL <path>: <why>` first.
3. **Generate the condition.** `fleet-goal condition <goal.json> --report-to ROUTE --turns 150 > <dir>/<lane>.cond.txt`. It starts with `/goal `, stays under 4000 characters, names the goal file, says the file wins, requires `GATE <id> PASS` lines and the `fleet-goal check` OK line after the last edit, has a `GOAL HALT: <reason>` exit and a turn bound.
4. **Launch.** `fleet launch --cli <claude|codex|opencode> [--model M] [--dir D] <lane> goal <dir>/<lane>.cond.txt`. Use `--split-right-of <name>` to put it beside another pane. `fleet launch --dry-run ...` prints the window script, the first prompt and the tmux command without starting anything.
5. **Confirm.** The launcher prints one line `OK <name> cli=<cli> mode=goal pane=<id> session=<id> goal=active`. If it prints `FAIL <reason>`, read the reason: a missing trust prompt acceptance (`install/<cli>.sh --trust <dir>`), a live duplicate name, or a condition that fails the lint. `fleet ls` shows liveness.

## Gotchas
- The condition must start with `/goal ` (commands are only recognized at the start of the prompt).
- A lane's name matches `^[A-Za-z0-9._-]+$` and must not belong to a live pane.
- Do not launch from inside a directory the CLI has not trusted: `/goal` needs a trusted workspace.
- A plain-prompt launch (`fleet launch <name> prompt <text|file>`) is only for a successor started with `resume`; new lanes use a goal.
- The decisions desk (DECIDE) never gets a `/goal`: `fleet desks up` starts it in desk mode.
- **Claude worker lanes do not get `Workflow`, `ScheduleWakeup` or `ReportFindings`** (about 6k tokens of tool schemas at lane start that a worker rarely uses). If the goal permits a workflow, put `"keepTools": ["Workflow"]` in the goal file (`fleet-goal check` validates it; allowed entries `Workflow`, `ScheduleWakeup`, `ReportFindings`, `all`); `fleet launch` reads it from the goal JSON the condition names. A code-review lane that reports with `ReportFindings`, or a `/loop` lane, keeps those the same way, or launch with `--keep-tools <list>`. The roster records `keepTools`, so `fleet reopen` keeps the same set, and a lane launched before this block reopens with every tool. Other roles and other CLIs are unchanged. Check with `fleet launch --dry-run ...`: the `--disallowed-tools=` list is in the printed script.
- A launched worker is parked by ROUTE when its goal is achieved, or stopped for more than 10 minutes (an unread message younger than 1 hour blocks it). Standing roles are never parked, and a goal-less worker lives only by ROUTE's decision, so every worker gets a goal. A parked lane comes back with `fleet reopen NAME` (same session id), or by a message to it.
- Lanes report status to ROUTE and send questions to DECIDE, so put both names in the goal file (`reportTo`) and the nextAction.
