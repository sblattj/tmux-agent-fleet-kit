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
| FLEET_STATUS_LABEL | empty | tmux format for the fleet session's `status-left`; overrides `$FLEET_STATE/status-label` (issue #44); empty means use the file, and with no file the kit sets no status option |
| FLEET_UPDATE_TIMEOUT | `20` | seconds per network call of `fleet update` (git ls-remote / fetch, curl) |
| FLEET_UPDATE_REPO | `sblattj/tmux-agent-fleet-kit` | the GitHub repo a copied tree updates from |
| FLEET_UPDATE_API / FLEET_UPDATE_TARBALLS | `https://api.github.com/repos/$FLEET_UPDATE_REPO` / `https://github.com/$FLEET_UPDATE_REPO/archive/refs/tags` | a copied tree's release lookup (`$API/releases/latest`, `tag_name`) and tarball base (`$TARBALLS/vX.Y.Z.tar.gz`); tests use `file://` |
| FLEET_DIR | `$PWD` | working dir for a launched lane (`--dir` overrides) |
| FLEET_HUMAN | `the human` | who answers DECIDE calls |
| FLEET_DECIDE / FLEET_ROUTE | `DECIDE` / `ROUTE` | desk lane names; a send to the retired `DECISIONS` / `ORCHESTRATOR` goes to `DECIDE` / `ROUTE` when the roster has the new desk and no lane under the old name |
| FLEET_NAME | set by the launcher inside each window | the lane's own name (fleet_mesh.py reads it as the sender) |
| FLEET_CLAUDE_BIN / FLEET_CODEX_BIN / FLEET_OPENCODE_BIN | `claude` / `codex` / `opencode` on PATH | binaries (tests point them at stubs) |
| FLEET_KIRO_BIN | `kiro-cli` | Kiro binary (tests point it at a stub) |
| KIRO_HOME | `~/.kiro` | Kiro's global dir (sessions, leases, agents, settings, steering); passed to kiro lanes when set; read by install/kiro.sh and bin/fleet-lifecycle |
| FLEET_KIRO_GOAL_MAX | `20` | kiro goal mode rewrites `/goal ` to `/goal --max N `; positive integer (Kiro's own default is 5, DOC) |
| FLEET_AGY_BIN | `agy` | Antigravity CLI binary (tests point it at a stub) |
| FLEET_WAKE_DEFER_MAX | `900` | seconds a deferred typed wake keeps retrying before it leaves a notice instead (see Typed wakes) |
| FLEET_WAKE_BACKOFF | `2` | first retry gap of a deferred wake, in seconds; doubles, capped at 30 |
| FLEET_WAKE_QUIET | `3` | a pane that got keys from an attached client this many seconds ago counts as busy; `0` turns the check off |
| FLEET_NO_TYPE_ROLES | `decide` | roles (comma or space separated, roster `role`) whose pane never gets typed keys: native `SendMessage` or a tmux notice only (issue #34); empty allows typing everywhere |
| FLEET_NATIVE_GRACE | `120` | seconds a Claude to Claude `peer_send` waits for the sender's `SendMessage` before the typed fallback |
| FLEET_NATIVE | `1` | `0` turns the Claude to Claude native route off (every Claude target gets the guarded typed wake) |
| FLEET_WAKE_SPAWN | `1` | `0` never starts a detached waker (unit tests); a busy pane then just keeps its unread message |
| FLEET_AGY_HOME | `~/.gemini` | where agy keeps `config/` and `antigravity-cli/` (agents, rules, mcp_config.json, brain/<conversation>/ transcripts); passed to agy lanes when set; read by install/agy.sh and bin/fleet-lifecycle. agy itself always uses `$HOME/.gemini` (PROVEN 1.5.13), so this must point there |
| FLEET_GFX | `auto` | `fleet show` mode: `auto`, `kitty`, `symbols` or `text` (see Graphics in panes in the README) |
| FLEET_CHAFA_BIN | `chafa` | chafa binary `fleet show` runs (tests point it at a stub); missing means text only |
| FLEET_SHOW_COLORS | `full` | chafa color mode for `fleet show` block art |
| FLEET_DESK_PASSTHROUGH | `1` | unset or `1`: `fleet launch` and `fleet desks up` run `set-option -g allow-passthrough on` on the fleet server (every pane, live desks too); never `off`, never `-u`, never `all`. Anything else (`0` to opt out): no passthrough call, the v0.8.0 behavior |

## State layout ($FLEET_STATE)

```
roster.json            {"<name>": {"name","cli","mode","pane","session_id","port","dir","model","pid","at"}}
                       cli: claude|codex|opencode|kiro|agy ; pane: tmux pane id like "%12" ; port: int or null ;
                       session_id: claude uuid | codex thread uuid | opencode "ses_..." | kiro "sess_<uuid>" | agy conversation uuid | null until known
inbox/<name>.jsonl     one JSON per line: {"id","at","from","to","text"} (+ "via":"native" for a mirror copy,
                       + "native":"pending" for a Claude to Claude peer_send waiting for its SendMessage)
inbox/<name>.native    ids of "native":"pending" lines whose SendMessage the mirror hook saw (no typed wake for them)
wake/<name>.woken      inbox line count covered by the last typed pointer (the deferred waker never types twice for them)
wake/<name>.lock       flock held by the one deferred waker of a lane (`fleet_mesh.py wake-later <name>`)
wake/pane-<id>.lock    flock held while one wake (or park's /compact) checks, types, verifies and sends Enter
status-label           the saved `status-left` format of the fleet session, verbatim, no added newline (`fleet status-label FORMAT`; `--reset` deletes it)
inbox/<name>.cursor    integer: number of lines already returned by peer_inbox
decisions.jsonl        {"id":"Q<n>","at","from","question","options":[...],"status":"open|answered","answer","answeredAt"}
launch/<name>.sh       the window script ; launch/<name>.txt the first prompt ; launch/<name>.launch.json
log/fleet.log          one line per launch/exit/delivery
```
Writers update roster.json atomically (write temp file in the same dir, then `mv`).

## bin/fleet

```
fleet launch [--cli claude|codex|opencode|kiro|agy] [--role R] [--model M] [--dir D] [--split right|left|above|below --of NAME [--size PCT] | --split-right-of NAME] [--dry-run] [--no-proof]
             <name> goal <condition.txt> | prompt <text|file> | desk [text|file] | resume <session-id>
fleet ls                      roster + liveness (pane alive?) + mesh=yes|no, one line per lane
fleet attach                  tmux attach -t $FLEET_TMUX_SESSION
fleet layout                  print the tmux window/pane map
fleet stop <name>             kill that pane (asks nothing; used by tests and retire)
fleet show IMAGE [--text S]   draw IMAGE in this pane (bin/fleet-show, issue #41): mode kitty, symbols or text, then one
                              text line; `fleet-show: mode=<m> (<why>)` on stderr; never sets a tmux option
```
- `fleet show` auto mode: no tmux -> kitty for TERM `xterm-ghostty`/`xterm-kitty`, `TERM_PROGRAM=ghostty` or
  `KITTY_WINDOW_ID`, else symbols. In tmux: `list-clients -t <this pane's session> -F '#{client_pid} #{client_termname}'`;
  any client with `mosh-server` among its ancestors (up to 12 levels, `ps` comm basename), or with a termname not in
  xterm-ghostty|xterm-kitty|ghostty|kitty, or no client at all -> symbols; else `show -Apv -t $TMUX_PANE allow-passthrough`
  on/all -> kitty (`chafa -f kitty --passthrough tmux`), else symbols with a hint. Size from `stty size </dev/tty`, else
  `#{pane_width}x#{pane_height}`; the image gets rows-3. Every chafa call runs with PATH fronted by
  `lib/fleet-show-shim/tmux`, which drops set|set-option|setw|set-window-option (also after a `;`) and execs the real
  tmux for the rest. Test hooks: FLEET_SHOW_CLIENTS, FLEET_SHOW_PS, FLEET_SHOW_PASSTHROUGH, FLEET_SHOW_SIZE,
  FLEET_SHOW_SHIM_LOG. Exit 0 once a mode ran (chafa failing adds a last `mode=text (chafa failed ...)` line); 2 on
  usage, a missing image or a bad FLEET_GFX.
- `<name>` matches `^[A-Za-z0-9._-]+$`; refuse if roster has a live pane for it.
- goal mode condition lint (same as today's launcher): starts with `/goal `, <= 4000 chars, names a `*.json` goal file,
  contains `file wins`, contains `stop after`. desk mode refuses text starting with `/goal`.
- desk mode default text: `You are <name>, the standing decisions desk for $FLEET_HUMAN. Load the running-the-decisions-desk skill and run it. Never set a /goal.`
- Opening: `tmux new-window -d -P -F '#{pane_id}' -t <session>: -n <name> -c <dir> <script>`; with `--split DIR --of X`:
  `tmux split-window -d <flags> -P -F '#{pane_id}' -t <pane of X from roster> -c <dir> <script>`, flags `-h` (right),
  `-h -b` (left), `-v` (below), `-v -b` (above), plus `-l PCT%` for `--size PCT`. `--split-right-of X` is
  `--split right --of X`. Create the session
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

## Fleet UI hub (lib/fleet_ui.py; `fleet ui ...`)

```
fleet ui serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open] [--tab decide|learn|watch|route]
    stdout: with --tab, first "<DECISIONS|LEARN|WATCH|ROUTE> UI: http://127.0.0.1:P/<tab>/?t=TOKEN"; then
    "FLEET UI: http://127.0.0.1:P/?t=TOKEN" and one "  <tab> <url>" line per tab
fleet ui url [--json] [--tab T]     the URL while the hub answers (exit 0), else exit 3; --json {url, tabs: {tab: url}}
fleet decisions serve | fleet learn serve | fleet watch serve [args]    aliases (bin/fleet): the tab URL when the hub
    answers ("<WORD> UI: <tab url>", exit 0), else `fleet ui serve --tab <tab> [args]`
```

- One port, one token. Mounts: `/decide/*`, `/learn/*`, `/watch/*` run that page's own `make_handler` class with the
  prefix stripped (so every route below is reachable as `/<tab><route>`); `/route/` (HTML) and `/route/api/state`
  (`{lanes: [{name, role, cli, status, pane, goal, dir}], log: [{id, at, asker, verbatim, by}]}`, newest 30 first) are
  read only; `/` -> 302 `/decide/`. A page's HTML gets `window.FLEET_BASE="/<tab>"` and a tab strip; the pages prefix
  every fetch, SSE and path check with `FLEET_BASE` (empty when served standalone).
- Every request needs the token (`?t=` or `X-Fleet-Token` for GET, the header only for POST), a loopback Host for the
  hub's port, and no foreign `Origin` on POST -> else 403. The DECIDE tab runs its own `Hub` thread (SSE and
  `notifyCommand`, `{url}` pointing at `/decide/?t=TOKEN#<id>`).
- Guard: `fleet_learn_serve.human_only`, so any `FLEET_ROLE` exits 4 (`decide` and `watch` included). Non-loopback
  `--bind` or a busy port: exit 1, one line.
- `$FLEET_STATE/ui.url` (0600) holds the URL while it runs (removed on exit, SIGTERM included). Port: `--port`, else
  `app.json` `{"ports": {"ui": N}}`, else 7343. A tab whose core fails to import answers 503; the others stay up.

## DECISIONS UI (lib/fleet_decisions.py; `fleet decisions ...`)

```
fleet decisions serve [...]          alias of the hub (see Fleet UI hub); the standalone page: python3 lib/fleet_decisions.py serve
python3 lib/fleet_decisions.py serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "DECISIONS UI: http://127.0.0.1:P/?t=TOKEN"
fleet decisions tui [--one] [--plain]                              --plain reads keys from stdin (one per line; "o <text>" = own answer)
fleet decisions pick <id> <letter|--text T|--edit T|--refuse [why]>   exit 0 relayed, 3 already answered, 4 role refused, 1 error
fleet decisions retry <id> | snooze <id> [minutes] | state [--json] | find "<text>" | report <id> ok|fail [note]
fleet decisions ask "<q>" [--options a,b] [--allow ...] [--wait] [--timeout 30m] [--new]   exit 0 picked, 1 refused/own/changes, 2 no answer
fleet decisions refresh [--json] | refresh choose <id> A|B | refresh confirm <id>   confirm needs a prior "choose B"
fleet decisions intake ["<lines>"|-]      WATCH TASK-CANDIDATE/FYI lines -> task calls / fyi forward (role decide or none; exit 4 otherwise)
fleet decisions forward                  forward each task call the desk answered yes with decision_answer (log row by == $FLEET_DECIDE), once
fleet decisions learn [<id>]             one LEARN row with the human's verbatim answer (newest answered call by default); exit 3 if already learned
```
- Cards and Called rows carry `watch` ({sourceKind, source, ref, url, eid}; url http(s) only) from the decisions.jsonl row,
  else from `$FLEET_STATE/decide/calls.json` (written by `fleet_decisions.register_call(cid, kind, watch=None, task=None, **extra)`,
  kind task-candidate | probe | ask). The page shows a source badge and a link (rel noopener noreferrer); the TUI prints
  `[sourceKind] ref url`. `decision_open` keeps only those five keys (one-line strings, at most 500 chars) and refuses a
  non-object `watch`; `decision_answer`'s log row adds `by` (the answering lane's FLEET_NAME, else the desk).
- Task calls (kind task-candidate or probe) offer `yes: start a writing lane | learn only | no: drop`. Inside the pick
  lock, a yes delivers `TASK <eid> <ref> "<quote>" <url|-> by human:<Qn>` from `$FLEET_DECIDE` to `$FLEET_ROUTE`, once per
  call (`settledAt` in calls.json); task calls get no "[DECISIONS UI copy]". "learn only" on a task candidate sends the
  event to LEARN (`add_learn_item`). A probe call also calls `fleet_probe.on_pick(cid, choice, meta)`; its errors are
  logged and noted, never raised. A task call answered outside a pick forwards (via `fleet decisions forward`) only when
  its log row's `by` is `$FLEET_DECIDE`; anything else is reported `unverified` and sends nothing.
- `FYI` intake delivers `FYI <n> events: <eids> decidedBy: decide:probe` to ROUTE once per eid set
  (`decide/fyi-forwarded.json`) and logs a `fleet_probe_contract.log_row("fyi-forward", ...)` row. Rows with an `action`
  key are not Called answers.
- `POST /api/learn {id}` (token and loopback checks as every route; 409 when already learned) = TUI key `l`.
- A call = a `decisions.jsonl` row and/or a desk goal `openCalls[]` item (or legacy `openForHuman[]`) with the same id; open only while every store
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

## LEARN page (lib/fleet_learn_serve.py; `fleet learn serve`)

    fleet learn serve [...]    alias of the hub (see Fleet UI hub); the standalone page: python3 lib/fleet_learn.py serve
    python3 lib/fleet_learn.py serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "LEARN UI: http://127.0.0.1:P/?t=TOKEN"

- Exit 1 on a non-loopback `--bind` or a port in use (one line, no traceback); exit 4 when `FLEET_ROLE` is set.
- Writes only `$FLEET_STATE/learn-ui.url` (0600). Every `learn/` write goes through `fleet_learn.review()`.
- HTTP: `GET /` and `GET /api/state` need `?t=` or `X-Fleet-Token`. `/api/state` =
  `{snapshot: fleet_learn.snapshot(), history: [{topic, origin, points: [{day, reviews, passes, score}]}],
  quiz: [{cid, lid, format, origin, prompt, source, options (choice only)}]}`; quiz items never carry `answer` or
  `grounding`. History covers the last 60 days of `reviews()`, joined to topics through `items(include_retired=True)`.
- `POST /api/review {cid, answer, rating?, latencyMs?}` -> `{review: <reviews.jsonl row>, answer: <source quote>}`;
  `POST /api/reveal {cid}` (recall only, writes nothing) -> `{cid, answer, source}`. Both need the `X-Fleet-Token`
  header (a `?t=` query is not enough). Not due -> 409; unknown card -> 404; bad id, recall without a rating, or a
  body that is not a JSON object -> 400; wrong or missing token, a foreign Host or a foreign Origin -> 403.
- `learn/learn.json` `servePort` (optional) sets the default port.

## WATCH page (lib/fleet_watch_serve.py; `fleet watch serve`)

    fleet watch serve [...]    alias of the hub (see Fleet UI hub); the standalone page: python3 lib/fleet_watch_serve.py
    python3 lib/fleet_watch_serve.py [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "WATCH UI: http://127.0.0.1:P/?t=TOKEN"

- A second front end on the WATCH desk: reads `fleet_watch.snapshot()` and calls the status pane's verbs
  (`poll_now`, `pause`, `resume`, `release_held`), so a page action and the same pane key end in the same state.
  `release_held` returns a list of Q ids, one per held ask.
- Default port 7342. `$FLEET_STATE/watch-ui.url` (0600) holds the URL while it runs.
- HTTP: `GET /`, `/api/state` need `?t=` or `X-Fleet-Token`; `POST /api/poll {source}`, `/api/pause {source}`,
  `/api/resume {source}`, `/api/release {}` need the `X-Fleet-Token` header (a `?t=` query is not enough). Wrong or
  missing token, a Host other than `127.0.0.1:P` / `localhost:P` / `[::1]:P`, or a foreign `Origin` -> 403. Unknown
  source -> 404, missing source -> 400. A bind other than loopback is refused (exit 1).
- Role guard: runs only with `FLEET_ROLE` unset or `watch`.

## Reserved loopback ports

7343 the Fleet UI hub (`fleet ui serve`; `fleet decisions|learn|watch serve` are its aliases). The standalone pages keep
7340 DECISIONS UI, 7341 LEARN page, 7342 WATCH page. 7349 WATCH webhook receiver (`fleet watch webhook serve`).
7344-7348 are free.

## lib/fleet_mesh.py

```
python3 lib/fleet_mesh.py serve                 stdio MCP server (JSON-RPC 2.0, newline-delimited, protocol 2025-06-18)
python3 lib/fleet_mesh.py send <from> <to> <text>    CLI delivery (same code path as peer_send)
python3 lib/fleet_mesh.py inbox <name>          print unread, advance cursor
python3 lib/fleet_mesh.py roster                print roster.json
python3 lib/fleet_mesh.py mirror                PostToolUse hook: copy a native SendMessage into the mesh (stdin = hook JSON, exit 0)
python3 lib/fleet_mesh.py wake-later <name>     deferred waker (started detached by a deferred wake; one per lane)
python3 lib/fleet_mesh.py pane-state <pane>     "empty <why>" exit 0, or "busy <why>" exit 1: may a wake be typed now
python3 lib/fleet_mesh.py stale                 "STALE mesh <lane> pid N: <why>" per serve process of this kit on older code (exit 0)
python3 lib/fleet_mesh.py version               the content hash (sha256, 12 hex) of lib/fleet_mesh.py
python3 lib/fleet_mesh.py type <pane> <text>    guarded type + Enter (the same checks, lock and Enter rule as a typed
                                                wake): "sent" exit 0, "failed" exit 1, "busy <why>" exit 3 (fleet park)
```

Stale servers (issue #31). An MCP client keeps one `serve` process for its whole session, so a kit upgrade on disk does
not reach it on its own. Each server writes `$FLEET_STATE/mesh/<pid>.json` (`pid`, `lane` = `FLEET_NAME`, `hash`,
`file`, `started`, `reexecs`) and compares `hash` with the file on disk before every request and every typed wake:
- Stale before a request: it runs the new file once (`version` must print the disk hash), then `execv`s it in place.
  Same pid, same stdin/stdout. The request and any bytes still unread are handed over (`FLEET_MESH_CARRY`), and the
  protocol keeps no state after `initialize`, so the client's session carries on. Bound: 20 re-execs per process.
- When it can't re-exec (`FLEET_MESH_REEXEC=0`, the self-check fails, the bound is reached): typed wakes are refused.
  The message is in the inbox, a tmux notice plus `@fleet_unread` replaces the typing, and the result says
  `typed wake deferred (stale: ...)`. Codex queue and OpenCode HTTP wakes are not typed and go as before.
- `fleet tick` prints the `stale` lines after the table (`FLEET_TICK_NO_MESH_SCAN=1` skips the ps scan). A server from
  before this change has no record; it is listed when it started before the file's mtime, its lane read from its
  `FLEET_NAME` env or the roster pane it runs under.
MCP tools: `peer_list {}`, `peer_send {to, text, from?}`, `peer_inbox {name?, all?}`,
`decision_open {question, options?: [str], from?}` (appends decisions.jsonl and delivers to $FLEET_DECIDE),
`decision_list {status?}`, `decision_answer {id, answer}` (marks answered, delivers to the asker only; see Messaging routing),
`decision_report {id, ok, note?}`, `decision_find {text}` (see DECISIONS UI). `decision_open` also takes `allow?: [pick|edit|own|snooze|refuse]`.
Revisions (issue #60, `lib/fleet_revisions.py`):
- `decision_revise {id, question?, options?, why, source?, from?}`: the asker (or the desk) revises an open call. It appends
  `revisions[] {rev, at, by, why, source?, old, new, changed}` to the row and bumps `rev` (absent = 1); `old`/`new` hold only
  the changed fields, `changed` names them for display (`question`, `option a`, `claim 1`). Never overwrites, never a new line.
  `desk.sh revise`, `fleet decisions revise` and the UI take the same path (`fleet_decisions.revise`), which mirrors the result
  onto the desk's copy and tells the other side (`[revised Q<n> rev N by <who>] changed: ...; why: ...; now: ...`).
- Refused: no `why`, nothing changed, a call no longer open, and a new question sharing too few content words with the rev 1
  question (a different topic is a new call). Options-only revisions skip the topic check. `desk.sh amend` refuses an open call.
- `decision_answer` takes `rev?` (the rev the answer was given against, default the current one) and records `answeredRev` on the
  row; an older rev also adds `answeredRev` and `warning: "answered rev 1, now rev 2"` to the log line, and the note to the relay. The UI sends the
  rev its card showed. Cards carry `rev`, `revSummary` ("rev 2, changed: question, option a"), `changed`, `diff` (word ops
  `[["=",..],["-",..],["+",..]]` per field) and `revision {rev, at, by, why, source}`.
- `fleet learn revisions [--days N] [--json]`: revisions per asker lane (the call's `from`) per day; a day at 3 or more and above
  twice the lane's usual (median) count is flagged. A coaching signal, never a block.
Sender = `from` arg, else `$FLEET_NAME`, else error.
Delivery (`deliver(frm, to, text)`): always append to inbox/<to>.jsonl, then wake by the target's roster `cli`:
- codex: `$FLEET_CODEX_BIN queue --thread <to> --message "[fleet message from <frm>] <text>"` (PROVEN wakes an idle TUI)
- opencode: `POST http://127.0.0.1:<port>/session/<session_id>/prompt_async` JSON `{"parts":[{"type":"text","text":"[fleet message from <frm>] <text>"}]}` (PROVEN, 204)
- claude, sent through an MCP tool (`peer_send`, `decision_open`, `decision_answer`) by a live fleet Claude lane's own
  MCP server (`CLAUDECODE=1`, `FLEET_NAME` = sender, roster cli claude) to a live (not parked, not done) Claude lane:
  NO typed wake. (`fleet send`, `fleet_mesh.py send` and the WATCH, probe and DECISIONS engines never take this
  route: no agent reads their result.) The inbox line gets
  `"native":"pending"` and the result tells the sender to call `SendMessage` to the target with the same text. That
  native message reaches the target without touching its input box; the mirror hook links it to the pending line
  (`inbox/<to>.native`) instead of writing a copy. A safety-net waker types the guarded pointer (below) only if no
  `SendMessage` was linked after `FLEET_NATIVE_GRACE` seconds. See Messaging model for why this is safe.
- claude, any other sender: one bracketed paste of the pointer, `tmux [-L sock] set-buffer -b fleet-wake-<pid> --
  "[fleet] message from <frm>: run peer_inbox to read it"` then `paste-buffer -p -d -b fleet-wake-<pid> -t <pane>`,
  then `send-keys -t <pane> Enter`, guarded (see Typed wakes: only into an empty, quiet input box)
  (never the message body itself: typed input is treated as the human's). Paste, not keys (issue #38): in Claude
  Code's vim NORMAL mode typed keys run as vim commands and `[fleet] mes` was eaten up to the `s`, leaving
  `sage from ...` stuck in the box; a bracketed paste lands verbatim in NORMAL and INSERT mode, and Enter submits from
  either. PROVEN on Claude 2.1.332 (see Typed wakes for the live loop).
- kiro: same guarded typed pointer line and `Enter` as claude (PROVEN: starts a turn on an idle lane; the lane then runs `peer_inbox`)
- agy: same guarded typed pointer line and `Enter` as claude (PROVEN typed text + Enter starts a turn on an idle agy 1.5.13 TUI;
  the woken lane then runs `peer_inbox`: PROVEN by the kit's live proof run (agy 1.5.13)). No pane in the roster: inbox only and the result says the wake failed.
  Before typing, delivery reads the agy pane (`capture-pane -p`) and refuses to type when it shows agy's trust dialog,
  color-scheme or Terms of Service onboarding, or a sign-in wait (its Enter would answer that screen for the human; the
  Terms of Service screen has a data-sharing opt-in pre-checked), or when the pane can't be read. The message stays in
  the inbox and the result says the wake FAILED.
- target missing from roster: inbox only, result says `queued (no live lane)`.
Result text names the wake used and whether it succeeded; a failed wake still leaves the message in the inbox.
A deferred wake reads `delivered X -> Y (inbox written; typed wake deferred (<why>); retrying for up to Ns, then a tmux notice)`.

**No-type panes (issue #34).** A lane whose roster `role` is in `FLEET_NO_TYPE_ROLES` (default `decide`, the desk the
human answers in) never gets typed keys, empty input or not. A Claude lane's `peer_send` or decision tool call to it
still goes native: the result tells the sender that its `SendMessage` is the only wake. When the grace runs out with no
SendMessage linked, the waker leaves the notice below instead of the typed fallback. Every other path (CLI `fleet
send`, Codex or OpenCode senders, engine rewakes) leaves that notice at once and returns `typed wake deferred (role
decide never gets typed keys ...)`. The inbox line is written first either way.

**Typed wakes (claude, kiro, agy).** `type_wake()` never types into text a human is writing. Before typing:
1. No attached client whose current pane is the target sent a key in the last `FLEET_WAKE_QUIET` seconds
   (`list-clients -F '#{pane_id} #{client_activity}'`).
2. The pane is not in copy mode, and no vim `-- NORMAL --` line shows. Claude Code's own vim NORMAL mode (no
   `-- INSERT --` in its footer) is NOT busy: a Claude pane gets the bracketed paste above, which NORMAL mode takes
   verbatim (v0.5.0 deferred it instead, but read `editorMode` only from `~/.claude.json`, while Claude Code 2.1.33x
   keeps it in `~/.claude/settings.json`, so the guard never fired and the keys were eaten; issue #38).
3. The row under the cursor (`display-message -p '#{cursor_x} #{cursor_y}'`, `capture-pane -p -e`) is an EMPTY prompt:
   the text left of the cursor is exactly a prompt glyph (`❯` Claude Code, `›` kiro-cli, `>` agy), and every other
   cell on that row and on the rows below it, up to the box's rule line, is placeholder: faint (Claude) or drawn in an
   explicit colour (kiro's grey hint, agy's mode hint). Typed text is drawn in the default foreground by all three
   (captured 2026-11-23, `test/fixtures/wake_screens.json`). It is an allow-list: a dialog, a held-message modal, a menu
   or any screen not matching counts as busy.
Any check failing, or the pane unreadable, means busy: nothing is typed, and a detached `wake-later <to>` (one per
lane, flock on `wake/<to>.lock`) retries with backoff (`FLEET_WAKE_BACKOFF`, doubling to 30 s) until the wake lands,
the lane reads its inbox, or `FLEET_WAKE_DEFER_MAX` passes. Then it leaves a notice that types nothing:
`display-message -d 15000` on every attached client and the pane option `@fleet_unread` ("N from X", cleared by
`peer_inbox`; show it with `#{@fleet_unread}` in a status format).
Check, type, verify and Enter run under a per-pane flock (`wake/pane-<id>.lock`, waits up to 8 s, else counts as
busy), so two senders never type into one box at once. After typing, Enter is sent only once the input row reads
exactly the pointer (whitespace ignored, wrapped rows joined). When the box instead holds only a piece of what was
just typed (the TUI ate keys), the kit clears it (`C-a C-k`, or `0 D` in Claude's vim NORMAL mode, read from
`editorMode` in `~/.claude.json` ($FLEET_CLAUDE_JSON), `settings.json` and `settings.local.json` in
`$CLAUDE_CONFIG_DIR` or `~/.claude`; `$FLEET_CLAUDE_SETTINGS` replaces that list), checks the box is empty and quiet
again, and retries, up to `FLEET_WAKE_TYPE_TRIES` (3) attempts. After the last one the box is left empty, Enter is not
sent, and the result says so; the inbox still holds the message. A box holding anything that is not a piece of the
pointer is never cleared (it may hold a human's keys). A box that holds nothing but an unsent fleet pointer (an
earlier timeout) gets its Enter on the next wake instead of blocking every later wake. `fleet park` types `/compact`
through the same guard (`fleet_mesh.py type <pane> /compact <cli>`; a Claude pane gets the paste) and refuses to park
when the box is busy. Without a cli argument, `type` pastes into a pane whose cursor row starts with `❯`.
**Residual race:** a key the human presses in the milliseconds between the last check and `send-keys` still lands next
to the pointer. The post-type check then sees more than the pointer and sends no Enter, so the human's half sentence
is never submitted; the pointer stays in their box for them to delete. The kit cannot remove it without deleting
their keys too. PROVEN on throwaway panes (tmux -L) of Claude Code 2.1.330, kiro-cli 3.5.0 and agy 1.5.13: an empty
prompt got the wake and the lane answered (190-char pointer wrapped on a 120-column pane, too); a pane holding
`good morni` or `abc` with the cursor at the start was left exactly as it was.
Issue #38 live loop (Claude Code 2.1.332, `editorMode: vim` in `~/.claude/settings.json`, throwaway pane on its own
tmux socket at 160x45, 20 wakes per mode, submits counted from the session transcript): in NORMAL mode v0.5.0 left
the `sage from ROUTE: ...` fragment 20 of 20 times and submitted none; v0.5.1 submitted all 20 with 0 fragments. In
INSERT mode and in a wake fired the moment a turn ends (redraw), both versions submitted 20 of 20 with 0 fragments.
`fleet_mesh.py type <pane> /compact claude` in NORMAL mode ran `/compact`. The long mesh=no pointer (with the inbox
path) goes through the same paste and is tested on the fake vim box (`test/fixtures/vim_prompt.py`), not live.

### Side runs: no decisions desk (issue #12)

A side run is a child of one lane, started outside the fleet's panes. Its env carries `FLEET_SIDE=<id>` and
`FLEET_PARENT=<parent lane>` (set by `fleet side run`; this guard only reads them). With `FLEET_SIDE` set:
- `decision_open`, `decision_answer` and `fleet decisions ask` refuse (MCP `isError`; CLI `FAIL ...`, exit 1):
  `side run S1 can't open decisions; send it to your parent lane L with peer_send` (`answer` for decision_answer).
  Nothing is written to `decisions.jsonl` or `decisions-log.jsonl`.
- `peer_send` delivers only to `$FLEET_PARENT`; any other `to` is refused. The message's `from` is `<parent>/<side id>`
  (e.g. `L/S1`), whatever `from` arg was passed, so the parent can tell which side run wrote.
- `FLEET_SIDE` set without `FLEET_PARENT`: all three refuse.
With `FLEET_SIDE` unset nothing changes. Helpers: `side_run()` -> `(id, parent)` or None, `side_refuse(verb)`.

## bin/fleet-goal

```
fleet-goal new <goal.json> --goal "<text>" [--report-to NAME]   skeleton with goal, owner, reportTo, phases[], gates{}, mustNot[], nextAction, done[], openCalls[], decisions[], changelog[]
fleet-goal condition <goal.json> [--report-to NAME] [--turns N]  prints a /goal condition (<= 4000 chars) that passes the fleet launch lint
fleet-goal check <goal.json>     first line `OK` or `FAIL <path>: <why>`; exit 0/1
```
`check` accepts `goal` as a string or an object with `title`/`objective`, phases with only an `id`, and the open-calls list as `openCalls` or the legacy `openForHuman` (one per file).
Desk helper `skills/running-the-decisions-desk/desk.sh` uses `fleet-goal check` and field `openCalls`; on a file that already has `openForHuman` it reads and writes that key instead (never migrated in place, so readers that know only the legacy name keep working). `lib/fleet_decisions.py` reads the same way.

## bin/fleet-update (`fleet update`, `tafk update`, v0.9.0)

```
fleet update [--check] [--dry-run]   tafk update ... is the same (bin/tafk -> fleet)
fleet version                         git describe --tags, else v$(cat VERSION), else `unknown`
```
- Install type: ROOT (bin/..) is a git work tree root -> git; otherwise a copied tree (needs `VERSION`, `X.Y.Z`).
- Versions: only `vX.Y.Z` tags count (no prerelease suffix), compared numerically per field; latest <= current -> `already at vCUR (latest release vLATEST)`, exit 0, no writes.
- git: current = `git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' --exclude '*-*'` (VERSION fallback); latest = highest from `git ls-remote --tags --refs origin 'v*'`. Update = fetch `refs/tags/vX` only, `git merge --ff-only` on a branch, `checkout --detach` on a detached HEAD.
- copied tree: latest = `tag_name` of `$FLEET_UPDATE_API/releases/latest`; update = tarball into `<parent>/.fleet-update.XXXXXX`, its `VERSION` must equal the tag, then `mv ROOT ROOT.prev-vCUR` and `mv new ROOT` (rolled back on failure).
- Output: `update available: vA -> vB (...)` (--check), `would ...` lines and `dry run: vA -> vB, nothing changed` (--dry-run), `updated: vA -> vB (...)`. Link lines: `unchanged:`, `relinked:`, `linked:`, `would link:`, `would relink:`, `left alone:`, `note: no symlink ...`.
- Refusals print `fleet update: <why>; nothing changed` on stderr and exit 1, before any write to the tree or branch: dirty (`git status --porcelain` non-empty), diverged (HEAD not an ancestor of the tag; checked before the fetch when the tag is already local, else right after the tag-only fetch), offline (ls-remote, fetch, API or tarball download failed or timed out), bad tarball. Bad arguments exit 2.
- Links: in `${FLEET_PREFIX:-$HOME/.local}/bin`, names fleet, fleet-goal, tafk: a symlink pointing into `ROOT/bin/` (its target's dir resolved with `pwd -P`, relative targets from the link's dir, so `/tmp/...` matches `/private/tmp/...`) is refreshed; a missing `tafk` is created when `fleet` points at `ROOT/bin/fleet`; anything else is left alone. Never runs an installer, `desks up` or tmux.

## bin/fleet-remote (`fleet remote ...`, issue #22)

```
fleet remote setup [--pubkey KEY|FILE] [--ip ADDR] [--dry-run]   idempotent; exit 0, or 1 with `ERROR: ...` on stderr
fleet remote status [--ip ADDR]                                 read-only; never prompts (sudo -n for sshd -T)
fleet remote qr [--read-only] [--ip ADDR]                        line 1: `mosh [--server=P] USER@IP -- TMUX [-L SOCK] attach -t SESSION [-r]`
fleet remote off [--dry-run] [--keep-sshd]
fleet remote root-script FILE|-                                  write the root steps as a script (mode 700); `-` = stdout; never runs sudo
fleet remote verify                                              unprivileged; one `ok:`/`FAIL`/`skipped:` line per check; exit 1 on any FAIL
fleet remote ssh-path [--check|--brew-bin]                       --check: `ok: T on the non-interactive ssh PATH: P` or `missing: T ...`, exit 1 if any missing
fleet remote add-key KEY|FILE [--dry-run]                        append once to ~/.ssh/authorized_keys (dir 700, file 600)
fleet remote attach-line [--read-only] [--ip ADDR]               exactly one line, the attach command
```
- Drop-in: `/etc/ssh/sshd_config.d/010-fleet-remote-keyonly.conf` (PubkeyAuthentication yes, PasswordAuthentication no,
  KbdInteractiveAuthentication no, PermitRootLogin no). Written before sshd is switched on, validated with `sshd -t` and
  `sshd -T` (passwordauthentication no), restored or removed on failure.
- State: `$FLEET_STATE/remote.json` `{"sshd_was_on_before_setup": bool, "dropin": path}`, written by the first setup
  and removed by `off`. `off` turns sshd off only when that field is `false` and `--keep-sshd` is absent.
- Env: `FLEET_REMOTE_IP` (overlay address), `FLEET_REMOTE_OS` (default `uname -s`), `FLEET_REMOTE_SSH_PORT` (22),
  `FLEET_TMUX_SOCKET` / `FLEET_TMUX_SESSION` (the attach target and the window-size server).
- `FLEET_REMOTE_SANDBOX=DIR` (tests): sshd files live under `DIR/etc/ssh`, and every system tool (sudo, sshd, systemsetup,
  socketfilterfw, launchctl, systemctl, nc, ssh-keygen -A, ifconfig/ip, tailscale, pmset, osascript) must be
  `DIR/bin/<name>`. A missing tool is an error, never a fall-through to the real one.

## v2 addendum (roles, lifecycle, routing)

### Roles
- `fleet launch --role route|decide|watch|learn|auth|worker` (default `worker`; `desk` mode defaults to role `decide`).
  `--role orchestrator|desk|listener` exits 1 with `FAIL role <old> is renamed <new>` in every mode, `resume`
  included, so `fleet reopen` of a lane whose roster role is an old name is refused. `fleet tick` gives such a row
  the action `forget` once its pane is dead (a live one shows `-`); they are not standing desks.
- `fleet forget NAME [--force]` (ROUTE or the human; other roles get `FAIL role <r> can't forget`): moves the roster
  row to `$FLEET_STATE/archive/forgotten/NAME-<utc ts>/roster-entry.json`, moves `inbox/NAME.jsonl` and
  `inbox/NAME.cursor` there too, deletes the row and logs `NAME forgotten` to `log/fleet.log`. A lane whose pane is
  alive is refused (`FAIL lane NAME is live (pane %N)`) unless `--force`, which drops the row, prints a WARN and
  leaves the pane running. Unlike `cleanup --apply` it touches one row only.
- Mesh: `fleet ls` prints `mesh=yes|no` per lane (`lib/fleet_mesh.py mesh`, `has_mesh`). Evidence, first match wins:
  the roster row's `mesh` bool; claude: `$FLEET_STATE/launch/<name>.sh` passes `fleet_mesh.py`, or a `fleet` server
  in `~/.claude.json` (`$FLEET_CLAUDE_JSON`; user or the lane dir's project) or `<dir>/.mcp.json`; codex, opencode,
  kiro, agy: a `fleet` server in the CLI's global MCP config (`$CODEX_HOME`, `$XDG_CONFIG_HOME/opencode`,
  `$KIRO_HOME`, `$FLEET_AGY_HOME`).
- Wake line (claude, kiro, agy panes): `[fleet] message from X: run peer_inbox to read it`; a mesh=no lane gets
  ` (no peer_inbox tool? read $FLEET_STATE/inbox/<to>.jsonl)` appended, the absolute path of its inbox.
  `fleet reopen` resumes through `fleet launch`, so a reopened claude lane gets `--mcp-config` with the fleet mesh.
- `--role probe` (phase 4): `bin/fleet` asks `lib/fleet_probe.py can-host <cli>` and dies on a NO
  (`FAIL role probe refused on --cli <cli>: ...`). Allowed hosts are `PROBE_ALLOWED`: a CLI joins
  only after `FLEET_PROBE_LIVE=1 test/test_probe_live.sh --cli <cli>` proves the probe can't run a shell command or
  write a file and a worker on the same CLI can. Allowed: `claude`, `opencode`, `kiro`. Evidence, 2026-11-22,
  test_probe_live.sh at 2ee61a8 with isolated CLI config: Claude Code 2.1.330 15 PASS / 0 FAIL, OpenCode 1.19.0
  17 / 0, kiro-cli 3.5.0 17 / 0. In each run both lanes started real sessions, the worker control wrote its file and
  POSTed to the fake API, the probe did neither, closed its row with probe_report and removed its clone, and the
  global CLI config hash was unchanged. Second evidence set, 2026-11-23, `FLEET_PROBE_ACH=1 test/test_probe_ach.sh`
  at c5bf601 through ach 0.32.0 (probe vs worker judged at tool-call level): 51 PASS / 0 FAIL / 0 INCONCLUSIVE
  across claude, opencode and kiro. Stated limit: on all three CLIs the probe's shell and write tools were absent
  (never offered), so no refusal was exercised. The bar "can't run a shell command or write a file, while a worker
  can" is met; "refused when tried" is unproven. codex and agy are refused (no proven way to drop shell/apply_patch).
  `FLEET_PROBE_ALLOW_UNVERIFIED=<cli>[,<cli>]` is a test-only override the live test sets: it can only reach a CLI
  `PROBE_HOSTS` marks True, logs `WARN FLEET_PROBE_ALLOW_UNVERIFIED=...` to stderr on every use, and is ignored when
  unset. A claude probe launches with `--tools Read,Grep,Glob,WebFetch --strict-mcp-config` and an `--mcp-config`
  holding only `fleet` and `fleet-probe`; a missing opencode/kiro `fleet-probe` agent fails the launch.
- OpenCode probe verbs: `install/opencode.sh` registers the `fleet-probe` MCP globally, and OpenCode 1.19 resolves
  tool rules last-match-wins with the global `permission` after the global `tools`, so `tools["fleet-probe_*"]=false`
  alone left the verbs on every agent (a live run listed all 8 on fleet-worker). Every agent but `fleet-probe` (each
  `.agent` key plus the built-in `build`, `plan`, `general`) gets `permission["fleet-probe_*"] = "deny"`; a string
  permission becomes `{"*": <it>, "fleet-probe_*": "deny"}`. `test/test_probe_opencode_tools.sh` checks it through
  `opencode debug agent` (and with `FLEET_PROBE_OC_LIVE=1` through live runs), with the pre-fix config as control.
- `fleet tick`/`fleet cleanup`: a live probe lane silent over 900 s gets `park-stalled`; `cleanup --apply` parks it
  `--no-compact` and marks its probes.jsonl row `stalled` (`fleet-probe stalled --apply`). `FLEET_NOW` fixes the clock for tests.
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
  Builds two split windows, all five desks in desk mode: `deskA` = AUTH (role auth, top left, split above ROUTE at
  40% of the column; docs/design/auth-desk.md) over ROUTE (role route, bottom left) | DECIDE (role decide, split right of ROUTE, the whole right column), `deskB` = WATCH (role watch, left) | LEARN (role learn, split right of WATCH). No separate
  WATCH or LEARN window. Every desk pane is tagged `@fleet-desk <role>`. WATCH and LEARN get a read-only "look for
  work" first prompt. Drops the `home` window when it created the session. `--dry-run` prints
  `window deskA: ROUTE (left) | DECIDE (right)` and `window deskB: WATCH (left) | LEARN (right)` before the launches it
  would run, plus `window deskA: AUTH (above ROUTE)`. Last line:
  `OK desks up: deskA AUTH=%N / ROUTE=%N | DECIDE=%N, deskB WATCH=%N | LEARN=%N`.
- Idempotent: a desk whose roster pane is live is left alone (`<NAME> already live (pane %N)`); when all five are live
  nothing changes (no launch, rename or tag). A dead right desk is relaunched split right of the live left one; a dead
  left desk is split beside the live right one and swapped to the left. The window is renamed `deskA`/`deskB` only
  when something in it was launched.
- AUTH on a four-desk fleet: AUTH is split above the live ROUTE and the other panes keep their ids. A dead DECIDE
  comes back as the right column; a dead ROUTE beside a live AUTH comes back under AUTH. After any deskA launch, when
  deskA holds exactly ROUTE, DECIDE and AUTH, it is tidied once (`OK deskA layout: AUTH over ROUTE | DECIDE`): pane
  order AUTH, ROUTE, DECIDE, then one custom `select-layout` (left column half the width, AUTH 40% of its height,
  DECIDE the full height; checksum computed like tmux's layout_checksum, so any tmux with custom layouts works). Otherwise the layout
  is left alone with a WARN.
- Window slots: every run, live desks included, ends with window `deskA` at tmux index 0 and `deskB` at 1. A desk
  window elsewhere is moved there with `move-window`, or `swap-window` with the window at that index (which takes the
  desk's old index); pane and window ids never change, and the session's current window is selected back.
  Prints `OK window deskA moved to index 0`, `window deskA already at index 0`, or under `--dry-run`
  `window deskA: placed at index 0` (same for deskB at 1).
- `fleet launch` (no `--split`) opens the lane window at the first free index from 2 (`new-window -t =SESSION:N`,
  retried up to 5 times if another launch took N first), so a lane never takes 0 or 1, desks up or not.
- Beside a running fleet: other lanes, including pre-rename ORCHESTRATOR and DECISIONS sessions, keep their windows,
  panes, roster entries and inboxes; nothing is typed into them and no alias map is written.
- WATCH status pane: when the run launched the WATCH desk, a non-LLM pane split under it (`split-window -v -l 12`)
  runs `FLEET_STATE=<state> fleet watch status`, tagged `@fleet-watch-status 1` and titled `WATCH-status`
  (`OK WATCH status pane %N`). One per session: a live tagged pane is reused (`WATCH status pane already live`).
  A run that did not launch WATCH adds none (`WATCH desk not launched by this run: no WATCH status pane`), so a fleet
  whose desks are all live is untouched. `--dry-run` prints `window deskB: WATCH status pane under WATCH` and the
  `would run: tmux split-window ...` line.

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
- `fleet park NAME [--no-compact]`: first types `/compact` (guarded: only into an empty, quiet prompt, else it
  refuses with `FAIL can't type /compact into NAME (busy <why>)`) and waits <= FLEET_COMPACT_WAIT (600 s) for the CLI's
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
  (`chat.disableTrustAllConfirmation` true) and `steering/AGENTS.fleet.md`; agy.sh writes `$FLEET_AGY_HOME/config/agents/fleet-<role>.md`
  from roles/*.agy.md + roles/<role>.md, `config/rules/fleet-rules.md` (`trigger: always_on`), `config/mcp_config.json`
  (`agy mcp add fleet python3 <repo>/lib/fleet_mesh.py serve`, or jq when `FLEET_AGY_HOME` is not `$HOME/.gemini` or agy is missing),
  `config/skills/<name>/`, and with `--trust DIR` the `antigravity-cli/settings.json` `trustedWorkspaces`; claude.sh needs nothing (roles are passed per launch).
- Rules layer (#55): `rules/AGENTS.fleet.md` is rendered to `$FLEET_CONFIG_DIR/AGENTS.fleet.md` (default
  `~/.config/fleet`), with the placeholder values in `rules.env` (shell, `printf %q`). Claude gets the block
  `<!-- fleet-rules:begin -->` / `@~/.config/fleet/AGENTS.fleet.md` / `<!-- fleet-rules:end -->` in `~/.claude/CLAUDE.md`,
  OpenCode an `instructions` entry with the absolute path, Codex the rendered text between the same markers in
  `$CODEX_HOME/AGENTS.md`, Kiro `steering/AGENTS.fleet.md`, agy `config/rules/fleet-rules.md`. `install/rules.sh
  refresh|uninstall|path [--dry-run] [--cli LIST]` (`fleet rules ...`, `fleet uninstall`); `fleet update` runs refresh.

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
| claude, from a live fleet Claude lane | none typed: the sender's own `SendMessage` (native cross-session message), typed fallback after `FLEET_NATIVE_GRACE` | PROVEN 2.1.330: a bypass lane's `SendMessage` to a bypass session ran a background turn with `good morni` still in the receiver's box |
| claude, from anyone else | guarded bracketed paste of the pointer (`set-buffer` + `paste-buffer -p`) + Enter, only into an empty input | PROVEN 2.1.330 (typed keys, hand-launched lane): idle bypass lane ran `peer_inbox`; PROVEN 2.1.332 (paste, issue #38): see Typed wakes |
| kiro | guarded typed pointer line + Enter | PROVEN kiro-cli 3.5.0 |
| agy | guarded typed pointer line (`send-keys -l`) + Enter | PROVEN agy 1.5.13 that typed text + Enter starts a turn; the full `peer_inbox` round trip through `fleet launch`: PROVEN by the kit's live proof run (agy 1.5.13) |
| codex | `codex queue --thread` (body included) | PROVEN |
| opencode | `POST /session/<id>/prompt_async` (body included) | PROVEN |

The kit itself never writes to Claude's native socket (`messagingSocketPath` in `~/.claude/sessions/<pid>.json`, e.g.
`/tmp/cc-socks/<pid>.sock`); a Claude sender's own `SendMessage` does (above). Measured on 2.1.330 with an honest plain frame `{"type":"user","message":{"role":"user","content":"..."}}`
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

`crossSessionInbound` (2.1.330: `accept` | `hold` | `refuse`; read from policy, `--settings` and user settings; project
and local settings can only tighten it), measured on a throwaway bypass session (2026-11-23):

| Value | `SendMessage` from a fleet Claude lane (bypass) | plain python3 frame (no mode) |
|---|---|---|
| unset (default) | delivered in the background, mode attested | HELD (modal) |
| `hold` | held | held |
| `accept` | delivered | delivered |

`accept` lets any process of the same user (the socket is 0600 in a 0700 dir) start a turn in the session, so the kit
does not set it; the default already lets Claude lanes reach each other. Native delivery to a STOPPED session fails at
send time ("No agent named ... is reachable", by name and by session id): nothing is queued, and a resumed session
receives nothing. So a parked Claude target keeps the mesh path: inbox line, reopen, guarded typed wake.

**Mirror rule.** A message one fleet lane sends another natively must also land in the mesh inbox. Mechanism: every
`roles/<role>.claude.json` has a `PostToolUse` hook, matcher `SendMessage`, running
`python3 "$FLEET_MESH" mirror` (exit 0 always). `mirror` reads the hook JSON (`tool_input.to`, `tool_input.message`),
maps `to` to a roster lane (a lane name, `name [ref]`, or `uds:<socket>` matched through
`~/.claude/sessions/<roster pid>.json` whose `sessionId` equals the roster `session_id`), and appends
`{id, at, from: $FLEET_NAME, to, text, via: "native"}` to `inbox/<to>.jsonl`. No wake and no reopen: the native
message already reached the lane. Skipped: no `FLEET_NAME`, target not on the roster, target is the sender.
When the sender's `peer_send` already wrote a `"native":"pending"` line to that lane, `mirror` links the SendMessage to
the oldest unlinked one (appends its id to `inbox/<to>.native`) instead of writing a second copy.
`peer_inbox` shows these as `from X (native copy)`. Until the receiver reads them they count as unread
(`peer_list`, `fleet tick`). Lanes launched before this change get the hook and the MCP on their next relaunch or reopen. PROVEN on two live bypass lanes: `SendMessage to="LANEB"` and
`to="uds:/tmp/cc-socks/<pid>.sock"` both produced `via: "native"` lines, and LANEB also got the message natively.

**A2A: deferred.** A2A (agent cards, tasks, HTTP transport) is built for networked agents from different vendors
finding and calling each other. Fleet lanes are tmux panes on one Mac that already share a filesystem mailbox, so A2A
adds discovery and transport they do not need. If a remote or third-party agent ever has to join, the path is an A2A
gateway lane that turns incoming A2A tasks into `peer_send` and replies from `peer_inbox`, leaving the mesh unchanged.

## WATCH UI (lib/fleet_listen.py)

`fleet listen ...` execs `python3 lib/fleet_listen.py ...` (stdlib only). Flags: `--source file|stdin|command|replay|teams-transcript`
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
  Unparseable lines are skipped. In the `file` source a JSON line `{"note": "..."}` with no `text` becomes a pane note
  (shown, never routed); `parse_utterance` and the WATCH `call` source skip it.
- `teams-transcript`: runs `whosaid teams transcript-live --jsonl` (ARG all digits: plus `--port ARG`; any other ARG,
  else `source.run`, replaces it) in a shell (own process group). Its stdout is JSONL: `{"status","at"}` -> source status `teams-transcript <status>` once per change,
  and on a change to `panel closed` one note `teams transcript panel closed; reopen it with: whosaid teams
  open-transcript`; `{"gap":true,"after","before"}` -> a note; a row `{"id","at","speaker","text","final"}` -> a line,
  an empty speaker becomes `unknown` with `unnamed: true` (first time: one note), missing `final` = true. A parsed
  line with `unnamed: true` is always drawn and served, and routed only when no speaker filter is set. Non-JSON and empty-text lines are
  skipped. Notes show in the pane and page and are never routed. Exit: `ended`, or `error: exit N` once.
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

## fleet feed (lib/fleet_feed.py)

`fleet feed ...` execs `python3 lib/fleet_feed.py ...` (stdlib only, issue #37).

- `start URL --token-file PATH [--name N (feed)] [--protocol whosaid|lines] [--stem S] [--from-start]
  [--since HH:MM:SS (lines only)] [--min-chars 12] [--batch 60] [--poll 3] [--header X-Token] [--parent LANE]
  [--bound 12h]`. Refuses (`FAIL ...`, exit 1): a token file that is not a regular file owned by the user with mode
  exactly 600, an empty one, a URL with userinfo, a non-http(s) URL, a bad name, `--since` or `--stem`, a feed
  of that name already running, and whatever `fleet side run` refuses (no parent lane in the roster). Writes
  `$FLEET_STATE/feeds/<name>/config.json` (0600; the token file's path, never the token), then runs `fleet side run
  --host feed --model <name> --bound <bound> --log feeds/<name>/log [--parent LANE] -- python3 lib/fleet_feed.py _run
  --dir feeds/<name>` and records `{"id","nonce"}` of that row in `feeds/<name>/side.json`. Prints
  `OK feed <name> <S#> pid=<pid> <protocol> <scheme://host[:port]/path>`, `transcript=<path>`, `log=<path>` and
  `follow it: fleet listen --source file <path>`.
- `_run --dir D`: holds `D/lock` (flock) for its life (a second one prints `FAIL feed <name> is already running`, exit 1);
  reads the token from the file once at start (refused file: exit 2). Appends to `D/transcript.jsonl`:
  utterances `{"clock","speaker","text","final":true}` plus `"stem","n"` (whosaid) or `"i"` (lines index), and
  `"unnamed":true` with speaker `unknown` for `them` or an empty speaker; and notes `{"note","at"}`. Text shorter than
  `--min-chars` is dropped. Lines are held and appended every `--batch` seconds (0: every poll); `D/state.json`
  (`key{protocol,url,stem}`, `stem`, `n`, `cursor`, `ended`, `began`, `from`, `sinceReached`, `unnamedNoted`, `size`)
  is replaced (tmp + rename) only after the append, and a transcript longer than `size` at start moves the position
  past the lines it holds. A changed protocol, URL or `--stem` starts a new position. SIGTERM/SIGINT/SIGHUP write the
  held batch and exit 0.
- whosaid: `GET <base>/sessions`, newest stem (or `--stem`); `GET <base>/sessions/<stem>` (no cursor: whole session)
  then `?cursor=C&wait=W` (W = 25, or `--batch` when smaller, at least 1; client timeout W + 15). Entries with
  `n <=` the saved max for that stem are skipped (so `reset`/`full` answers never duplicate). The first session seen
  at the feed's first start is skipped to its end unless `--from-start`; every later stem is read from its start.
  `meta.ended` or a 404 for the stem: status `gone`, then `/sessions` every `--poll` seconds for a larger stem
  (`--stem`: exit 0). Speaker = `me` for `who: me`, else `name`, else `who`. A name set later on an earlier line is not
  rewritten.
- lines: `GET URL?from=N` (other query keys kept), token in `--header`; only newline-terminated lines count toward N.
  First start: N = the current line count unless `--from-start` or `--since`. 404/410: status `gone`, keep polling.
- Status: `ok | stalled (3 failed polls) | auth refused | gone | stalled (bad request, feed stopped) | stopped` in `D/status`,
  one log line `source: http-feed <status>` and one note `source: http-feed <status>`, each only on a change. A failed
  poll is a network error, a timeout, a connection cut mid-answer, any other error in a poll, a non-JSON answer or an HTTP status other than 200/400/401/403/404; an idle long
  poll (200, no entries) is not one. 401/403: `auth refused`, exit 2. 400: exit 1. Any other exit (stop, bound, signal) ends on `stopped`; those two keep their reason.
- `ls`: one line per feed dir: `<name> <S#|-> alive|dead <status> <protocol> <safe url>`, or `no feeds`. `status N`:
  alive/dead, side run id and pid, source, position, transcript line and note counts, log path. `stop N`: SIGTERM to the
  side run's process group, waits up to 10 s for the row to end, `OK stopped feed N (S#)` (or `OK feed N is not running`).
- Never written anywhere: the token (stdout, log, status, state, transcript, side rows, messages). URLs are printed as
  `scheme://host[:port]/path`. GET only; redirects are not followed.

## Four desks (docs/design/four-desks.md, v0.2.0)

- `bin/fleet` dispatches `watch|learn|probe` to `bin/fleet-watch`, `bin/fleet-learn`, `bin/fleet-probe` (no role guard;
  each engine fences its own writes). A verb not built yet prints `fleet <x>: not built yet` and exits 2.
- WATCH contract: `lib/fleet_watch_contract.py` (Source interface, Event schema, `watch.json` validator with
  credential references only, `snapshot()` shape, `SOURCES` registry of `lib/watch_sources/<kind>.py`). Event:
  `sourceKind` is the source type, `kind` the routing class `ask|task-candidate|fyi`, `to` always `DECIDE`; a
  learn-item is an extra delivery for `score >= learn.minScore`. `poll()`'s `next_poll_sec` is a floor:
  wait = max(60, pollSec, next_poll_sec). The engine injects `cfg["me"] = {"names", "id"}` per source.
- Probe contract: `lib/fleet_probe_contract.py` (`decide/probes.json` targets, `decide/probes.jsonl` rows
  `{id,target,lane,cli,startedAt,endedAt,tokensIn,tokensOut,found,callId,status,eid}`, `decide/probes-nuked.json`,
  decisions-log rows with `decidedBy: decide:probe`, `human: false` for alone actions).
- `fleet learn` (bin/fleet-learn -> lib/fleet_learn.py). State: `$FLEET_STATE/learn/{ledger.jsonl, cards.jsonl,
  reviews.jsonl, proof.json, cursors.json, learn.json, export/}`; every write resolves inside `learn/`.
  Schemas and the Python API (add_item, add_learn_item, confirm, supersede, drop, items, item, sync, make_cards,
  due, review, reviews, use_scheduler, rebuild_proof, snapshot, export): lib/fleet_learn_contract.md.
  `FLEET_LEARN_NOW=<ISO UTC>` pins the clock. Exit codes: 0 ok, 1 refused (`FAIL ...`), 2 usage.

### fleet learn grader and scheduler (lib/fleet_learn_explain.py, lib/fleet_fsrs.py)

```
$FLEET_STATE/learn/learn.json: {"grader": {"kind": "command", "run": "<shell command>", "timeoutSec": 20, "maxTokens": 400},
             "scheduler": "fsrs" (default) | "fixed", "fsrs": {"desiredRetention": 0.9, "weights": [21 floats], "maximumInterval": 36500}}

stdin:  one JSON {"card": {cid, lid, format, prompt, grounding}, "answer", "quote", "rubric", "format", "maxTokens"}
stdout: one item per line: `KIND: text | quote: <verbatim words from the source> [| minor]`
        KIND is COVERED, MISSING, WRONG or NOTE; any other line is ignored and counted.
bound:  killed (process group) after timeoutSec. A model behind the command needs its own max_tokens and deadline.
```

- An item's quote must be a normalized substring of the card's grounding.quote (apply cards: or its rubric).
  A WRONG line with no quote, or a quote not in the source, is dropped; the same rule holds for COVERED and MISSING.
  A line carrying a secret is dropped and its text is not recorded.
- pass = at least one grounded COVERED, no grounded WRONG, no grounded MISSING without `| minor`.
  Rating handed to the scheduler: good on a pass, again on any fail (a fail is a lapse in proof.json).
- Timeout, non-zero exit, or no grounded item -> self-grading with the source shown (gradedBy: self). Never a pass by
  silence. No grader configured -> self-grading.
- A bad `fsrs` section in learn.json is refused when the scheduler is selected, never silently defaulted.

### fleet watch verbs (bin/fleet-watch, lib/fleet_watch.py)

```
fleet watch run [--once] [--force] [--source NAME] [--json]   poll due sources; route to DECIDE and LEARN, never ROUTE
fleet watch check [--config PATH] [--resolve]                   validate watch.json; exit 1 on a literal credential
fleet watch status [--once] [--interval S]                      terminal status pane (j/k select, p poll, z pause, r resume, h release held, q quit)
fleet watch stats [--json]                                      counts per kind, dropped per rule
fleet watch snapshot [--compact]                                snapshot() JSON
fleet watch poll-now|pause|resume NAME, fleet watch release-held   the pane's verbs (shared with `fleet watch serve`)
fleet watch webhook serve [--config F] [--bind A] [--port N]    optional loopback webhook receiver (7349)
```

State files are listed in docs/design/four-desks.md section 6. Rate: `ratePerMin` (default 30, per source or in
`defaults`) feeds a per-source token bucket around `urllib.request.urlopen`; a wait over 5 s defers the poll, and
`fleet watch run --json` lists those under `deferred`. LEARN-ITEMs go to `$FLEET_LEARN`, else `LEARN`; the
engine's send refuses `ROUTE` and the `orch_name()` value. Every ask's `decision_open` carries
`watch: {sourceKind, source, ref, url (http/https only), eid}`.

### WATCH sources: per-kind config keys and change types

- `ado` (Azure DevOps Boards): `{"name", "kind": "ado", "org", "project", "auth": {"type": "pat", "token": "env:ADO_PAT"}}`.
  Two-step poll (WIQL with `timePrecision=true`, then a batch get); cursor = last ChangedDate + ids at that instant.
  Keeps a per-item memory in `state["items"]` (at most 2000) to tell transition, assigned and comment apart, so the
  engine must persist Source.state. Webhooks: a `webhook` route `{"for": "<ado source>", "signature": "basic",
  "auth": {"type": "basic", "user": <name or ref>, "secret": <ref>}}`; the receiver's built-in basic verifier checks
  it (ADO service hooks sign nothing), and `accept_webhook` only normalizes, taking previous values from the hook's
  `oldValue`/`newValue` pairs so the eid matches the poll's. Boards only; ADO Repos pull requests are not covered.
- `gitlab` (GitLab and Gitea, one module): `{"name", "kind": "gitlab", "flavor": "gitlab"|"gitea", "base",
  "repos": [...], "branches": [...] (optional), "lookbackSec" (optional, first-poll window, default pollSec),
  "auth": {"type": "bearer", "token": "env:..."}}`. Token scope: read_api (GitLab) / read:repository (Gitea).
  Webhooks: a `webhook` route `{"for": "<gitlab source>", "signature": "gitlab"|"gitea", "auth": {"type": "hmac",
  "secret": <ref>}, "pollSec": 60}`. gitlab.py registers both verifiers at import: `gitlab` checks a `whsec_`
  token's Standard Webhooks `webhook-signature` inside a 300 s `webhook-timestamp` window, else the plain
  `X-Gitlab-Token` in constant time; `gitea` checks `X-Gitea-Signature` (hex HMAC-SHA256 of the body, empty refused). Change types: `pr.opened`, `pr.review_requested`, `pr.pushed`,
  `pr.merged`, `pr.closed`, `pr.reopened`, `pr.updated`, `branch.pushed`; tags `review-requested`, `mention`,
  `assigned-to-me`, `my-pr`.
- `github`: `{"name", "kind": "github", "repos": ["owner/repo", ...], "auth" (optional: `{type: bearer, token:
  "cmd:gh auth token"}`), "backfill" (false: the first poll of a repo primes the cursor and emits nothing)}`. Cursor:
  per repo ETag, recent event ids and lastAt, plus the notifications Last-Modified. `next_poll_sec` returns the
  server's `X-Poll-Interval`, or the largest `Retry-After` of a repo that failed while others succeeded (a partial
  failure returns events, not an error). Unauthenticated: no `/notifications`, and every request (304s too) spends
  one of 60/h, so with `pollSec` 900 about 15 quiet repos fit; past ~10 repos add a read-only token. Without
  `me.id` no `me`, `review-requested`, `assigned-to-me` or `mention` tags. `review_requested` reaches WATCH only via
  `/notifications` or a webhook. Webhooks: the receiver's `github` verifier (`X-Hub-Signature-256`).
- Engine duty every adapter relies on: save the Source's `state` after every poll, including one that RAISES
  (`failStreak` drives the exponential backoff; GitHub's backoff test builds a fresh Source per poll on one dict).
- `jira`: `{"name", "kind": "jira", "base", "auth", "deployment" ("cloud" when the host ends in .atlassian.net,
  else "server"), "query" (JQL scope, ANDed with the relative updated window; a query with ORDER BY is refused),
  "instance" (default: the host), "sinceSec" (0)}`. Cursor `{updated, ids}`; `state["myself"]` caches `/myself` ids
  (no secret), so mentions and assignments resolve without `me.ids`. `health()` adds `truncated` (changes over
  `maxEventsPerPoll` for one issue, oldest kept). Webhook route: `signature: "hmac"`, `header: "X-Hub-Signature"`.
- `bitbucket` (Bitbucket Cloud): `{"name", "kind": "bitbucket", "repos": ["ws/repo"], "branches": [...], "auth":
  {"type": "bearer", "token": <ref>} (or basic: user + app password / API token), "api" (default
  https://api.bitbucket.org/2.0), "web" (default https://bitbucket.org), "maxRequestsPerPoll" (30)}`. Read scope:
  repository read and pull request read. Webhook route: `"signature": "bitbucket"` (bitbucket.py registers it at
  import; `X-Hub-Signature: sha256=<hex HMAC>`). `pullrequest:updated` emits nothing (a review request for me
  excepted); the next poll reports `pr.pushed` or `pr.updated`. `rateRemaining` is always null.
- `confluence`: `{"name", "kind": "confluence", "base" (with `/wiki` on Cloud), "auth", "spaces": [...],
  "timeZone": "+HH:MM" (the API user's profile zone, CQL dates are read in it; default "+00:00"), "overlapSec" (3600),
  "pageLimit" (50), "cql" (extra clause, ANDed), "firstLookbackSec" (0), "maxPages" (20)}`. Cursor `{since, ids}` plus
  `pending: {next, hi, hiIds}` while a poll stopped with pages left. Change types `page.created`, `page.edited`,
  `page.commented`; tags `mention`, `my-page`. No webhooks.
- The engine puts `maxEventsPerPoll` and `quoteChars` (from `source_settings`) and `me` (`source_cfg_with_me`) into
  the cfg a Source is built with.

### WATCH webhook receiver (lib/watch_sources/webhook.py; `fleet watch webhook serve`)

- `fleet watch webhook serve [--config F] [--bind 127.0.0.1|::1] [--port N]`: loopback only. Exit 2 for a
  non-loopback bind, a bad config or no webhook sources; 0 on Ctrl-C. Default port 7349.
- The receiver is the only place a signature is checked (VERIFIERS: `github`, `hmac`, `basic`, plus any name an
  adapter registers with `register_verifier` at import). It then calls the target adapter's `accept_webhook` with
  empty creds (normalize only) and appends the Events to the route's spool.
- State: `$FLEET_STATE/watch/webhook/<route>.jsonl` (`state_paths(...)["webhook"]`), mode 0600, directory 0700.
  The `webhook` source's poll() drains it (cursor = byte offset, kept in cursors.json by the engine). A fully
  drained spool over 1 MiB is truncated.
- watch.json `webhook` source keys: `name`, `kind: "webhook"`, `for` (optional, a polled source name),
  `signature` (`github` | `hmac` | `basic` | a registered name), `header`/`prefix`/`algo` (hmac only),
  `auth {type: "hmac", secret: <ref>}` (or `{type: "basic", user: <name or ref>, secret: <ref>}`), `bind`
  (loopback only), `port`, `pollSec`. One receiver serves every route, so all routes agree on bind/port.
- Latency: the engine drains a route's spool every `pollSec`; set `pollSec: 60` (the floor) on webhook sources.
  Both the receiver and the engine resolve the route's secret reference; an unresolvable ref fails that poll.
- HTTP: `POST /hook/<name>`. 202 `{accepted: n}`; 200 for a GitHub ping; 401 bad or missing signature; 404
  unknown route; 405 not POST; 411 no Content-Length; 413 over 1 MiB; 501 target kind has no mapping; 503
  secret did not resolve. Responses never carry the secret or a digest.

## Side runs (lib/fleet_side.py; `fleet side ...`)

A side run is a process a lane (the parent) starts outside the fleet's tmux panes: codex, agy, gemini, an `ach run`,
any CLI. The kit tracks only its pid, log path and result path, so it stays host-agnostic.

- Store: `$FLEET_STATE/sides.jsonl`, one JSON row per run, rewritten whole under an exclusive flock on the file
  (the `fleet_probe._flock_rw` pattern). Never in `roster.json` (jq rewrites it with no lock).
- Row: `{"id": "S<n>", "parent", "host", "model", "pid", "log", "result", "boundSec", "startedAt", "status",
  "endedAt", "exit", "tokensIn", "tokensOut", "children", "notifiedAt", "source", "sessionId"}`. `id` is the highest `S<n>` + 1,
  allocated under the lock. `log`/`result` are absolute (or null). Times are UTC `...Z`. `status`: `running | done |
  failed | timeout | killed | orphan`. `children` starts at 0, `source` is `cli` for `fleet side start`.
  `sessionId` is the host's session/thread id (a string) or null; #13 usage reads it to find codex rollouts.
- `fleet side start --parent NAME --host H [--model M] --pid PID [--log P] [--result P] [--bound 30m] [--session-id ID]`:
  prints the id.
  `--parent` defaults to `$FLEET_NAME`. `--bound` takes `90s`, `30m`, `2h` or seconds; default 30m. Refused (stderr
  `FAIL <reason>`, exit 1): a parent not in the roster (`no lane NAME in the roster`), a pid that isn't alive, a
  bound over 12h.
- `fleet side end ID [--status done|failed] [--exit N]`: sets `status`, `endedAt` and `exit`. Idempotent: a row that
  already has `endedAt` is left unchanged (exit 0). Unknown id: exit 1.
- `fleet side ls [--parent NAME] [--json]`: one line per row, or the rows as a JSON array.
  `fleet side show ID`: the row as JSON; unknown id exits 1. No subcommand or an unknown one: usage, exit 2.
- Python API (`import fleet_side`): `start(**fields) -> row` (fields include `sessionId=`; raises `SideError` when refused), `end(id, status,
  exit=None) -> row`, `update(id, fn) -> row` (fn gets a copy of the row and returns the new row, or None for no
  change), `rows(parent=None)`, `get(id)` (None when unknown).
- Extending: add the subcommand to `COMMANDS` in `lib/fleet_side.py` (one line) and keep its logic in its own
  module (`fleet_side_watch.py`, `fleet_side_run.py`, `fleet_side_ach.py`).

## Side runs over MCP (lib/fleet_side_mcp.py, issue #16)

`fleet_mesh.py serve` lists three more tools (`TOOLS` in `lib/fleet_mesh.py`; the `t_side_*` wrappers import
`lib/fleet_side_mcp.py` lazily, which calls the `fleet_side` API):
- `side_start {host, pid, model?, log?, result?, bound?, parent?, sessionId?}` -> the id (`S1`). `parent` defaults
  to `$FLEET_NAME` (same rule as `_sender`); `source` is `mcp`. Goes through `fleet_side.start`, so the run gets
  whatever `fleet side start` gets (the #14 watcher spawn lives there).
- `side_end {id, status?: done|failed, exit?}` -> `S1 done endedAt=...`, the CLI line. Idempotent like the CLI.
- `side_list {parent?, all?}` -> one `<id> <status> <fleet_side_usage.fmt(row)>` line per run, e.g.
  `S1 running codex gpt-x 0m/30m ? tok`; `no side runs` when empty. `parent` defaults to `$FLEET_NAME` (every lane when unset), `*`
  lists every lane; ended runs are left out unless `all=true`.
- Refusals (`no lane NAME in the roster`, dead pid, bound over 12h, `no side run S9`, bad status) come back as
  `isError: true` with `error: <reason>` through `call_tool`; the server keeps running.

## Side runs follow the parent (lib/fleet_side_lifecycle.py, issue #18)

- `fleet park NAME [--keep-sides]` and `fleet stop NAME [--keep-sides]`: before the pane is killed (after park's
  compact), every `running` side run of NAME is stopped: SIGTERM, then SIGKILL after `FLEET_SIDE_KILL_GRACE` seconds
  (default 5), and the row gets `status=killed`, `endedAt` and `notifiedAt` (so no completion message reaches the
  parent that is going away). The signal goes to the run's process group when the pid leads its own group (and it is
  not the caller's group), else to the pid alone. A failure warns and never blocks the park or stop.
  `--keep-sides` leaves the pids alive and sets `status=orphan` (no `endedAt`); the watcher still enforces the bound.
  Prints `OK stopped N side run(s) of NAME (S1 ...)` or `OK kept ...`. A park refused before the kill (unconfirmed
  compact) touches no side run. `fleet reopen` revives nothing: killed rows stay killed.
- `fleet cleanup` section `(6) side runs whose parent is parked, done or gone, or whose pid outlived its bound:`,
  one line per live (`running|orphan`, pid alive, not a zombie) run:
  `  ORPHAN-SIDE S3 parent=L pid=1234 age=41m bound=30m why=parked|done|archived|gone|over-bound`. Gone = no roster
  row, or a live row in cleanup's (3) (pane gone). A dry run and `--apply` alone change nothing here;
  `--apply --kill-orphans` kills them the same way (`status=killed`, prints `OK killed side S3 pid=1234`).
- `fleet side stop-parent NAME [--keep-sides]` and `fleet side orphans [--dead A,B] [--json] [--kill]` are the CLI
  hooks the above call. Python: `fleet_side.stop_for_parent(name, keep=False) -> [rows]`;
  `fleet_side_lifecycle.orphans(dead=(), now=None)` (rows plus `why`, `ageSec`), `kill_orphans(dead=())`,
  `kill_run(row, grace=None) -> bool`.

## Side-run watcher (lib/fleet_side_watch.py, issue #14)

One detached `fleet side watch ID` per run: it detects completion, enforces the bound, and sends the parent exactly
one inbox message.

- Spawn: `fleet_side.start()` (so `fleet side start` too) calls `fleet_side_watch.spawn(id)`: `setsid`, stdin
  `/dev/null`, stdout/stderr to `$FLEET_STATE/log/side-watch.log` (the `fleet_mesh.spawn_waker` pattern).
  `FLEET_SIDE_WATCH_SPAWN=0` turns spawning off. One watcher per id: each holds an flock on
  `$FLEET_STATE/side/<id>.watch.lock` for its life; a second one exits 0 at once. Unknown id: `FAIL no side run ID`, exit 1.
- Each poll (`FLEET_SIDE_POLL`, default 5 s): the run is finished when the pid is gone (a zombie counts as gone) or
  the result file has stayed unchanged (mtime, size) for 2 polls (never for `source=run` or `source=ach` rows: a
  `fleet side run` supervisor waits on the real child and records its exit and tokens, issue #42); `status=done`, or `failed` when `exit` is non-zero
  or the result is an ach RunResult (`runId`) with `exitStatus != "success"`. Past `startedAt + boundSec`: SIGTERM to
  the pid and its descendants, SIGKILL to what is left after `FLEET_SIDE_GRACE` (default 10 s), `status=timeout`.
  `sample_children` (#13) runs every poll and the max lands in `children`. `endedAt` set by `fleet side end` ends
  the loop with that status. The watcher's own bound is `boundSec + grace + 60 s` (then `status=orphan`).
- On finishing: `tokensIn`/`tokensOut` from `usage()` (#13), then one message to the parent via
  `fleet_mesh.deliver`, sender `side:<id>`:
  `side S3 codex gpt-x done in 7m12s (bound 30m), 2.1M tok in / 40k out, 2 helper procs; result /abs/result.json; log /abs/log`.
  Helper procs = `children - 1` (the tree minus the root); unknown tokens print `?`; a missing path prints `-`.
- Exactly once: `notifiedAt` is claimed under the store lock (only when null) before `deliver` runs, not after, so a
  slow wake never holds the store lock and a second watcher, a restart or `fleet side end` never sends a second
  message. (Deviation from the issue text, which sets `notifiedAt` after delivering.)
- Logs: `side watch <id>: ...` lines in `$FLEET_STATE/log/fleet.log`.
- API: `spawn(id)`, `watch(id)`, `finish(id, status, max_children) -> row`, `notify(id)`, `message(row)`, `dur(sec)`.

## Side-run usage (lib/fleet_side_usage.py, issue #13)

Pure functions over files; nothing here reads or writes `$FLEET_STATE/sides.jsonl`.

- `usage(row) -> {"tokensIn", "tokensOut", "children", "source"}`: each value an int or `None` (unknown, never 0).
  Token sources, first hit wins: `row["result"]` as an ach RunResult (`tokens[]`, in = input + cacheRead +
  cacheWrite, `source="ach"`); `row["result"]` as `{"tokens": {"in": N, "out": N}}` (`source="result"`);
  `host == "codex"` with `row["sessionId"]` via `fleet_probe.tokens_for("codex", sid)` plus every helper session
  below it (`source="codex"`). `children` is `row["children"]` as stored.
- Codex helpers: a rollout's first line `session_meta` names its parent in `payload.parent_thread_id` or
  `payload.source.subagent.thread_spawn.parent_thread_id` (checked on codex 0.153.4 to 0.166.0). Helpers of helpers
  count too. `CODEX_HOME` overrides `~/.codex`.
- `sample_children(row) -> int`: live (non-zombie) processes in the tree rooted at `row["pid"]`, root included, from
  `ps -axo pid=,ppid=,stat=`; 0 when the root is gone. The watcher stores the max over polls in `row["children"]`.
- `fmt(row, now=None) -> str`: `codex gpt-x 7m/30m 2.1M tok` (host, model if set, elapsed/bound in minutes, in + out
  tokens); the clock stops at `endedAt`; unknown tokens print `? tok`.

## Side-run wrapper (lib/fleet_side_run.py, issue #15)

`fleet side run [--parent NAME] --host H [--model M] [--bound 30m] [--log P] [--result P] -- CMD [ARGS...]`
launches CMD detached, registers it (`source="run"`) and prints `S<n> pid=<child pid> log=<abs> result=<abs>`, then
returns without waiting (well under 1 s).

- Defaults: log `$FLEET_STATE/sides/<id>/log` (stdout and stderr, appended), result
  `$FLEET_STATE/sides/<id>/result.json` (CMD may write it; nothing is created there for it).
- Process: CMD runs in its own session and process group (pid = pgid = sid), with no controlling tty and stdin from
  `/dev/null`. It never reads or types into a tmux pane.
- Env: `FLEET_SIDE=<id>`, `FLEET_PARENT=<parent>`, `FLEET_NAME=<parent>`, `FLEET_SIDE_RESULT=<result path>`
  (#12 reads the first two).
- Supervisor: a small process in its own session (row field `supervisorPid`) waits on CMD. It reads the exit with
  `waitid(WNOWAIT)`, so the child pid stays a zombie (still "alive" to `pid_alive`) until the supervisor has called
  `end` and filled `tokensIn`/`tokensOut` from `fleet_side_usage.usage(row)` where they are null; then it reaps.
  A watcher therefore never sees the pid gone before `exit` is in the row. Status: exit 0 `done`, non-zero `failed`,
  killed by a signal `timeout` when past `startedAt + boundSec`, else `killed`; `exit` is the code, or 128 + signal.
  `end` is idempotent, so a watcher that ended the row first (timeout) wins. SIGTERM, SIGINT or SIGHUP to the
  supervisor is forwarded to CMD's process group.
- The row's `pid` briefly holds the supervisor pid (registration needs a live pid before the id exists), then is
  pointed at CMD before `run` prints. The #14 watcher spawned by `start` sends the one completion message.
- Refused (stderr `FAIL <reason>`, exit 1, no row written): no command after `--`, no parent, a parent not in the
  roster, a bad bound.

## Side runs in the roster views (lib/fleet_side_roster.py, issue #17)

Read-only over `sides.jsonl` and the roster. A run is shown while it has no `endedAt` (status `running`, or
`orphan` set by the watcher); ended runs are hidden here and listed by `fleet side ls` (`--all` is accepted and is
the default: every row).

- Child line, one per shown run, right under its parent's line: `  -> S3 codex gpt-x 7m/30m 2.1M tok`
  (`fleet_side_usage.fmt`; stored tokens, else the result file's tokens, else `? tok`), in id order. ` ORPHAN` is
  appended when the parent is `parked`, `done` or `archived`, is not in the roster, or the row's status is `orphan`.
- `peer_list`: child lines after each lane line; runs whose parent is not in the roster follow at the end under a
  `<parent> (gone)` header.
- `fleet tick`: the same lines under each lane's row of the text table and after it for gone parents (also when
  the table is `(no lanes)`). `--json`: each fact gains `"sides": [row + {"line", "orphan"}]` (empty list when
  none). The lifecycle's python finds the module through `FLEET_KIT_LIB` (set by `pyh`).
- `fleet ls`: the same child lines under each lane, then the gone block.
- `fleet side roster [--gone]`: `<parent>\t<line>` per shown run whose parent is in the roster (what `fleet ls`
  reads); `--gone` prints the gone block.
- API: `visible(row)`, `is_orphan(row, roster)`, `line(row, roster, now)`, `side_rows(roster, now) ->
  {parent: [rows]}`, `child_lines(name, roster, now)`, `gone(roster, now)`, `gone_lines(roster, now)`.

## Side runs from ach (lib/fleet_side_ach.py, issue #19)

Registers `ach` runs as side runs with no change to ach, from its run registry: one record per run at
`$AGENTIC_CODING_HARNESS_STATE_DIR/runs/<runId>.json` (default `~/.agentic-coding-harness`; agentic-coding-harness
`src/core/store.ts` `stateDir`, `src/core/registry.ts` `RunRecord`).

- `fleet side adopt-ach [--apply]`: scans records with `status=running`. For each, the parent lane is the first
  roster lane (not parked/archived) whose `pid`, or whose pane's tmux `#{pane_pid}` (`$FLEET_TMUX_SOCKET`), is on
  the record `pid`'s ppid chain (one `ps -axo pid=,ppid=,args=`). A match not yet registered (no side row with
  `source=ach` and the same `achRunId`) is registered through `fleet_side.start` with `host=<agent>`,
  `model` = `metadata.model` (else `model` from `<--output-dir>/invocation.json`, the dir read from the ach
  process's argv, a relative dir joined to the record's `cwd`), `boundSec` = `FLEET_SIDE_ACH_BOUND` (default 30m), `log` = `rawTranscript`, `result` = the record
  file, `sessionId`, `source=ach`, `achRunId=<runId>`. Output, one line per record:
  `S1 parent=L host=codex source=ach achRunId=... pid=N` (applied), `would register parent=L ...` (dry run, the
  default), `skip <runId>: pid N not alive | no lane ancestor for pid N | <start refusal>`. Nothing to scan:
  `(no running ach runs to adopt)`. Idempotent: a second `--apply` registers nothing; `--apply` holds
  `$FLEET_STATE/side-adopt-ach.lock` for the whole scan, so concurrent adopters register a run once.
- `fleet tick` runs it as a dry run and prints only matches, prefixed `side adopt-ach: `; `fleet tick --adopt`
  applies. With `--json` the dry run is skipped and `--adopt` output goes to stderr, so stdout stays pure JSON.
  No daemon.
- Usage (#13): `fleet_side_usage.result_tokens` also reads an ach RunRecord (`runId` + `totals` dict): in =
  `inputTokens + cacheReadTokens + cacheWriteTokens`, out = `outputTokens`, `source="ach"`.
- Completion (#14 watcher): `fleet_side_ach.finish_status(row)` returns `None` while a `source=ach` row's record
  says running, else `done` (`success`) or `failed` (`error | aborted | unavailable | interrupted`); `None` for any
  other row. `fleet_side_watch._loop` calls it each poll and skips its "result unchanged for 2 polls" rule for
  `source=ach` (the record exists from the start and sits unchanged while the run works); an ach pid gone with the
  record still `running` ends `failed`. `fleet side adopt-ach --apply` goes through `fleet_side.start`, so each
  adopted run gets its watcher.

## bootstrap.sh (`fleet bootstrap ...`, issue #40)

```
bootstrap.sh [--dry-run|-n] [--verify] [--no-desks] [--cli C] [--pubkey KEY|FILE]    also: curl -fsSL .../bootstrap.sh | bash [-s -- FLAGS]
fleet bootstrap ...                                                                 same flags; role-guarded like lane lifecycle
```
- curl|bash: body is one function, `fleet_bootstrap "$@"` is the last line. Piped (no `BASH_SOURCE` file), it clones
  `FLEET_KIT_REPO` (default the public repo) to `FLEET_KIT_DIR` (default `~/.local/share/tmux-agent-fleet-kit`), reuses an
  existing clone without pulling, and execs the clone's `bootstrap.sh`. `--dry-run` piped prints `would clone` and stops.
- Steps: `1/5 packages`, `2/5 agent CLIs`, `3/5 ssh PATH`, `4/5 phone key`, `5/5 desks`, then `root steps (sudo half)` and
  `attach from the phone`. The attach line is the last line printed.
- `~/.zshenv` block markers: `# >>> fleet bootstrap: ... >>>` / `# <<< fleet bootstrap <<<`. Not edited when the file is a
  symlink or matches `dotfiles|chezmoi|yadm|home-manager|nix-darwin|stow|managed by|do not edit` (case-insensitive).
- Root script: `$FLEET_STATE/bootstrap-root.sh` (default state `~/.local/state/fleet`); bootstrap prints
  `! sudo bash <path>` and `fleet bootstrap --verify`. `--verify` execs `fleet remote verify`.
- Env: `FLEET_BIN` (the fleet binary to call), `FLEET_NO_DESKS`, `FLEET_KIRO_BIN`, `FLEET_AGY_BIN`, `FLEET_BOOTSTRAP_OS`.

## AUTH desk (docs/design/auth-desk.md, v0.10.0)
- `fleet auth <verb>` (`bin/fleet-auth` -> `lib/fleet_auth.py`). State under `$FLEET_STATE/auth/`: `auth-log.jsonl`
  (append-only, one JSON row per event, ids `A<n>`), `slot.json` (the one sign-in in progress), `freeze.json`,
  optional `helpers.json` (the human's, never written by the kit; `FLEET_AUTH_HELPERS` overrides the path). Writes
  hold a `flock` on `auth/.lock`. Text that looks like a secret (`fleet_listen.is_secret`) is refused with `FAIL`
  before anything is written; an unwritable inbox or state file is `FAIL` too, never a traceback.
- `ask --what W [--why Y]` (anyone): logs `requested` and tells AUTH (`fleet_mesh.deliver`). Prints the id.
- `begin --what W [--flow session|password] [--for LANE] [--match NN] [--ask A<n>]` (role auth or unset): refused
  (exit 1) when frozen or while another slot is held. A slot older than `FLEET_AUTH_SLOT_SECS` (900) is broken first
  (`slot-broken` row, `WARN`). Sends DECIDE `AUTH heads-up: sign-in A<n>, <what> for <lane>, starting now (<flow>);
  ...` before it logs `started` (with `flow`). `--flow session` reuses an already signed-in browser session and is
  preferred; `password` (default) is a fresh sign-in. With `--match`, prints the big-digit banner too.
- `number NN [A<n>]` (auth or unset): prints `MATCH THIS NUMBER (...)` and a 5-row block banner of the digits, logs
  `number-shown` (id, at, for, match), sends DECIDE `AUTH A<n>: match NN now (...)`. Refused with no open sign-in.
- `end A<n> --result ok|failed|cancelled [--note N]` (auth or unset): logs `ended`, frees the slot, tells the lane.
  `ok` is `ok-unconfirmed` until the human confirms.
- `unexpected-push [--at TIME] [--note N] [--quote Q]` (anyone; decide needs `--quote`): logs `push-unexpected` with
  `near` (the rows within 10 min of TIME) and sends DECIDE `AUTH ANOMALY A<n>: ...`. A push during an announced
  sign-in is expected and needs no verb.
- `confirm A<n> --as-expected|--unexpected [--quote Q]` (unset, or role decide with `--quote` carrying the human's
  typed words). `--unexpected` also runs `unexpected-push` for that sign-in.
- `freeze --reason R` / `thaw` (route, decide or unset). `log [--json]`, `trace TIME [--window MIN] [--json]`,
  `status [--json]` (slot, freeze, openAsks, verdicts, numbers, anomalies), `helpers` (anyone).
- Verdict per sign-in: `open` (started), `broken` (slot broken), `ok-unconfirmed` / `fail` (ended), `pass` (confirmed
  as expected), `fail` (an unexpected prompt).
- `fleet park` refuses a standing desk (roster role route, decide, watch, learn or auth): `FAIL <NAME> is a standing
  desk (role R) and is never parked; to retire it: fleet handoff <NAME>, then fleet stop <NAME>`, exit 1.
