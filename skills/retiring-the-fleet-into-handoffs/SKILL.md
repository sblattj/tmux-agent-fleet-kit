---
name: retiring-the-fleet-into-handoffs
description: "Retire the orchestrator and every live lane without stopping anyone mid-work: each lane names a successor and writes a handoff file, the orchestrator writes its own delta handoff, and the next orchestrator respawns successors with fleet launch. Triggers on: retire the fleet, retire all lanes, you are retiring, new orchestrator taking over, hand over to a fresh session, create a handoff for another session, reconnect every session to the new MCP version, resurrect the orchestrator, successor handoffs."
---

# retiring-the-fleet-into-handoffs

Hands the whole fleet to fresh sessions: one main handoff for the next orchestrator plus one handoff per lane, named after its successor. Handoff folder: `${FLEET_HANDOFF_DIR:-$HOME/.local/state/fleet/handoff}` (main file at its top, lane files in `peers/`).

## Lifecycle first
Retirement is the whole-fleet case. Day to day the orchestrator parks single lanes (`fleet park NAME`) when their goal is achieved or stopped for more than 10 minutes, never standing roles, and never while an unread message younger than 1 hour is waiting. `fleet reopen NAME` resumes the same session id by default; hand off to a fresh session only with a reason (context too big, transcript missing, stale), and if `$FLEET_STATE/handoff/NAME.md` is absent `fleet handoff NAME` builds it from the transcript. A message to a parked lane reopens it. Before retiring, run `fleet cleanup` (dry run) to see stray windows, dead roster entries and quiet goal-less workers; `fleet cleanup --apply` acts on them (the orchestrator only).

## Do this
1. **Roster.** `fleet ls` (and `ListAgents` in Claude Code). Skip worker cells another lane owns; their owner retires them. The decisions desk stays up: tell it to relay answers for retired lanes to the next orchestrator.
2. **Main handoff first** (durable before anything else): `<handoff dir>/<topic>-handoff-<date>-<slot>.md`. Write it as a delta on the previous handoff: "supersedes X", what changed, a lane table (owns / waiting on), open decisions for the human, follow-ups, new gotchas. Put a `## First thing` section at the top (step 5).
3. **Retirement message to every lane** (one message each, sent together; `SendMessage` for Claude, `peer_send` for Codex/OpenCode):
   > Retirement notice from <me>: <me> is retiring, and a new orchestrator session named "<NEXT>" takes over. Do not stop mid-work. Finish your current step (<name it if you know it>), then: (1) pick a new descriptive name for your successor session; (2) write a self-contained handoff to <handoff dir>/peers/<new-name>.md starting with the line `Was: <your-current-name>`: what you own, ids, head SHAs, worktree paths, done, next, waiting on, gotchas; (3) reply to <me> with the name and path. If <me> is gone, the file alone is enough. Then stop.

   Lanes that run workers: let the workers finish and report first.
4. **As replies land:** check the roster, verify any surprising claim with one call (`gh pr view`, `git log`), and fold corrections into the main handoff under `## Corrections found during retirement`. Then:
   ```bash
   ~/.claude/skills/retiring-the-fleet-into-handoffs/roster.sh <handoff dir>/peers <old-name> <old-name> ...
   ```
   It lists every handoff with its `Was:` line and names retirees that have not filed.
5. **`## First thing` for the next orchestrator:** list the peers folder; wait for, never stop, any lane still finishing; then start one session per handoff file, named after the file, with the prompt "You are <new-name>. Read <handoff dir>/peers/<new-name>.md and continue that work; report to <NEXT>":
   ```bash
   fleet launch --cli <cli> <new-name> prompt "You are <new-name>. Read <path> and continue that work; report to <NEXT>"
   ```
   A successor that is a new session (not `resume`) starts inside `/goal`: have the retiring lane write a living goal file next to its handoff (`fleet-goal new`), then `fleet launch <new-name> goal <cond.txt>` (see `launching-a-goal-lane`). `fleet launch` refuses a name that is still live, closes nothing itself, and prints a line starting `OK <name>` on success.
6. **Close the open decisions before the human leaves.** They are desk items: ask them through the desk, write the answers into the main handoff under `## Decisions`, and relay any answer that changes a live lane's plan to that lane. When a lane already retired, put the relay in the main handoff addressed to its successor.
7. **Launch the next orchestrator the same way:** `fleet launch ORCHESTRATOR prompt "You are <NEXT>. Read <main handoff> and do what it says, starting with First thing."` The old orchestrator must have exited (`fleet stop <old>`) or the new one needs a different name.

## Gotchas
- **A mode mismatch can silently drop messages.** Claude lanes run in bypass mode; an orchestrator in a different permission mode can have lane reports held. `fleet launch` starts everything in bypass for that reason.
- **A relaunched orchestrator can double-start successors.** Tell it which successors already exist (`fleet ls`).
- **Lane claims drift during handoff.** Verify "PR is open/merged/changes requested" with one call before writing it into the next handoff.
- **Grep-by-old-name cannot tell who filed.** Hence the required `Was: <old-name>` first line; roster.sh keys on it. A hand-opened session writes `Was: <listed name>`.
- **Two successors can claim one PR stack.** Say in the main handoff who writes and who only watches.
- **A handoff's "retry X" can be stale.** Check state before replaying any step a handoff says to retry.
- **A lane without a question tool asks in plain text, then goes idle.** Read each idle notice's preview for a question and answer it (through the desk if it needs the human).
- Loop lanes must delete their scheduled jobs before handing off, or the successor's jobs fire twice. Flag the earliest one-shot in the main handoff.
- Main handoff before the lane broadcast: if the session dies mid-retirement the folder plus the main file still let the next orchestrator rebuild.
- Do not stop anyone mid-work: lanes finish the current step, then hand off.
