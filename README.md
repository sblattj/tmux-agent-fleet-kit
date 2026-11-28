# tmux-agent-fleet-kit

Run a small fleet of coding-agent sessions in tmux: a **ROUTE** desk that routes work and never asks questions, a **DECIDE** desk that is the only place the human answers, **WATCH** and **LEARN** desks, and **goal lanes** that each start inside a goal locked to a living goal file. Works with Claude Code, Codex CLI, OpenCode v1, Kiro CLI (v3 engine) and Antigravity CLI (`agy`), alone or mixed.

Needs: bash 3.2+, tmux 3.0+, jq, python3 (stdlib only), and the CLI you launch. The outer terminal does not matter: the kit only talks to tmux.

## The layout

```
tmux session "fleet"   (attach from any terminal: fleet attach)
  window deskA:  ROUTE | DECIDE   ROUTE (left): routes work, owns lifecycle; DECIDE (right): the only place the human answers
  window deskB:  WATCH | LEARN    WATCH (left): watches and reports, to DECIDE only; LEARN (right): the ledger of what the fleet decided and did
  window: lane-a  (/goal)         status reports -> ROUTE
  window: lane-b  (/goal)         questions      -> DECIDE
  more lanes, each claude | codex | opencode | kiro | agy
```

`fleet desks up [--cli C] [--model M] [--dir D] [--handoff DIR] [--no-proof] [--dry-run]` builds the two desk windows, `deskA` (ROUTE left, DECIDE right) and `deskB` (WATCH left, LEARN right), with no separate WATCH or LEARN window; each desk is an LLM lane in desk mode, and WATCH and LEARN start on a read-only "look for work" brief (docs/design/four-desks.md). When it launches WATCH it also opens a small non-LLM pane under it running `fleet watch status` (tagged `@fleet-watch-status`, one per session). It is idempotent: a desk whose pane is live is left alone, so a rerun on a live fleet prints `<D> already live` for each desk and launches nothing; every run also puts window `deskA` at tmux index 0 and `deskB` at 1 (moving or swapping windows, never changing a pane id), and `fleet launch` opens lane windows at index 2 and up; a dead desk is relaunched into its half of its window. It never touches other lanes: a fleet still running the older ORCHESTRATOR and DECISIONS sessions keeps them, with their windows, roster entries and inboxes, and the new desks start beside them under their own names. Names are fixed: there is no alias from the old names to the new ones.

The desks are the default (issue #9): every installer ends with `fleet desks up --cli <its CLI>` (`--no-desks` skips it), and `fleet launch` of a worker or probe lane brings the desks up first when the roster has no live ROUTE or DECIDE, or fails with `FAIL no desks: ...` and launches nothing (`--no-desks` or `FLEET_NO_DESKS=1` to launch anyway). `fleet tick` flags `WARN desks down: ...` when a desk pane of deskA or deskB is dead or missing.

**Migrating from pre-kit desks.** A fleet whose routing and decision desks are older sessions (ORCHESTRATOR, DECISIONS, LISTENER) moves over without two desks answering at once: each old desk writes a handoff (`fleet handoff ORCHESTRATOR`, or its own handoff file), then is stopped (`fleet stop NAME`); then `fleet desks up --handoff DIR` starts each new desk on its predecessor's file, `DIR/<DESK>.md` or the old name's (`ORCHESTRATOR.md` for ROUTE, `DECISIONS.md` for DECIDE, `LISTENER.md` for WATCH). `fleet handoff` writes to `$FLEET_STATE/handoff`, so `--handoff "$FLEET_STATE/handoff"` picks those up. It refuses while an old desk is still live in the roster.

- `bin/fleet` launches and lists lanes (`fleet launch`, `fleet ls`, `fleet attach`, `fleet layout`, `fleet stop`). `bin/tafk` is the same command under a second name (a symlink to `fleet`), so `tafk <anything>` runs `fleet <anything>`.
- `bin/fleet-update` moves the kit to the latest release (`fleet update`, `tafk update`).
- `bin/fleet-goal` creates, validates and turns a living goal file into a `/goal` condition.
- `lib/fleet_mesh.py` is a stdlib-only stdio MCP server (`peer_list`, `peer_send`, `peer_inbox`, `decision_open`, `decision_list`, `decision_answer`) for CLIs without native messaging.
- `rules/fleet-rules.md` is the operating rules block the installers put into each CLI's global instructions file.
- `skills/` has four Claude Code skills: running the decisions desk, launching a goal lane, checking a peer before relaying, retiring the fleet into handoffs.

## Quick start

Common first steps: clone the repo, then run the installer for your CLI. Every installer is idempotent, retires the v0.1.x role names (desk, orchestrator, listener) on upgrade, takes `--dry-run` (prints every change, writes nothing), `--prefix DIR` (default `$HOME/.local`; symlinks `bin/fleet`, `bin/fleet-goal` and `bin/tafk` into `DIR/bin`, so put that on PATH) and `--trust DIR` where the CLI needs it. Placeholders in the rules come from `FLEET_HUMAN`, `FLEET_DECIDE` (default `DECIDE`) and `FLEET_ROUTE` (default `ROUTE`).

### Claude Code
```bash
install/claude.sh --trust ~/work/project          # skills, rules in ~/.claude/CLAUDE.md, bin symlinks, then fleet desks up
install/claude.sh --mesh                          # optional: registers fleet-mesh user-wide; fleet launch already passes it per lane (--mcp-config)
fleet desks up --dir ~/work/project              # the five desks (the installer already ran it; rerun for another --dir): deskA (ROUTE | DECIDE over AUTH), deskB (WATCH | LEARN)
fleet-goal new /tmp/lane-a.goal.json --goal "..." --report-to ROUTE
fleet-goal condition /tmp/lane-a.goal.json --report-to ROUTE > /tmp/lane-a.cond.txt
fleet launch --cli claude lane-a goal /tmp/lane-a.cond.txt
```

### Codex CLI
```bash
install/codex.sh --trust ~/work/project           # rules in $CODEX_HOME/AGENTS.md (default ~/.codex), codex mcp add fleet, trust entry, role profiles
fleet launch --cli codex lane-b goal /tmp/lane-b.cond.txt
```

### OpenCode v1
```bash
install/opencode.sh --bypass                      # goal + compact plugin clones, plugins + mcp + role agents in opencode.json/tui.json, rules
fleet launch --cli opencode lane-c goal /tmp/lane-c.cond.txt
```
`--bypass` sets `"permission": "allow"` so unattended lanes are never blocked on a prompt; leave it off if you want prompts. Plugin override env: `FLEET_OC_GOAL_PLUGIN_URL`, `FLEET_OC_GOAL_PLUGIN_REF` (default tag `v1.5.3`).

OpenCode v1 has no auto-compact of its own for a model that reports `limit.context: 0`, and a big window can still run long. The installer also clones [opencode-compact-at-token-limit](https://github.com/sblattj/opencode-compact-at-token-limit) (default tag `v0.1.2`, override with `FLEET_OC_COMPACT_PLUGIN_URL` / `FLEET_OC_COMPACT_PLUGIN_REF`) and lists it in `opencode.json` only (it is a server plugin, not a TUI one). Every lane then compacts on its own when a step's context reaches the limit. One plugin owns this: the installer writes the goal plugin's entry as `["file://...", {"compactAtTokens": 0}]`, which turns the goal plugin's own forced compaction off, so a goal lane never compacts twice for one crossing. The limit has one knob, `FLEET_OC_COMPACT_LIMIT` at install time (`150000`, `150k`, `1.5m`; default `200000`; `0` means no forced compaction at all). It is written as the plugin option (`["file://...", {"limit": 150000}]`), which beats `OPENCODE_COMPACT_AT_TOKENS`, so whatever env the tmux server carries never changes it. A rerun without the variable keeps the limit already there.

### Kiro CLI
```bash
install/kiro.sh                                   # rules, fleet MCP, role agents, trust-all confirmation off, all under ${KIRO_HOME:-~/.kiro}
fleet launch --cli kiro lane-d goal /tmp/lane-d.cond.txt
```
The installer always sets `chat.disableTrustAllConfirmation` to `true` (lanes always run trust-all) and writes: `steering/fleet-rules.md`, `settings/mcp.json` (`mcpServers.fleet` = `python3 <repo>/lib/fleet_mesh.py serve`), `settings/cli.json`, `agents/fleet-<role>.json` (all under `${KIRO_HOME:-~/.kiro}`). The setting is not in the vendor docs, but it is PROVEN to clear the trust-all warning on a fresh `KIRO_HOME`; without it that warning defaults to "No, exit" and the launcher fails loud.

### Antigravity CLI (agy)
```bash
install/agy.sh --trust ~/work/project             # rules, fleet MCP, role agents, skills, all under ${FLEET_AGY_HOME:-~/.gemini}/config
fleet launch --cli agy lane-e goal /tmp/lane-e.cond.txt
```
The installer writes `config/rules/fleet-rules.md` (`trigger: always_on`), `config/mcp_config.json` (`mcpServers.fleet` via `agy mcp add`, jq when agy is not on PATH or `FLEET_AGY_HOME` is not `$HOME/.gemini`), `config/agents/fleet-<role>.md` and `config/skills/<name>/`; `--trust DIR` adds DIR to `antigravity-cli/settings.json` `trustedWorkspaces`. Nothing global turns permissions off: each lane gets `--dangerously-skip-permissions` plus a per-lane `--settings` file (`$FLEET_STATE/launch/<name>.agy-settings.json`: tool permission always-proceed, the lane dir trusted, the role's deny rules). agy always reads `$HOME/.gemini`; `FLEET_AGY_HOME` only tells the kit where that is. Sign in and finish agy's first-run screens once by hand before launching lanes (agy records that in `antigravity-cli/cache/onboarding.json`): the launcher fails loud on a trust dialog, the onboarding screens or a sign-in wait, and fleet-mesh never types a wake into an agy pane showing one of them, because Enter there would answer for you (the Terms of Service screen has a data-sharing opt-in pre-checked).

### Updating (`tafk update`)

```bash
tafk update --check                               # report only: "update available: v0.8.1 -> v0.9.0" or "already at v0.9.0"
tafk update --dry-run                             # the refusals plus every step it would run; writes nothing
tafk update                                       # move to the latest release, then refresh the bin links; prints "updated: vOLD -> vNEW"
```

`fleet update` is the same command. It finds the latest release tag (the highest `vX.Y.Z`; prerelease tags like `v1.0.0-rc.1` don't count) and never moves backwards: a kit already at or past it prints `already at vX.Y.Z`, exits 0 and changes nothing.

- **A git checkout** (the clone the Quick start makes): the current version is the nearest `vX.Y.Z` tag at HEAD, the latest is the highest one on `origin` (`git ls-remote`, no GitHub login needed). It fetches that tag and fast-forwards the current branch to it (`git merge --ff-only`; a detached HEAD moves to the tag). It refuses, exits 1 and changes nothing when the tree is dirty (anything in `git status --porcelain`, untracked files too), when HEAD isn't an ancestor of the tag (diverged: rebase or reset it yourself), or when `origin` can't be reached.
- **A copied tree** (no `.git`, for example an unpacked release tarball): the current version is its `VERSION` file, the latest is the GitHub releases API's `tag_name`. It downloads that tag's tarball next to the tree, checks the `VERSION` inside, then swaps it in with two renames and keeps the old tree beside it as `<dir>.prev-vOLD` (the swap rolls back if the second rename fails). Offline or a bad tarball: exit 1, nothing changed. A tree with neither `.git` nor `VERSION` can't tell its version, so reinstall it: clone the repo and run its `install/<cli>.sh`.
- **Links:** afterwards the symlinks in `${FLEET_PREFIX:-~/.local}/bin` that already point into this kit are refreshed, and `tafk` is added beside a `fleet` link that points here. Links that point anywhere else are left alone. It never reruns an installer and never touches tmux, so live desks and lanes keep running; they pick up the new code on their next `fleet` call.
- Every network call is bounded by `FLEET_UPDATE_TIMEOUT` (default 20 seconds). `fleet version` prints the kit's version (`git describe`, else `VERSION`).

Try anything first with `fleet launch --dry-run ...` (prints the window script, first prompt and tmux command, touches nothing).

## Roles

`fleet launch --role route|decide|watch|learn|auth|worker|probe` (default `worker`; `desk` mode defaults to `decide`). The role is exported as `FLEET_ROLE` in the lane's window and stored in the roster. The old names `orchestrator`, `desk` and `listener` are refused at launch in every mode, resume included (`FAIL role desk is renamed decide`); their files were removed once the pre-rename sessions retired. `probe` is DECIDE's read-only probe lane (docs/design/four-desks.md 2.5): no shell, no writes, only the `fleet-probe` read verbs over a throwaway shallow clone. It launches only on a CLI whose live lockdown test (`FLEET_PROBE_LIVE=1 test/test_probe_live.sh --cli <cli>`) proved the probe can't run a shell command or write a file while a worker on the same CLI can: today that is claude, opencode and kiro (live runs 2026-11-22: Claude Code 2.1.330 15/0, OpenCode 1.19.0 17/0, kiro-cli 3.5.0 17/0); codex and agy are refused outright (`FAIL role probe refused on --cli codex`), because they can't drop their shell tool on a proven path; a missing `fleet-probe` profile fails the launch instead of launching without it. The five standing roles and `desk` mode launch Claude with `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF=1` (shown in `--dry-run`), so a desk that exits never hands its session to a background job that keeps its name. A name is taken when the roster has a live pane for it, a `claude` process has `-n NAME` in argv, or (issue #5) a live pid's `~/.claude/sessions/<pid>.json` has `name` NAME (`FLEET_CLAUDE_HOME` overrides the dir); the launch FAILs `already live (pid N, ...)`. The last two checks are skipped with `--no-proof`.

| Role | Does | Denied |
|---|---|---|
| route | owns launch, park, reopen, cleanup; routes work; reads the decisions log; never edits | Edit, Write, NotebookEdit, `git commit`, `git push` |
| decide | the only place the human answers; relays each answer to the asker and logs it; the only desk that sends work to ROUTE | route's denies plus `fleet launch`, `park`, `stop`, `reopen` |
| watch | turns feed events into messages to DECIDE, never to ROUTE | same as decide |
| learn | keeps the ledger of decisions and shipped work, quizzes the human | same as decide |
| worker | runs one goal, reports to ROUTE, asks DECIDE | `fleet launch`, `park`, `stop`, `reopen` |

**Per-lane env (issue #4).** `fleet launch --env KEY=VALUE` (repeatable) writes `export KEY=VALUE` into the lane's window script, with VALUE shell-quoted and KEY checked as an env name (fleet's own `FLEET_NAME`, `FLEET_ROLE`, `FLEET_STATE`, `FLEET_TMUX_SESSION`, `FLEET_MESH` and the skip var are refused). The set is stored in `launch/<name>.launch.json`; `fleet reopen` (resume and `--fresh`) passes it again, and a `fleet launch ... resume` with no `--env` reapplies it. `FLEET_PRELAUNCH_HOOK=<cmd>` runs before every launch, dry runs included, with the lane's name, mode and argument (the condition file for a goal), and prints `KEY=VALUE` lines to export, so a goal-file field can drive them; `--env` wins on a clash and a nonzero exit refuses the launch. `FLEET_STRIP_EXTRA="VAR1 VAR2"` adds parent-shell-only vars to the script's `unset` line. `--dry-run` shows the export and unset lines, and for Claude the env proof FAILs `env check failed: env:KEY` when a key never reached the CLI process.

Every role denies AskUserQuestion. Claude worker lanes also deny Workflow, ScheduleWakeup and ReportFindings unless the goal file lists them in `keepTools` (or `fleet launch --keep-tools`). Role files live in `roles/` (`<role>.md`, `<role>.claude.json`, `<role>.codex.toml`, `<role>.opencode.json`, `<role>.kiro.json`, `<role>.agy.json` + `<role>.agy.md`). Separately, `fleet launch|park|stop|reopen|cleanup --apply` refuse with `FAIL role <r> can't <verb>` when `FLEET_ROLE` is set to anything but `route`; a human shell with `FLEET_ROLE` unset is allowed.

### How each CLI enforces a role

Tags: **PROVEN** = run locally; **DOC** = vendor docs; **UNVERIFIED** = not proven on the versions below.

| | Claude Code | Codex CLI | OpenCode v1 | Kiro CLI | Antigravity CLI |
|---|---|---|---|---|---|
| Launch wiring | `--settings roles/<role>.claude.json --append-system-prompt-file roles/<role>.md` | `-p fleet-<role>` with `$CODEX_HOME/fleet-<role>.config.toml` (installed by `install/codex.sh`) | `--agent fleet-<role>` with `agent."fleet-<role>"` in `opencode.json` (installed by `install/opencode.sh`) | `--agent fleet-<role>` with `$KIRO_HOME/agents/fleet-<role>.json` (installed by `install/kiro.sh`); PROVEN the installed `fleet-worker` loaded (session.json `agentMode` `fleet-worker`) | `--agent fleet-<role>` with `$FLEET_AGY_HOME/config/agents/fleet-<role>.md` (installed by `install/agy.sh`; frontmatter `mainAgent: true`, `inheritMcp: true`, body = `roles/<role>.md`) plus `--settings <lane>.agy-settings.json` carrying `roles/<role>.agy.json`; PROVEN a probe agent from `config/agents/` loaded with `--agent` |
| Install step | none (passed per launch) | `install/codex.sh` | `install/opencode.sh` | `install/kiro.sh` | `install/agy.sh` |
| Enforcement | DOC "Deny rules block in every mode, including bypassPermissions ... Allow rules have no effect in bypassPermissions", so roles use `permissions.deny` only | PROVEN `-p fleet-worker` layers the profile's `developer_instructions`: asked to run `fleet park NOLANE`, the lane declined ("ORCHESTRATOR owns fleet parking") while the same prompt without `-p` ran it; `enabled_tools` behaviour is UNVERIFIED; the shell cannot be limited in bypass mode, so a codex role is guidance plus the MCP tool list, not a hard fence | PROVEN `opencode debug config` resolves the installed `fleet-worker` agent (prompt loaded from `{file:...}`, bash deny map present); with a neutral prompt and the same permission block, OpenCode logged `pattern="fleet park NOLANE" ... action=deny` and blocked the call even with global `permission: "allow"`, while `echo` ran; `tools` behaviour is UNVERIFIED | PROVEN V3 `permissions.rules` (capability `shell`, effect `deny`): a deny on `fleet park*` is enforced under `--trust-all-tools`, also inside `/goal` workflows and in `cd /tmp && fleet park X` chains; NOT caught: `sh -c "fleet park X"` ran. PROVEN the installed `fleet-worker` lane declined `fleet park NOLANE` from its role text. DOC deny rules in any scope still apply under trust-all. A `file://` prompt path is rejected at runtime and Kiro silently falls back to the default agent (dropping the denies), so `install/kiro.sh` inlines `roles/<role>.md` as the prompt (PROVEN). So a Kiro role is deny rules for direct commands plus role text, not a hard fence | PROVEN a `permissions.deny` `command(fleet stop)` rule in the `--settings` file blocked `fleet stop nothing` under `--dangerously-skip-permissions` (agy logs the loaded deny list at start); worker and decide/watch/learn deny `command(fleet launch)`, `command(fleet park)`, `command(fleet stop)`, `command(fleet reopen)`, route and decide/watch/learn deny `write_file(*)`, `command(git commit)`, `command(git push)` and get no write tools in their agent file. UNVERIFIED: whether the `write_file(*)` deny also covers edit tools and artifacts, and `sh -c` wrappers. So an agy role is deny rules plus role text, not a hard fence |

The launcher warns once and launches without the role when the Codex profile, OpenCode agent, Kiro agent or agy agent is not installed (agy deny rules still apply then, they come from the per-lane settings). `tafk` is `fleet` under a second name, so every per-CLI deny on `fleet launch`, `fleet park`, `fleet stop` and `fleet reopen` has the same deny on `tafk launch`, `tafk park`, `tafk stop` and `tafk reopen` (`test/test_roles.sh` checks the twins in all four formats). The `fleet` role guard is an accident guard, not a security boundary: it trusts `FLEET_ROLE`, which any shell can unset. The per-CLI deny lists (Claude, OpenCode, Kiro, agy) add a second layer; Codex has only the role text, and Kiro's deny misses `sh -c` wrappers.

### LEARN: the ledger and the quiz

`fleet learn` keeps `$FLEET_STATE/learn/`: an append-only ledger of what the fleet decided or shipped (origin
`fleet`) and what changed outside it (origin `outside`), quiz cards, reviews and a proof cache.

- `fleet learn sync` reads `decisions-log.jsonl` (it appears with the first answered call), WATCH events with
  score >= `learn.minScore`, and the goal files of lanes whose goal is achieved. Call decisions
  (`decided-on-call`) arrive as `heard` and are never quizzed until a desk answer or `fleet learn confirm`
  backs them.
- Teach before test. `fleet learn lesson [--topic T]` shows the terms first (each work subject with a verbatim
  definition and its typed links), then each row in context; every line is a verbatim quote plus its source, and a
  what/why line its quote does not back is dropped and counted, never reworded. Heard rows show "not decided yet".
  Showing a lesson records it seen (`learn/seen.jsonl`, the human only); only then do that row's cards come due, and
  the 24 h proof clock starts there. `--preview` records nothing. `fleet learn lessons` lists what waits.
- Two card stacks. `auto` is what sync and the sources make; `manual` is the cards the human asked for:
  `fleet learn add ... --manual`, or `fleet learn stack L3-L9 --manual` for rows already in the ledger
  (`fleet learn stack` lists the stack, `--auto` moves rows back). Manual cards are served ahead of every auto card,
  in lessons and in the quiz, the newest request first, and `fleet learn rebuild` keeps them (`learn/manual.jsonl`).
  They also guide the auto cards: auto rows that share a topic or work subject with a manual row come first among the
  auto rows, `fleet learn examples` lists the manual cards as few-shot examples for LEARN, and an optional
  `learn.json` `autoPrompt` command (bounded by `timeoutSec`, off by default) writes questions for untitled auto rows
  from those examples.
- `fleet learn session [--topic T]` is the lesson for T, a quiz on T's cards only, and one result line (with no
  `--topic`, the first lesson waiting: the manual stack first); a first
  exposure is never graded as learned. `fleet learn digest [--since ISO|Nd|Nh]` teaches the rows newer than the last
  digest (default 7 days). `fleet learn brief --for X` is a read-only brief for any role; `learn.json`
  `brief.selector` names an optional command (JSON on stdin, one row id per line out, bounded by
  `brief.timeoutSec`), with a fallback to X's rows, else the most recent.
- Sources: `$FLEET_STATE/sources.json` (else `watch.json`) names the git repos, trackers and call workspaces LEARN
  reads through WATCH's adapter registry, credentials as `env:`, `keychain:` or `cmd:` references only
  (`fleet learn sources` checks them). Generic example: `docs/learn-sources.example.json`. Adapters include
  `whosaid` (call decisions, local, heard) and `linear` (GraphQL, read-only). No config, no polling. Each source
  drains until it has nothing more, bounded per source by 50 polls and 300 s, with WATCH's `ratePerMin` gate on
  every request; a rebuild that stops early fails closed (rc 1), a sync prints `sources: PARTIAL` and reads on.
- `fleet learn rebuild` regenerates the ledger, cards, entity graph (`part-of`, `ships-in`, `blocked-by`,
  `decided-by`, `owner` links from the sources), proof and cursors from the sources. Only the human's own results,
  `reviews.jsonl` and `seen.jsonl`, are kept; they re-attach by source key, and a failing source aborts with the old
  state intact. A manual `fleet learn add` is not a source, so a rebuild drops it (its seen rows show as orphaned).
- `fleet learn quiz` runs recall (self-rated), multiple choice (distractors are other ledger rows, never
  invented) and source check (paste a verbatim line) on FSRS intervals (`"scheduler": "fixed"` in `learn/learn.json` keeps 1/3/7/21 days). Explain back and
  apply cards are graded by an optional `command` grader in `learn.json`, else self-graded.
- An item is learned only with a recall pass and a checkable pass, the latest at least 24 h after first exposure
  (the lesson seen, else the first review), and no fail since. `fleet learn proof --check` rebuilds proof.json from reviews.jsonl and compares.
- `fleet learn snapshot` gives counts per origin, never one blended total. `fleet learn export` writes one
  markdown file per topic.

## Lifecycle

ROUTE keeps the fleet small by parking lanes that are finished and bringing them back when needed. `fleet tick` (read-only) shows who is due.

`fleet tick` also ends with one browser line, `chrome tabs: N (agent A, other B)`, read from Chrome's DevTools list at `FLEET_CDP_URL` (default `http://127.0.0.1:9222`, pages only; "agent" means cdp-toolkit recorded opening the tab). Above `FLEET_TAB_WARN` (default 50) it adds a WARN line. If Chrome is down it prints `chrome tabs: unavailable` and tick carries on. It only reads; it never opens or closes a tab.

| Tier | Meaning | Roster `status` |
|---|---|---|
| LIVE | pane alive, recently active | `live` |
| IDLE | LIVE with nothing happening for 10 minutes; only a label in `fleet tick`/`fleet ls`, nothing is stopped | `live` |
| PARKED | compacted, then CLI stopped (C-c twice, SIGTERM after `FLEET_PARK_WAIT`), window gone, entry and session kept, resume command saved | `parked` |
| DONE / archived | finished for good; kept in the roster file and in `archive/roster-<ts>.json` copies | `done` / `archived` |

- **Park** a worker when its goal is achieved, or its goal was stopped for more than 10 minutes. Never park route, decide, watch, learn or auth (or a roster entry still carrying a retired role); `fleet park` refuses a standing desk outright. A worker with no goal is parked only by the orchestrator's decision. An unread inbox message younger than 1 hour blocks a park.
- **Compact before park.** `fleet park` types `/compact` into the lane and waits (up to `FLEET_COMPACT_WAIT`, 600 s) for the CLI's own record of a finished compaction: a `compact_boundary` line in the Claude transcript, a `compacted` line in the Codex rollout, a completed `summary` message in the OpenCode session, a `tombstone` line with `kind` `summarization` in the Kiro session's `messages.jsonl`, a `SYSTEM` `CHECKPOINT` row starting `# Resuming from a compaction` in the agy conversation's `transcript_full.jsonl` (PROVEN agy writes it on `/compact`). Only then does it stop the CLI, so a reopen resumes the compacted context. If no record appears, or the transcript can't be read, it refuses to park and leaves the lane running; `fleet park NAME --no-compact` parks without compacting. The roster keeps `compactedAt` next to `parkedAt`. PROVEN on Claude, Codex, OpenCode and Kiro, and for agy by the kit's live proof run (`fleet park` compacted, then `fleet send` reopened the same conversation id and the lane answered on it): after a compacted park, `fleet send` reopened the same session id and the lane named the word it had written before the compact.
- **Reopen** (`fleet reopen NAME`) resumes the same session id by default. `--fresh` starts a new lane from a handoff, and is for a reason only: context too big, transcript missing, or stale. If `$FLEET_STATE/handoff/NAME.md` is absent it is generated from the transcript (`fleet handoff NAME`).
- **Cleanup** (`fleet cleanup`, dry run) reports goal-less workers quiet for more than 2 hours, tmux windows with no roster lane, roster entries whose pane or pid is gone, and CLI processes naming a lane with no pane (by argv, or a live Claude process whose `sessions/<pid>.json` carries the lane's name). `--apply` parks and marks the first and third and archives the roster first; `--kill-orphans` is needed to kill the others. Section (6) lists side runs (`ORPHAN-SIDE S3 parent=L pid=1234 age=41m bound=30m why=parked`) whose parent is parked, done or gone, or that outlived their bound; `--apply --kill-orphans` kills them. `fleet park` and `fleet stop` kill the lane's running side runs first (`--keep-sides` leaves them alive as `orphan`).
- **Forget** (`fleet forget NAME`) archives one roster row with its inbox and cursor under `$FLEET_STATE/archive/forgotten/` and drops it, for a lane that is gone for good (a retired desk such as ORCHESTRATOR or DECISIONS, which `fleet tick` marks `forget` once its pane is dead). It refuses a lane whose pane is alive unless `--force`.

## Routing

- Messages go through `fleet send` / `peer_send` (fleet-mesh). Delivery always appends to the target's inbox, then wakes the lane. When the target is parked or done, delivery first runs `fleet reopen <to>` (timeout 180 s, `$FLEET_BIN` or `bin/fleet`), re-reads the roster, and wakes as usual; the result says `reopened`. A failed reopen still leaves the inbox line and says so.
- Decision answers go straight to the asker, not through ROUTE (the DECISIONS UI also sends ROUTE a copy; see below). Each answer (from `decision_answer` or `desk.sh answer`) is appended to `$FLEET_STATE/decisions-log.jsonl` as `{id, at, asker, verbatim, relayedTo}` (`decision_answer` adds `by`, the answering lane's `FLEET_NAME`), which ROUTE reads.
- New work, scope changes and launch or park requests go through ROUTE.

## Per-CLI matrix

Tags: **PROVEN** = run locally on 2026-11-18 (see Proven on below); **DOC** = current vendor docs; **UNVERIFIED** = not proven on these versions.

| | Claude Code | Codex CLI | OpenCode v1 | Kiro CLI | Antigravity CLI |
|---|---|---|---|---|---|
| Goal at launch | DOC first prompt `/goal <cond>`; the launcher waits for `Goal set:` in the transcript | PROVEN `codex --dangerously-bypass-approvals-and-sandbox "/goal ..."` set a goal; TUI showed Goal achieved; the `~/.codex/goals_1.sqlite` `thread_goals` row went `complete` | PROVEN `opencode --port P --prompt "/goal ... --max-turns N"`: plugin `goal_set` ok:true, title starts with a marker, state in `.opencode/goals/.../state.json` | PROVEN native `/goal [--max N] <objective>` (DOC default 5 iterations); the launcher rewrites `/goal ` to `/goal --max $FLEET_KIRO_GOAL_MAX ` (default 20); it runs as a `goal` workflow in a `wf-coder` child session and the launcher proves it by a `_kiro/workflow/run_start` line in the parent `messages.jsonl`; `fleet tick` showed GOAL `achieved` from the `_kiro/workflow/run_complete` status `completed` line; a goal stopped at max iterations read as `stopped` is UNVERIFIED (never observed) | PROVEN native `/goal <objective>` typed into the TUI: an `-i` first prompt is sent as plain text, never as a slash command, so goal mode waits for the idle footer `? for shortcuts`, types the line, waits 1 s, then presses Enter; agy expands it into a `USER_INPUT` row whose metadata says `/goal is a [Slash Command]`, which is what the launcher waits for. `fleet tick` reads `achieved`/`stopped` from `<!-- GOAL_COMPLETE -->` / `<!-- GOAL_CANCELLED -->` in `PLANNER_RESPONSE` rows only (the metadata quotes both markers); PROVEN by the kit's live proof run (agy 1.5.13): a goal-mode launch printed `goal=active` and `fleet tick` then read `achieved` |
| Name | `-n NAME` | PROVEN no `--name`; the launcher types `/rename NAME` + Enter via `tmux send-keys` | roster entry maps name to session id + port | PROVEN no `--name` used; `/title` only sets the terminal title (DOC); the roster maps name to session id (`sess_<uuid>`, read from `$KIRO_HOME/session-leases/<sid>.lease` whose pid descends from the pane pid; goal child sessions are skipped) | no name flag; `--log-file` gets `Created conversation <uuid>` at start (PROVEN) and the roster maps the name to that id; transcripts in `$FLEET_AGY_HOME/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript_full.jsonl` (PROVEN) |
| Send / list | fleet-mesh (`--mcp-config` per launch, stub-tested); native `SendMessage` / `ListAgents` too, mirrored into the mesh inbox by the role hook (PROVEN on hand-launched lanes) | PROVEN `codex queue --thread NAME --message T` delivered to a live idle TUI and started a turn; list via fleet-mesh | fleet-mesh MCP only (no native messaging; DOC v1 docs) | fleet-mesh MCP only (installed in `mcp.json`); PROVEN `fleet send` -> the idle lane ran `peer_inbox` and replied via `peer_send` into the sender's inbox | fleet-mesh MCP only (installed in `mcp_config.json`; PROVEN the server inherits the lane's env, so `FLEET_NAME`/`FLEET_STATE` reach it); PROVEN by the kit's live proof run (agy 1.5.13): a mesh send woke an idle lane, which ran `peer_inbox` and answered with `peer_send`, and its `decision_open` reached the desk inbox |
| Wake when idle | PROVEN pointer line as one bracketed paste (`tmux paste-buffer -p`, so vim NORMAL mode can't eat it, #38); the native socket holds it on bypass lanes, see INTERFACES.md Messaging model | `codex queue` (PROVEN) | PROVEN `POST localhost:P/session/ID/prompt_async` returns 204 and the lane did the work | PROVEN `tmux send-keys` typed pointer line plus a separate Enter (`wake=tmux send-keys ok=True`) started a turn on the idle lane | `tmux send-keys -l` pointer line, then a separate Enter (PROVEN typed text plus Enter starts a turn on an idle agy TUI) |
| Bypass | `--dangerously-skip-permissions` + the skip env var | `--dangerously-bypass-approvals-and-sandbox` | `"permission":"allow"` in its config (installer: `--bypass`) | `--trust-all-tools` per launch (PROVEN pane showed "Trust All Tools active, confirmations are off"); DOC deny rules still apply | `--dangerously-skip-permissions` per launch plus `--settings` with `toolPermission` always-proceed (PROVEN no prompts); DOC/PROVEN deny rules still apply |
| Trust step | needed for `/goal`; installer pre-trusts the dir (UNVERIFIED key name `hasTrustDialogAccepted`), else accept once | PROVEN the prompt appears in an untrusted dir; a `-c projects...trust_level` override did NOT skip it; installer writes `[projects."DIR"] trust_level = "trusted"` | none seen | PROVEN none beyond `--trust-workspace` per launch (skips "Trust this workspace?"; DOC it does not save trust); a fresh machine shows a trust-all warning defaulting to "No, exit" unless `chat.disableTrustAllConfirmation` is `true` (PROVEN under a scratch `KIRO_HOME`); the launcher fails loud if it appears | PROVEN `trustedWorkspaces` in the per-lane `--settings` skips the trust dialog (agy never saves it); `install/agy.sh --trust DIR` also writes it to `settings.json`; the launcher fails loud on the dialog |
| Install adds | rules + skills; fleet-mesh comes per launch (`--mesh` optionally registers it user-wide) | `codex mcp add fleet -- python3 .../fleet_mesh.py` + AGENTS.md rules | `opencode.json` `"mcp":{"fleet":{...}}` + goal plugin clone (public repo, pinned tag `v1.5.3`) + compact plugin clone (public repo, pinned tag `v0.1.2`) + rules | `steering/fleet-rules.md` (Kiro loads global steering `*.md`; PROVEN only that a probe saw global steering under the default agent, for the installed role agents UNVERIFIED), `settings/mcp.json` fleet server, `settings/cli.json`, `agents/fleet-<role>.json` | `config/rules/fleet-rules.md` (PROVEN agy loads global `rules/*.md` with `trigger: always_on`), `config/mcp_config.json` fleet server, `config/agents/fleet-<role>.md`, `config/skills/<name>/` (whether agy loads these skills is UNVERIFIED) |
| Auto-compact | the CLI's own (not tested here) | the CLI's own (not tested here) | PROVEN compact plugin: a kit-launched lane with limit 10,000 crossed at 14,979 tokens and wrote one completed `summary` message on its own (no `/compact` typed); the same launch without the plugin wrote none | the CLI's own (not tested here) | the CLI's own (not tested here) |

**Proven on:** Claude Code 2.1.327, codex-cli 0.166.0, OpenCode 1.19.0, kiro-cli 3.5.0 (v3 engine), agy 1.5.13 (single-feature probes, plus one full live proof run of install, launch, wake, park and reopen, all under an isolated `HOME`), tmux 3.6b, macOS. Linux: all test suites pass on Ubuntu (bash 5.3, tmux 3.6); live lanes there are UNVERIFIED (the Linux paths use `/proc/<pid>/environ`).

**Mesh is the mailbox, native is the doorbell.** Every message lands in the target's fleet-mesh inbox; a native channel only wakes the lane. A Claude lane's `SendMessage` to another fleet lane is copied into the mesh inbox by a `PostToolUse` hook in `roles/*.claude.json`. A2A is deferred. Details: INTERFACES.md, Messaging model.

### Waking a Claude, Kiro or agy lane from a non-Claude peer
**It never types into a box you are typing in.** Before typing, fleet-mesh reads the pane: the input row must be an empty prompt (only the prompt glyph and a dim placeholder) and no attached client may have pressed a key in the last 3 s. Otherwise the wake waits (a detached retry with backoff, up to 15 min) and then leaves a tmux notice and the pane option `@fleet_unread` instead of typing; the message is in the inbox either way. A Claude lane's `peer_send` to another live Claude lane types nothing at all: the sender follows up with its own `SendMessage`, which Claude Code delivers in the background. Details and the one residual race: INTERFACES.md, Typed wakes.

The decision desk pane never gets typed keys at all, empty input or not (`FLEET_NO_TYPE_ROLES`, default `decide`). Claude senders reach it only by native `SendMessage`; every other wake leaves a tmux notice and `@fleet_unread` instead.

A running fleet-mesh server picks up a kit upgrade by itself. Before its next request, it re-execs in place on the new code, keeping the same MCP session. If it can't, it stops typing wakes and leaves tmux notices instead. `fleet tick` lists any server still on old code, with its lane (INTERFACES.md, lib/fleet_mesh.py).

fleet-mesh types one line into the Claude (or Kiro, or agy) pane: `[fleet] message from X: run peer_inbox to read it`. Claude panes get it as one bracketed paste (`tmux set-buffer` + `paste-buffer -p`), because in Claude Code's vim NORMAL mode typed keys run as vim commands and ate the start of the line (#38); Kiro and agy get `tmux send-keys -l`. If the box then holds only a piece of the line, the kit clears that piece and retries (up to 3 tries), so no fragment is left behind. A lane `fleet ls` shows as `mesh=no` (not started by `fleet launch`, so no fleet MCP and no `peer_inbox`) gets ` (no peer_inbox tool? read $FLEET_STATE/inbox/<name>.jsonl)` appended. It arrives as typed input, so it carries a pointer only, never the message body, and the rules tell lanes to treat it as a pointer. This is the chosen default; a development channel (blocked on managed org plans per the docs) and "no wake, check `peer_inbox` at phase ends" are the alternatives. PROVEN on a live idle Claude 2.1.330 bypass lane: `fleet_mesh.py send` typed the pointer, the lane ran `peer_inbox` (fleet MCP from `--mcp-config`) and repeated the body. Claude's own messaging socket is not used for this: it holds a non-Claude sender's message on a bypass lane (see INTERFACES.md). PROVEN for Kiro: the same typed pointer line plus Enter woke an idle Kiro lane, which ran `peer_inbox` and answered through `peer_send`. agy gets the same line as literal keys (`send-keys -l`) plus a separate Enter; PROVEN by the kit's live proof run (agy 1.5.13): the woken lane ran `peer_inbox` and replied through `peer_send`.

## Environment

All variables are optional. The full table (`FLEET_TMUX_SESSION`, `FLEET_TMUX_SOCKET`, `FLEET_STATE`, `FLEET_DIR`, `FLEET_HUMAN`, `FLEET_DECIDE`, `FLEET_ROUTE`, `FLEET_NAME`, `FLEET_CLAUDE_BIN`, `FLEET_CODEX_BIN`, `FLEET_OPENCODE_BIN`, `FLEET_KIRO_BIN`, `KIRO_HOME`, `FLEET_KIRO_GOAL_MAX`, `FLEET_AGY_BIN`, `FLEET_AGY_HOME`, `FLEET_GFX`, `FLEET_CHAFA_BIN`, `FLEET_DESK_PASSTHROUGH`, `FLEET_STATUS_LABEL`) and the state layout are in [INTERFACES.md](INTERFACES.md).

Installer-only: `FLEET_OC_GOAL_PLUGIN_URL`, `FLEET_OC_GOAL_PLUGIN_REF`, `FLEET_OC_CONFIG_DIR`, `FLEET_PREFIX`, `FLEET_ROLES_DIR` (default `<repo>/roles`), `CODEX_HOME` (default `~/.codex`). Kiro: `FLEET_KIRO_BIN` (default `kiro-cli`), `KIRO_HOME` (default `~/.kiro`; passed to kiro lanes and read by the installer and lifecycle when set), `FLEET_KIRO_GOAL_MAX` (default 20, the `--max` of `/goal`). agy: `FLEET_AGY_BIN` (default `agy`), `FLEET_AGY_HOME` (default `~/.gemini`; where the installer writes and where launcher and lifecycle read transcripts; agy itself always uses `$HOME/.gemini`). Runtime: `FLEET_BIN` (the `fleet` script fleet-mesh runs to reopen a parked lane; default `<repo>/bin/fleet`).

## Status label

The fleet session shows tmux's own `status-left` unless you set one. `fleet status-label` changes it for the fleet session only (`set-option -t`, never `-g`):

```bash
fleet status-label '[#{session_name}:#{window_index}] '   # save to $FLEET_STATE/status-label and apply now
fleet status-label                                        # print "saved: ..." and "live: ..."
fleet status-label --reset                                # delete the file, unset both options on the session
```

The format is saved verbatim (a trailing space stays). `status-left-length` is set to the rendered width plus 1 (counted in characters, never below 10), so a long label is not cut off. `FLEET_STATUS_LABEL` overrides the file. Every `fleet launch` re-applies the label right after the session exists, so it survives reattach and a tmux server restart. With no file and no env the kit makes no status option call, so the default is unchanged. With no session yet, the label is saved and applies on the next launch. Test: `bash test/test_status_label.sh`.

## Goal files

```
fleet-goal new <goal.json> --goal "<text>" [--report-to NAME]
fleet-goal check <goal.json>        first line OK or FAIL <path>: <why>; exit 0/1
fleet-goal condition <goal.json> [--report-to NAME] [--turns N]
```
`check` needs `goal` (a string, or an object with a non-empty `title` or `objective`), `phases[{id}]` (`title` optional), `nextAction`, `done[]`, `openCalls[{id,question,status}]` (calls waiting on DECIDE, each carrying its Q id; a file that still uses the legacy name `openForHuman` is read as is, one of the two per file), `decisions[{id,decision}]`, and ISO-8601 `at` stamps where present. `condition` prints a `/goal` line under 4000 characters that names the file, says the file wins, requires one `GATE <id> PASS` line per `gates` key plus the `fleet-goal check` OK line after the last edit, has a `GOAL HALT: <reason>` exit, a turn bound, and ends the turn when only `openCalls` items remain. A FAIL line names the field and what is missing (`phases[0]: missing id`, `openCalls[2]: missing status`).

## Fleet UI hub

One loopback page for every desk, on one port with one token (Q700). Each desk page is a tab, mounted from the page's own handler, plus a read-only ROUTE board:

```bash
fleet ui serve --open     # http://127.0.0.1:7343/?t=<token>: tabs /decide/ /learn/ /watch/ /route/ (/ goes to /decide/)
fleet ui url [--tab T]    # the hub URL (token included) while it answers, else exit 3
```

- **Tabs**: `/decide/` (the DECISIONS UI below, with its live updates and `notifyCommand`), `/learn/` (the LEARN page), `/watch/` (the WATCH page), `/route/` (the roster and the last 30 answers in `decisions-log.jsonl`, read only; in the pane: `fleet ls`). A tab strip on every page switches between them.
- **The old commands are aliases**: `fleet decisions serve`, `fleet learn serve` and `fleet watch serve` print the hub URL for their tab when the hub answers, else start the hub (their first stdout line keeps its `DECISIONS UI:` / `LEARN UI:` / `WATCH UI:` prefix, now with the tab URL). The standalone pages stay as modules (`python3 lib/fleet_learn.py serve`, `python3 lib/fleet_decisions.py serve`, `python3 lib/fleet_watch_serve.py`), which their own tests use.
- **Security**: every page's rules, once. Loopback bind only, a fresh token per launch (`?t=` for page loads and SSE, the `X-Fleet-Token` header for every POST), Host and Origin checks, lane text escaped or rendered with `textContent`, the token kept out of the request log, the URL in `$FLEET_STATE/ui.url` (mode 0600, removed on exit). The guard is LEARN's human-only check: any `FLEET_ROLE` is refused (exit 4), `decide` and `watch` included, since one token opens every tab.
- **Port**: `--port`, else `$FLEET_STATE/app.json` `{"ports": {"ui": N}}`, else 7343 (7340-7342 stay free for the standalone pages).

## DECISIONS UI

A second front end on the desk's own state for answering calls without typing into the desk pane. It reads `decisions.jsonl`, the desk goal file, `decisions-log.jsonl` and the roster, and writes only through the desk's paths (`fleet_mesh` mark and deliver, `desk.sh answer`), so a pick in the UI and an answer in the desk pane end in the same state (tested).

```bash
fleet decisions serve --open          # the hub's DECIDE tab, http://127.0.0.1:7343/decide/?t=<token> (see Fleet UI hub)
fleet decisions tui                   # terminal fallback; j/k move, a/b/c pick, o own answer, r refresh all, v called list, ? help
fleet decisions pick Q12 a            # or --text "own words" | --edit "changed rec" | --refuse "why"
fleet decisions ask "Ship it?" --options "yes (rec),no" --wait --timeout 30m   # from a lane: exit 0 picked, 1 refused or own answer, 2 no answer
fleet decisions find "ship it"        # past answers; an exact repeat of an answered call is answered from the log with its quote
fleet decisions report Q12 ok "merged"   # the asker says whether it acted on the answer
```

- **Open calls** are cards, newest on top: the recommended option first and marked `rec`, each option's downside, an own-answer box (Enter sends), and `also asked by` when other lanes asked the same thing (relay targets plus open duplicates of the same question). A click greys the card at once; a pick is idempotent on the call id, so a second click, a second tab or the desk pane answering first relays nothing more (HTTP 409, CLI exit 3).
- **Relay at click time** to every asker, plus a copy to `$FLEET_ROUTE` (`copyOrchestrator`, default on). One log line per pick in `decisions-log.jsonl`: `{id, at, asker, verbatim, relayedTo}` plus `via:"ui"`, `frontend` (web, cli or tui), `kind` (pick, own, edit, refuse, found), `copiedTo` and `delivery` (each target's own `deliver()` result string).
- **Called list** badges come from those strings: `relayed HH:MM`, `queued (no live lane)`, `reopened`, or `relay FAILED` with a Retry button. Retry wakes the failed or queued targets again without writing a second inbox line. `decision_report {id, ok, note}` (or `fleet decisions report`) adds `acted on` / `act failed`.
- **New calls** slide in, the tab title shows `(n) DECISIONS`, and a desktop notification (Notify button) focuses the card on click. `notifyCommand` in the config runs on each new call while the server is up (`notify: false` turns both off) with `{title}`, `{body}`, `{url}` and `{id}` filled in; `{url}` deep-links to the card (`...?t=<token>#Q12`).
- **Refresh all** asks ROUTE to have every live lane re-sync, then looks for answers given elsewhere in `refreshSources`. Results go to an A/B page: keep A (the call stays open) or use B (relay the found answer). B needs a second Confirm click, enforced by the server, and rows nobody approves stay open. A B whose quote is not found verbatim in its named source is dropped and counted as `ungrounded: n`.
- **Live updates** over SSE (`/events`), with a 1 s poll fallback.
- **Security**: binds loopback only (`--bind` accepts 127.0.0.1, ::1 or localhost and refuses anything else), a random token per launch (`?t=` for page loads and SSE, the `X-Fleet-Token` header for every POST), Host and Origin checks, lane text rendered with `textContent` only, the token kept out of the request log, and the URL written to `$FLEET_STATE/decisions-ui.url` (mode 0600). Only the human answers calls: with `FLEET_ROLE` set to anything but `desk`, `serve`, `pick` and `tui` refuse (exit 4).
- **tmux popup**: `bind C-d display-popup -E "fleet decisions tui --one"` answers the newest call in a popup, with `gum` when it is on PATH, else curses.
- **Per-call permissions**: `decision_open {question, options, allow: [pick, edit, own, snooze, refuse]}` limits what the card offers (default: all), and `pick`/`snooze` refuse anything not allowed, whichever front end sends it.
- **Same id, two calls**: if the desk goal file and `decisions.jsonl` hold different questions under one id, the card says so and offers no answer buttons; answer it in the desk pane.
- **Limits**: the role guard reads `FLEET_ROLE`, so it stops mistakes, not a lane that clears its own env on purpose. Refresh approvals in the TUI go through `fleet decisions refresh choose <id> B` then `refresh confirm <id>` (the `r` key prints both).

Config `$FLEET_STATE/decisions-ui.json` (all optional):

```json
{"port": 7340, "notify": true, "notifyCommand": ["terminal-notifier", "-title", "{title}", "-message", "{body}", "-open", "{url}"],
 "copyOrchestrator": true,
 "refreshSources": [{"kind": "jsonl", "path": "$FLEET_STATE/decisions-log.jsonl", "label": "decisions log"},
                    {"kind": "command", "run": "my-chat-export --since 1d", "label": "chat"},
                    {"kind": "candidates", "run": "my-matcher", "label": "matcher"}]}
```

`jsonl` and `command` sources yield lines (JSON with `text`, `at`, `id`, or plain text) that name a call id; `candidates` yields `{id, quote, path, kind}` rows whose quote must appear verbatim in `path`. Python 3.8+ stdlib only, one static HTML file (`lib/decisions-ui.html`).

## LEARN page

`fleet learn serve` is the web front end on the LEARN desk (phase 7): proof per origin, never blended ("fleet: 2 of 3
learned", "outside: 0 of 2"), score history per topic (one point per day with reviews: passes over reviews, as a
sparkline per topic and origin), the heard-only rows that are never taught as decided, the last 20 reviews, and the
same quiz as `fleet learn quiz`. It reads `fleet_learn.snapshot()` and `reviews()`, and answers through
`fleet_learn.review()`, the verb the terminal quiz calls, so an answer in the page and one in the TUI end in the same
state (tested: reviews, card schedules, `proof.json` and the snapshot compared, with a control that must differ).

    fleet learn serve --open      # the hub's LEARN tab, http://127.0.0.1:7343/learn/?t=<token> (see Fleet UI hub)

- **Lesson**: the first topic waiting, terms (with a part-of map) then rows in context, each with its quote and
  source; "Seen" records it (POST `/api/seen`, token and the human only) and its cards join the quiz.
- **Quiz**: due cards in the TUI's order. Recall: type from memory, Reveal shows the source quote, then rate
  again / hard / good / easy. Choice: click one of four. Source check: paste the line; it must be a verbatim substring
  of the quote. Only due cards take an answer, so a double click or a second tab is a 409 and writes nothing.
  The page never sees a card's answer before it is graded (recall reveals only after the attempt).
- **Security**: as the DECISIONS UI. Loopback bind only (127.0.0.1, ::1 or localhost), a random token per launch
  (`?t=` for page loads, the `X-Fleet-Token` header for every POST), Host and Origin checks, text rendered with
  `textContent` only, the token kept out of the request log, the URL written to `$FLEET_STATE/learn-ui.url` (mode
  0600). Only the human takes quizzes: with `FLEET_ROLE` set to anything, `serve` refuses (exit 4), since a lane
  answering cards would fake proof of learning.
- **Port**: `--port`, else `servePort` in `$FLEET_STATE/learn/learn.json`, else 7341.

## Fleet Desk (Mac app)

The desk pages (DECISIONS, LEARN with its Lesson tab, WATCH, and the ROUTE board) in their own window, with a Dock icon and native notifications. It is a Tauri v2 shell in `app/desk-ui`: it forks none of the pages. It runs `fleet app` verbs and points its window at the tab you pick on the one `fleet ui serve` hub address, so every app action has a pane command (Q698: the UI is a superset of the pane, the pane keeps the full text for mosh).

```bash
fleet app build    # unsigned local Fleet Desk.app (bunx @tauri-apps/cli, Rust via cargo); never signed, notarized or published
fleet app open     # opens it in the background
```

| In the app | In the pane |
| --- | --- |
| Home page, Desks menu (Cmd-1 Decisions, Cmd-2 Learn, Cmd-3 Lesson, Cmd-4 Watch, Cmd-5 Route, Cmd-0 Home, Cmd-R reload) | `fleet app urls`, then `fleet decisions tui`, `fleet learn lessons` / `quiz`, `fleet learn lesson`, `fleet watch status`, `fleet ls` |
| Starts the hub when it is not answering | `fleet app serve` (detached; log in `$FLEET_STATE/app-ui.log`) |
| A native notification per new DECIDE call and per topic with new lessons waiting | `fleet app notify` (`--dry-run` shows without recording) |
| The phone tunnel on the home page | `fleet app tunnel [user@host]` |

Notifications are deduped in `$FLEET_STATE/app-notified.json`: calls key on the call id, lessons on each lesson id, so a restart never re-fires and a topic fires again only for a lesson not notified before. More than 4 new items in one poll (every 20 s) become one summary. When `decisions-ui.json` sets `notifyCommand` and the hub is up, its DECIDE tab already notifies calls, so the app stays quiet on calls. macOS asks once to allow Fleet Desk notifications; until you allow them, banners are dropped (the calls and lessons stay on their pages and in the pane), and the running app needs one relaunch after you click Allow before banners show. Every `fleet app` verb is the human's (any `FLEET_ROLE` is refused: the URLs carry the page tokens). Port: `$FLEET_STATE/app.json` `{"ports": {"ui": N}}`, else 7343. The menu is the desk list `fleet app urls` prints, so the Route entry needs no app rebuild. The app finds the kit through `FLEET_KIT_ROOT`, else the checkout it was built from, and drops `FLEET_ROLE` when it runs `fleet app`.

**From the phone.** The hub binds 127.0.0.1 only, and that stays. Forward the same port number over ssh (the hub checks the Host header against `127.0.0.1:<its port>`, so another local port gets 403), bound to 127.0.0.1 on the phone too:

```bash
ssh -N -L 127.0.0.1:7343:127.0.0.1:7343 <user>@<overlay-ip>
```

`fleet app tunnel <user>@<overlay-ip>` prints that line with the real port, then the tab URLs (token included) to open in the phone browser. The token changes when the hub restarts. The ssh setup is the one in Remote attach below.

## Side runs

A lane starts codex, agy, gemini or any other CLI outside the fleet's panes with `fleet side run`, which launches it
detached and registers it as the lane's child in one step (contract: `INTERFACES.md`, "Side runs"):

```bash
fleet side run --host codex --model gpt-x --bound 30m -- codex exec "fix the flaky test"
# S3 pid=4821 log=$FLEET_STATE/sides/S3/log result=$FLEET_STATE/sides/S3/result.json
fleet side ls          # S3 running ... then done/failed with exit and tokens
```

## New computer

One command sets up everything that needs no root, then writes the root steps to a script for you to run yourself:

```bash
curl -fsSL https://raw.githubusercontent.com/sblattj/tmux-agent-fleet-kit/main/bootstrap.sh | bash
curl -fsSL https://raw.githubusercontent.com/sblattj/tmux-agent-fleet-kit/main/bootstrap.sh | bash -s -- --dry-run   # print every step, change nothing
```

Piped from curl, it clones the kit to `~/.local/share/tmux-agent-fleet-kit` (`FLEET_KIT_DIR` overrides it; an existing clone is reused, never pulled), then runs that clone's `bootstrap.sh`. The whole script is one function called on its last line, so a cut-off download runs nothing. From a checkout, `fleet bootstrap` runs the same steps with the same flags: `--dry-run`, `--verify`, `--no-desks`, `--cli C`, `--pubkey KEY|FILE`. It is idempotent and prints every step.

**The no-sudo half**, in order:
1. `brew install` whichever of `tmux`, `mosh` and `jq` are missing. No Docker: if you want containers, use OrbStack.
2. Each agent CLI it finds (`claude`, `codex`, `opencode`, `kiro-cli`, `agy`) gets its `install/<cli>.sh --no-desks`. A CLI with no installer (gemini) is listed and skipped.
3. Puts Homebrew's bin dir on the non-interactive ssh PATH (what a phone's `ssh host cmd` and mosh's server start get). It appends one marked block to `~/.zshenv`:
   ```bash
   # >>> fleet bootstrap: brew bin on the non-interactive ssh PATH (tmux-agent-fleet-kit) >>>
   export PATH="/opt/homebrew/bin:$PATH"
   # <<< fleet bootstrap <<<
   ```
   A second run finds the block and adds nothing. If `~/.zshenv` is a symlink, or looks managed by a dotfiles tool (chezmoi, yadm, stow, home-manager, nix-darwin, "managed by", "do not edit"), it prints the line for you to add and leaves the file alone. With a non-zsh login shell it prints the line too.
4. With `--pubkey`, appends the phone's key to `~/.ssh/authorized_keys` (`fleet remote add-key`).
5. Brings the desks up once, the same way every installer ends: `fleet desks up --cli <first CLI found>` (`--no-desks` or `FLEET_NO_DESKS=1` skips it).

**The sudo half** never runs here. Bootstrap writes `~/.local/state/fleet/bootstrap-root.sh` (`fleet remote root-script FILE`) and prints the line to run it:

```bash
! sudo bash ~/.local/state/fleet/bootstrap-root.sh
fleet bootstrap --verify
```

The script does what `fleet remote setup` does as root, in the same safe order: host keys if missing; the key-only sshd drop-in, checked with `sshd -t` and `sshd -T` and rolled back on failure, before sshd is switched on; Remote Login on (systemsetup, then launchctl); and the application-firewall allow for the resolved `mosh-server` binary, which is what lets mosh's UDP 60000-61000 in on macOS. On Linux it enables ssh with systemd and opens UDP 60000-61000 in ufw or firewalld when one is active. Rerun it after upgrading mosh.

`fleet bootstrap --verify` (same as `fleet remote verify`) checks the effect without privileges and exits non-zero on any failure: sshd listening, the drop-in present, a bare `ssh localhost tmux -V` with your own key (skipped when no `~/.ssh/id_*` key is in `authorized_keys`), `mosh-server` on the non-interactive ssh PATH, and on macOS the firewall allow.

**Blink Shell (iOS) host.** In Blink, `config` > Hosts > New:

| Field | Value |
|---|---|
| Alias | a short name, for example `fleet` |
| Hostname | the host's ZeroTier or Tailscale IP (`fleet remote status` prints it) |
| Port | `22` |
| User | your login on the host (`id -un`) |
| Key | a key generated in Blink (Keys > New); copy its public half to the host and run `fleet bootstrap --pubkey '<key>'` |
| Mosh Server | the full `mosh-server` path, for example `/opt/homebrew/bin/mosh-server` (`fleet remote attach-line` shows it) |

Then `mosh fleet` from Blink, and attach with the line bootstrap prints last (`fleet remote attach-line`):

```bash
tmux attach -t fleet
```

With `window-size latest` (setup sets it), the window follows whichever device typed last, so the laptop can stay attached while you type on the phone. Tip: to keep a phone from resizing the window for everyone else, attach it with `tmux attach -f ignore-size -t fleet` instead.

## Remote attach (phone or tablet)

`fleet remote` lets you follow a run, answer DECIDE or read a lane from a phone or tablet (Blink Shell, Termius) over ssh + mosh on a private overlay network: ZeroTier, Tailscale, or any VPN that gives this host a private address. It never opens anything to the public internet itself.

```bash
fleet remote setup --pubkey ~/phone.pub --dry-run   # print every step, change nothing
fleet remote setup --pubkey ~/phone.pub             # idempotent; run it again after upgrading mosh
fleet remote status                                 # read-only checks
fleet remote qr [--read-only]                       # the attach command (and a QR code with qrencode installed)
fleet remote off [--keep-sshd]                      # remove the drop-in; sshd off unless it was on before setup
```

`setup`, in order:
1. Appends the key to `~/.ssh/authorized_keys` once (dir 700, file 600).
2. Generates the sshd host keys if missing (macOS has none until Remote Login has run once).
3. Writes the key-only drop-in `sshd_config.d/010-fleet-remote-keyonly.conf`, before sshd is switched on, so passwords are never open. It checks `sshd -t`, plus `sshd -T` for `passwordauthentication no`; if either fails, it rolls the drop-in back and leaves sshd alone.
4. Turns sshd on: macOS Remote Login, or systemd on Linux.
5. Allows the resolved `mosh-server` binary in the macOS application firewall. A package upgrade moves that path, so rerun setup afterwards.
6. Sets `window-size latest` on the fleet tmux server.

`fleet remote root-script FILE`, `verify`, `ssh-path [--check|--brew-bin]`, `add-key KEY` and `attach-line` are the pieces `fleet bootstrap` uses (see New computer); they never run sudo.

Root steps go through sudo, with an askpass dialog when there is no TTY. The overlay address comes from `tailscale ip -4`, then from a ZeroTier interface (`zt*`, `feth*`), then from an address in 100.64.0.0/10. `--ip` or `FLEET_REMOTE_IP` overrides it. When `mosh-server` is outside a non-login ssh PATH (for example Homebrew), the attach command adds `--server=<path>` and the full tmux path. The phone attach line is the last line `setup` prints.

**The non-interactive ssh PATH.** A phone's `ssh host cmd` (and mosh's server start) runs with sshd's default PATH, `/usr/bin:/bin:/usr/sbin:/sbin`, plus whatever the login shell's non-interactive startup adds (zsh reads `~/.zshenv`; `bash -c` reads nothing). Homebrew's bin dir (`/opt/homebrew/bin`, or `$(brew --prefix)/bin`) is not on it. `setup` and `status` check both tools there; when one is missing, `setup` prints the line to add yourself (it never edits shell files):

```bash
export PATH="/opt/homebrew/bin:$PATH"   # in ~/.zshenv (zsh), the directory `setup` printed
```

Check it from the phone (or any other machine) before the first attach; it prints the tmux version, and `command not found` means the PATH line is still missing:

```bash
ssh <user>@<overlay-ip> 'tmux -V'
```

**Never expose the tmux socket over raw TCP** (socat, ncat, port forwards). Anyone who reaches that port runs commands as you, with no authentication. Use ssh/mosh only.

Notes:
- sshd listens on every interface, not just the overlay. Key-only auth is the guard, and `status` says so.
- A sleeping host drops the session. Use `caffeinate -dims` (macOS) or `systemd-inhibit` (Linux).
- On Linux, `setup` leaves the host firewall alone. If one is on, allow UDP 60000-61000 on the overlay interface.
- The Linux sshd step is not covered by tests.

## Graphics in panes

`fleet show IMAGE [--text SUMMARY]` puts a chart or a diagram in the pane it runs in, the best way every viewer can see it, and always prints one text line after it (SUMMARY, else the sidecar `IMAGE-without-extension.txt`, else `image: <file name>`). It needs [chafa](https://hpjansson.org/chafa/) (`brew install chafa`); without it you get the text line only. On stderr it says what the human saw: `fleet-show: mode=<kitty|symbols|text> (<why>)`.

| Viewer | What it sees |
|---|---|
| Ghostty or kitty, no tmux | the real image |
| Ghostty or kitty in tmux, passthrough on (the kit default) | the real image (Kitty unicode placeholders: text cells tmux moves and clips, so splits and window switches are fine) |
| Ghostty or kitty in tmux, passthrough off (`FLEET_DESK_PASSTHROUGH=0`, or set off by hand) | colored block art, plus a hint |
| any client attached over mosh (phone, tablet) | colored block art for everyone on that pane (mosh carries no images, and one pane shows one output) |
| other terminals (Terminal.app, iTerm2, xterm, ...) | colored block art |

`FLEET_GFX=auto|kitty|symbols|text` overrides the choice (default `auto`). A mosh viewer is found by walking each tmux client's parent processes for `mosh-server`, since its TERM says nothing.

`fleet show` never changes a tmux option. chafa does on its own: when it finds `allow-passthrough` off inside tmux it runs `tmux set-option allow-passthrough on` with no target (it lands on the session's active window), and leaves it on if it is killed early. So every chafa call runs behind `lib/fleet-show-shim/tmux`, which drops `set`, `set-option`, `setw` and `set-window-option`. Agent shells that inherit `TMUX` from a fleet pane should be careful with any tool that talks to tmux.

Passthrough is on by default (since v0.8.1). `fleet launch` and `fleet desks up` run `tmux set-option -g allow-passthrough on` on the fleet's tmux server, so every pane there (desks, the WATCH status pane, lanes) can draw real images. It is a server-wide option: with no `FLEET_TMUX_SOCKET` the fleet shares your default tmux server, so your other sessions on it get it too. The kit only ever turns it on: it never writes `off`, never unsets it and never uses `all` (placeholders don't need `all`, and `all` lets hidden panes paint).

Opt out with `FLEET_DESK_PASSTHROUGH=0` (any value other than `1` does the same): no passthrough call at all, the v0.8.0 behavior byte for byte. An opt-out doesn't turn off a global that's already on; run `tmux set -g allow-passthrough off` yourself for that.

On mosh, `fleet show` in auto mode picks block art even with passthrough on, and that's expected: mosh only carries text, so Kitty graphics never reach the screen. A client attached over mosh (Blink, for example) shows the symbols version; a local Ghostty or Kitty client shows the real image. If any attached client is on mosh, auto mode picks symbols for everyone on that pane. `FLEET_GFX=kitty` forces the image path, but a mosh client still shows nothing there.

## Tests

```bash
python3 -m unittest test/test_listen.py # WATCH UI: parsing, routing, secrets, classifier contract, sources, page
python3 -m unittest test/test_feed.py   # fleet feed: whosaid and ?from=N servers, resume, filler, batching, status notes, token never leaks
bash test/test_listen.sh      # WATCH UI in a real tmux pane on a private socket: layout, live filter, page, quit
python3 -m unittest test/test_mesh.py   # fleet-mesh delivery, reopen, decisions log
python3 -m unittest test/test_wake_splice.py   # typed wakes: input-box guard, and a vim NORMAL-mode fake Claude box gets the whole pointer (#38)
bash test/test_remote.sh     # fleet remote: sandboxed sshd/firewall stubs, tripwire sudo, private tmux socket; never touches this host
bash test/test_bootstrap.sh  # fleet bootstrap / curl|bash: throwaway HOME, stub brew and CLIs, tripwire sudo, root script run in a sandbox
python3 -m unittest test/test_mesh_stale.py   # stale mesh servers: re-exec in place keeps the stdio session, refuse typed wakes, tick lists them
python3 -m unittest test/test_decisions.py   # DECISIONS UI: cards, pick relay, badges, refresh A/B, server auth, tmux fallback
python3 -m unittest test/test_side_guard.py   # side runs: decision calls refused, peer_send only to the parent as <parent>/<id>
python3 -m unittest test/test_watch_serve.py   # WATCH page: loopback + token (403 without), page action = pane key, same state
python3 -m unittest test/test_probe.py test/test_probe_roles.py   # DECIDE probes: read verbs, calls, nuke/resume, stats, host allow-list
python3 -m unittest test/test_side_usage.py   # side runs: tokens from result or codex session, child count, roster line
python3 -m unittest test/test_side_roster.py  # side runs under the parent in peer_list, tick, ls; ended hidden, ORPHAN parents
python3 -m unittest test/test_side_ach.py     # side runs: adopt-ach registers running ach runs by process ancestry, once
bash test/test_probe_stall_park.sh            # a probe silent 15 min is parked as stalled by fleet cleanup --apply
bash test/test_forget.sh                      # fleet forget archives one row; a live lane is refused without --force
bash test/test_status_label.sh               # fleet status-label: default, set, sizing, reset, env, server restart (private tmux server)
bash test/test_desks_default.sh               # desks by default: installer step, launch guard, tick desks down, desks up --handoff
bash test/test_show.sh                        # fleet show: every mode, mosh viewer, shim drops chafa's set-option, FLEET_DESK_PASSTHROUGH default on, =0 opt-out, never off
FLEET_PROBE_LIVE=1 test/test_probe_live.sh --cli claude   # launches 2 real lanes: the probe can't shell or write, a worker can
FLEET_PROBE_ACH=1 test/test_probe_ach.sh                  # the same bar through ach on claude, opencode and kiro (model calls)
bash test/test_probe_opencode_tools.sh                     # OpenCode offers the fleet-probe verbs to fleet-probe only (FLEET_PROBE_OC_LIVE=1: live)
bash test/test_goal.sh        # fleet-goal and the desk helper (incl. decisions log), with a broken-file control
bash test/test_installs.sh    # installers against a throwaway HOME with stubbed CLIs
bash test/test_update.sh      # fleet update / tafk update: current, newer, dirty, diverged, offline, detached, copied tree (local bare origin, file:// release fixtures)
bash test/test_lifecycle.sh   # tick, park, reopen, handoff, cleanup (incl. side runs killed on park/stop, ORPHAN-SIDE) against stub CLIs and fixture transcripts
bash test/test_fleet.sh       # fleet launch (dry run + stub CLIs on a private tmux socket); run with FLEET_* unset
bash test/test_roles.sh       # role files and launch wiring; run with FLEET_* unset
bash test/test_launch_env.sh  # fleet launch --env, FLEET_STRIP_EXTRA, FLEET_PRELAUNCH_HOOK, env proof (issue #4)
bash test/test_privacy.sh     # no personal data in the repo (FLEET_PRIVACY_TERMS=a|b adds names)
python3 -m unittest test/test_contracts.py   # four-desks contracts (WATCH, probe) and the watch|learn|probe dispatch
bash test/test_learn.sh       # fleet learn: ledger, sync, quiz, 24 h proof with an injected clock, export
bash test/test_learn_lesson.sh   # teach before test: lesson, session, digest, brief, seen gate, guard, rebuild
bash test/test_learn_stacks.sh   # two card stacks: manual first, newest request on top, rebuild keeps them, autoPrompt
python3 -m unittest test/test_learn_sources.py   # whosaid + linear adapters on fakes, sources.json refs-only, rebuild from sources
python3 -m unittest test.test_fsrs test.test_explain   # FSRS against py-fsrs reference values; explain back grader
python3 -m unittest test/test_app.py   # Fleet Desk: human-only verbs, deduped notifications, serve, tunnel (same port, Host check), app vs pane parity
python3 -m unittest test/test_learn_serve.py   # LEARN page: token/loopback, page answer == TUI answer, score history per topic
python3 -m unittest test/test_watch.py   # WATCH engine: replay counts, scrub, dedupe, filters, quiet hours, literal-credential refusal
python3 -m unittest test/test_watch_rate.py   # WATCH per-source token bucket: wait, defer, X-RateLimit-Remaining drain (injected clock)
python3 -m unittest test/test_side.py   # side runs: fleet side start|end|ls|show, refusals, 20 parallel starts, idempotent end
python3 -m unittest test/test_side_mcp.py   # side runs over MCP: side_start|end|list via fleet_mesh.py serve, refusals as isError
python3 -m unittest test/test_side_watch.py   # side runs: watcher marks done/timeout/failed, kills past the bound, one parent message
python3 -m unittest test/test_side_run.py   # fleet side run -- CMD: detached, no tty, env contract, exit + tokens recorded
python3 -m unittest test/test_watch_ado.py test/test_watch_gitlab.py test/test_watch_confluence.py test/test_watch_github.py test/test_watch_jira.py test/test_watch_bitbucket.py   # WATCH adapters on recorded fixtures
python3 -m unittest test/test_watch_webhook.py   # WATCH webhook receiver: signatures, loopback-only bind, spool, webhook+poll dedupe
```

## Limits

- Needs tmux; no Windows support. macOS: test suites plus live lanes proven (bash 3.2, tmux, Claude Code 2.1.327, Codex 0.166.0, OpenCode 1.19.0, kiro-cli 3.5.0). Linux: every test suite passes on Ubuntu (bash 5.3, tmux 3.6); live CLI lanes on Linux are UNVERIFIED.
- Installers edit JSON with jq, so a JSONC `opencode.json` (comments) is refused with a message instead of being rewritten. Existing files are backed up as `<file>.fleet-bak.<timestamp>` before any change.
- Proofs the launcher runs (pid/env check, goal active) depend on CLI internals (session transcripts, a sqlite file, the OpenCode HTTP API) that can change between versions; `--no-proof` skips them.
- The OpenCode goal and compact plugins are cloned as-is at their pinned tags (the compact plugin has no dependencies); whether it needs a build or install step on your machine is UNVERIFIED here. If the plugin dir already holds an install from the plugin's own installer (a `package.json`, no `.git`), the installer reuses it and never clones over it; a non-empty dir with neither fails loud.
- Claude trust pre-seeding (`install/claude.sh --trust DIR --trust-only`) is PROVEN on Claude Code 2.1.327: lanes stuck at the trust dialog launched cleanly after it. The launcher fails loud (`FAIL ... workspace-trust dialog`) if a pane still sits at that dialog.
- Claude's Bypass Permissions accept dialog appeared on 2 of 12 lane starts in testing even with `skipDangerousModePermissionPrompt` set (cause UNVERIFIED). The launcher detects it and fails loud (`FAIL ... Bypass Permissions accept dialog`), including on `resume`; accept it in the pane (`fleet attach`) or stop and relaunch. A resume (`fleet reopen`, `fleet launch ... resume`) gets the same pane check for the workspace-trust dialog, and prints OK only once Claude has registered the process in `~/.claude/sessions/<pid>.json` (`FLEET_CLAUDE_HOME` overrides the dir); no registration within the proof timeout FAILs naming the file (issue #8).
- `opencode export` (1.19) truncates its JSON when stdout is a pipe, so `fleet handoff` writes it to a temp file first.
- Waking an idle Claude lane from a non-Claude peer relies on typed input into the pane (see above).
- Kiro: the role deny misses `sh -c "fleet park X"` (PROVEN it ran), so a Kiro role is not a hard fence. `KIRO_API_KEY` is passed through as-is; behaviour when it is set is UNVERIFIED. Whether Kiro's own auto-compaction writes the same `tombstone` marker is UNVERIFIED. A goal that stops at max iterations being read as `stopped` is UNVERIFIED. Live Kiro lanes on Linux are UNVERIFIED.
- agy: the role deny's reach beyond the named shell commands (edit tools, artifacts, `sh -c` wrappers) is UNVERIFIED, so an agy role is not a hard fence. agy has no config-dir override, so lanes share the user's `~/.gemini` sign-in and conversations. Whether agy's own auto-compaction writes the same `CHECKPOINT` marker is UNVERIFIED. Live agy lanes on Linux are UNVERIFIED.
- Nothing is pushed or sent anywhere by the kit; the only network calls are the two plugin `git clone`s in `install/opencode.sh` and the WATCH and LEARN sources you list in `watch.json` or `sources.json`.

## WATCH: the engine

```
fleet watch run [--once] [--force] [--source NAME] [--json]   poll due sources; route to DECIDE and LEARN, never ROUTE
fleet watch check [--config PATH] [--resolve]                   validate watch.json; exit 1 on a literal credential
fleet watch status [--once] [--interval S]                      terminal status pane (j/k select, p poll, z pause, r resume, h release held, q quit)
fleet watch stats [--json]                                      counts per kind, dropped per rule
fleet watch snapshot [--compact]                                snapshot() JSON
fleet watch poll-now|pause|resume NAME, fleet watch release-held   the pane's verbs (shared with `fleet watch serve`)
fleet watch webhook serve [--config F] [--bind A] [--port N]    optional loopback webhook receiver (7349)
fleet watch serve --open             # the hub's WATCH tab, http://127.0.0.1:7343/watch/?t=<token>: sources, counts, recent events; poll now, pause, resume, release held asks
```

Run `fleet watch status` as the WATCH window's non-LLM pane, and `fleet watch run` (the poll loop) in its own pane
or a launchd/systemd job; it sleeps until the next source is due (at most 60 s per sleep). Credentials in
`$FLEET_STATE/watch.json` are `env:`, `keychain:` or `cmd:` references only; `fleet watch check` exits 1 on a literal.

GitHub without a token: every request spends one of 60 per hour (a 304 too), so `pollSec` 900 fits about 15 quiet
repos. Past ~10 repos, add a read-only token: `"auth": {"type": "bearer", "token": "cmd:gh auth token"}`.

## WATCH webhooks (optional)

`fleet watch webhook serve` listens on 127.0.0.1 only (any other bind is refused). It never opens a public port;
expose it through your own tunnel if you want push. Each `webhook` source in watch.json is one route,
`POST /hook/<name>`, with its HMAC secret given by reference. A delivery with a bad signature is refused. A good one
lands in the same pipeline as the poll, so a webhook plus the next catch-up poll of the same change make one event.

## WATCH UI

`fleet listen` opens a tmux pane that follows a live call or chat, shows the transcript filtered to the speakers you care about, and routes what it hears. It consumes text only: bring your own transcriber or meeting exporter.

```
fleet listen [--source file|stdin|command|replay|teams-transcript ARG] [--speakers "A,B"] [--config FILE] [--speed N]
             [--page [--port N]] [--pane-right-of NAME | --here] [--plain] [--exit-at-eof]
```

```
+-- WATCH: weekly sync ---------------------------------------------------- 00:23:41 +
| filter: Alice_Example, Bob_Example   [f]ilter  [p]ause  [m]ark  [o] page  [q]uit  [?] |
+------------------------------------------------------------------------------------+
| 15:22:58 Alice_Example  can we get the retry change into this week?               |
|          -> ASK  sent to DECIDE as Q1                                             |
| 15:23:10 Bob_Example    I'll need the staging numbers before Friday               |
|          -> ACTION  sent to ROUTE (owner: unresolved)                             |
| 15:23:55 Alice_Example  and the dashboard ...          (unconfirmed, dimmed)      |
+------------------------------------------------------------------------------------+
| SAY THIS                                                                           |
|  * p95 went from 840 ms to 310 ms on the last run (lane-a, 11:40)                  |
+------------------------------------------------------------------------------------+
| routed: 1 ask, 1 action | unsure: 0 | source: command ok | classifier 40 ms | [o] page |
+------------------------------------------------------------------------------------+
```

| Heard | Goes to | How |
|---|---|---|
| A question or request aimed at you | DECIDE | `decision_open` with the quote, speaker and time |
| A commitment or task | ROUTE | mesh inbox, batched (`batchSec`, default 5), `owner: <lane>` or `owner: unresolved`, `due:` when said |
| A question the fleet can answer now | SAY THIS box | from `facts` or `factFiles` in the config, with the source |
| A decision made on the call | ROUTE + `decisions-log.jsonl` | `kind: decided-on-call`, `confirmed: false`; never closes a desk call |
| Unsure (hedged) | ROUTE | marked `uncertain` |
| A spoken or pasted secret | nowhere | the pane says one appeared; the text is dropped before any classifier sees it |
| Anything else | nowhere | no message |

- Only final lines are routed (`final: true`, or WhisperLive-style `completed: true`); the unconfirmed tail shows dimmed.
- Every routed ask, action or decision must quote the transcript verbatim, or it is not sent (counted as `ungrounded`). Duplicates are dropped.
- Sources: `file` follows a growing or rewritten-in-place transcript and emits each line once (a `{"note": "..."}` line becomes a pane note, never routed; `fleet feed` writes its status that way); `stdin`; `command` runs any command printing one utterance per line; `replay` plays a saved transcript (`--speed 1` real time, `0` no delay); `teams-transcript` follows the live transcript panel of a Teams call through whosaid (below). A line is JSON (`{"at","speaker","text","final"}`, `{"start","end","speaker","text","completed"}` or `{"clock","name","text"}`) or text `[hh:mm:ss] Speaker: text`. `speakerAliases` maps diarizer labels (`SPEAKER_00`) to names.
- A source failure shows once in the status bar and the pane keeps running.
- `teams-transcript` runs `whosaid teams transcript-live --jsonl` (a numeric ARG adds `--port ARG`; any other ARG or `source.run` replaces the command) through the command adapter. It needs a whosaid release with that command. whosaid only reads the panel. Its status (`ok`, `panel closed`, `window not found`) shows in the status bar once per change. A closed panel (it closes when you rejoin) also adds one note naming `whosaid teams open-transcript`, the one allow-listed click that reopens it. A gap (rows that scrolled past between two reads) adds a note. The panel has no speaker names, so lines show as `unknown` with a one-time note. No speaker filter can match them, so they stay visible under a filter but are routed only when no filter is set.
- The speaker filter decides what the pane and page show and what gets routed; it is applied at draw time, so widening it brings earlier lines back. `me` lines always show and route (its own questions are not sent to the desk).
- Keys: `f` change the speaker filter live (the source keeps running), `p` freeze the view, `m` mark the moment, `o` start the explainer page, `?` help, `q` quit.
- `--page` (or `o`) serves a local explainer page on 127.0.0.1 only (stdlib HTTP, no build step): what is being discussed, glossary terms translated, the SAY THIS list, the calls and actions created, and "load more" for earlier lines.
- Placement: by default a new window in the fleet session; `--pane-right-of NAME` splits right of a lane; `--here` runs in the current terminal (`stdin` and `--plain` always do). The pane sends as `WATCH` (`--name`). It runs with `FLEET_ROLE=watch` and tags its tmux pane `@fleet-desk watch`, so `fleet cleanup` does not count its window as an orphan (the retired `@fleet-listener 1` tag is no longer skipped). `q`, SIGTERM and killing the pane all send the held batch and stop the source.

Config, `$FLEET_STATE/listener.json` (or `--config`; `//` comment lines allowed):
```jsonc
{
  "source": {"kind": "command", "run": "my-transcriber --live --jsonl"},
  "speakers": ["Alice_Example", "Bob_Example"],
  "me": "Human_Example",
  "speakerAliases": {"SPEAKER_00": "Alice_Example"},
  "route": {"actions": "ROUTE"},
  "classifier": {"kind": "command", "run": "my-llm-classify", "timeoutSec": 10, "maxTokens": 300,
                 "minIntervalSec": 5, "tailChars": 1800},
  "facts": [{"match": ["p95"], "say": "p95 went from 840 ms to 310 ms", "source": "lane-a, 11:40"}],
  "factFiles": ["~/notes/lane-reports/*.md"],
  "glossary": {"p95": "the time 95 of every 100 requests finish under"},
  "watchers": [{"name": "numbers", "trigger": "on keyword p95", "run": "my-numbers-lookup", "routeTo": "say"},
               {"name": "recap", "trigger": "every 30s", "run": "my-recap", "routeTo": "say"}]
}
```
### A call on another machine: `fleet feed`

When the transcriber runs on a different machine (a laptop on the call, the fleet on a dev box), `fleet feed` follows
the transcript that machine serves and mirrors it into a local file, which `fleet listen --source file` (and the WATCH
`call` source) then follow like any transcript:

```bash
fleet feed start http://laptop.example:8766 --token-file ~/.config/fleet/call-token   # whosaid live serve
# OK feed feed S4 pid=5120 whosaid http://laptop.example:8766
# transcript=$FLEET_STATE/feeds/feed/transcript.jsonl
# follow it: fleet listen --source file $FLEET_STATE/feeds/feed/transcript.jsonl
fleet listen --source file "$FLEET_STATE/feeds/feed/transcript.jsonl"
fleet feed ls              # feed  S4  alive  ok  whosaid http://laptop.example:8766
fleet feed status feed     # alive/dead, status, position, line count, transcript and log paths
fleet feed stop feed
```

```
fleet feed start URL --token-file PATH [--name N] [--protocol whosaid|lines] [--stem S] [--from-start]
                 [--since HH:MM:SS] [--min-chars 12] [--batch 60] [--poll 3] [--header X-Token]
                 [--parent LANE] [--bound 12h]
```

- Protocols. `whosaid` (default) is `whosaid live serve` (whosaid v1.52.0 or later): the newest session (or `--stem`),
  long-polled with its cursor, `Authorization: Bearer <token>`. `lines` is any server where `GET URL?from=N` returns
  the text lines `[HH:MM:SS] speaker: text` after the first N, with the token in `--header` (default `X-Token`);
  `--since HH:MM:SS` starts at the first line at or after that clock (the served file can hold more than one call).
- It starts at the current end of the call; `--from-start` replays the backlog. A whosaid session that starts after the
  feed is read from its first line.
- It runs detached under the side-run supervisor (`fleet side run --host feed`, so it shows in `fleet side ls` and
  outlives the pane or tool call that started it), for at most `--bound` (12h, the side-run cap). Its position is in
  `$FLEET_STATE/feeds/<name>/state.json`, saved only after the lines are written, so `fleet feed start` again with the
  same name resumes and never re-sends a line.
- Lines under `--min-chars` characters of text (default 12) are dropped as filler. Lines are written in one batch every
  `--batch` seconds (default 60; `0` writes each poll), so a long call does not flood DECIDE.
- A speaker the transcriber has not named yet (`them`) shows as `unknown` (one note says so); no speaker filter
  matches it, so it stays visible and is routed only with no filter. A name whosaid sets later on an earlier line is
  not rewritten: lines already written keep the speaker they had.
- Status `ok`, `stalled (3 failed polls)`, `auth refused`, `gone` (the call ended; it then waits for the next
  session) or `stopped` (`fleet feed stop`, the bound or a signal), once per change, never per failed poll: in `$FLEET_STATE/feeds/<name>/status`, the feed's log, and the
  listen pane as a NOTE (`source: http-feed stalled (3 failed polls)`), shown and never routed. A refused token
  (401/403) or a refused request (400) stops the feed; fix it and start it again.
- Read-only: GET only, redirects are not followed, nothing said on the call is acted on (asks and decisions go to
  DECIDE as usual).

Token rules:

- The token comes only from `--token-file`, read on each start; the file must be yours and mode 600 (`chmod 600`),
  else `start` refuses. There is no argv or env form, and a URL with `user:password@` is refused.
- The token never appears in stdout, the log, the status, the state, the transcript or a message, and no URL is
  printed with a query or credentials in it.
- Rotate a token that was ever pasted into a command (shell history, a chat, a tool call) after the call.

The default classifier is keyword rules (`classifier.rulesFile` adds regexes per `ask|action|decided|question`). A `command` classifier or watcher reads one JSON request on stdin (`utterances`, `tail` bounded to `tailChars`, `speakers`, `me`, `maxTokens`) and prints one item per line: `ASK:`, `ACTION:`, `SAY:`, `DECIDED:` or `NOTE:`, then the text, then optional ` | owner: X`, ` | due: Y`, ` | source: S`, ` | options: a; b`, ` | rec: a`, ` | uncertain`. Other lines are ignored and counted; a run that exits non-zero or times out routes nothing. It runs at most once per `minIntervalSec` (lines in between are batched into the next call), is killed after `timeoutSec`, and its latency shows in the status bar; any model call behind it needs its own token cap and deadline. Watcher triggers: `every Ns`, `on speaker X`, `on keyword K`; `routeTo: "say"` keeps only SAY and NOTE items.

## Contributing: issue first

Every change to the kit starts as an issue on this repo, and the commit that lands it references the issue
(`fixes #N` or `refs #N`). Issues are scrubbed before filing: no employer or customer names, people, ticket ids,
account ids, hostnames, URLs or local paths; describe the problem and the change in the kit's own terms. A change
that landed without one gets a backfilled issue, closed with a link to its commit.

## License

MIT, see [LICENSE](LICENSE).
