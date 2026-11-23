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
| FLEET_WAKE_DEFER_MAX | `900` | seconds a deferred typed wake keeps retrying before it leaves a notice instead (see Typed wakes) |
| FLEET_WAKE_BACKOFF | `2` | first retry gap of a deferred wake, in seconds; doubles, capped at 30 |
| FLEET_WAKE_QUIET | `3` | a pane that got keys from an attached client this many seconds ago counts as busy; `0` turns the check off |
| FLEET_NATIVE_GRACE | `120` | seconds a Claude to Claude `peer_send` waits for the sender's `SendMessage` before the typed fallback |
| FLEET_NATIVE | `1` | `0` turns the Claude to Claude native route off (every Claude target gets the guarded typed wake) |
| FLEET_WAKE_SPAWN | `1` | `0` never starts a detached waker (unit tests); a busy pane then just keeps its unread message |
| FLEET_AGY_HOME | `~/.gemini` | where agy keeps `config/` and `antigravity-cli/` (agents, rules, mcp_config.json, brain/<conversation>/ transcripts); passed to agy lanes when set; read by install/agy.sh and bin/fleet-lifecycle. agy itself always uses `$HOME/.gemini` (PROVEN 1.5.13), so this must point there |

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
```
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

## DECISIONS UI (lib/fleet_decisions.py; `fleet decisions ...`)

```
fleet decisions serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "DECISIONS UI: http://127.0.0.1:P/?t=TOKEN"
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

## LEARN page (lib/fleet_learn_serve.py; `fleet learn serve`)

    fleet learn serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "LEARN UI: http://127.0.0.1:P/?t=TOKEN"

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

    fleet watch serve [--port P] [--bind 127.0.0.1|::1|localhost] [--open]   first stdout line: "WATCH UI: http://127.0.0.1:P/?t=TOKEN"

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

7340 DECISIONS UI (`fleet decisions serve`), 7341 LEARN page (`fleet learn serve`), 7342 WATCH page (`fleet watch serve`),
7349 WATCH webhook receiver (`fleet watch webhook serve`). 7343-7348 are free.

## lib/fleet_mesh.py

```
python3 lib/fleet_mesh.py serve                 stdio MCP server (JSON-RPC 2.0, newline-delimited, protocol 2025-06-18)
python3 lib/fleet_mesh.py send <from> <to> <text>    CLI delivery (same code path as peer_send)
python3 lib/fleet_mesh.py inbox <name>          print unread, advance cursor
python3 lib/fleet_mesh.py roster                print roster.json
python3 lib/fleet_mesh.py mirror                PostToolUse hook: copy a native SendMessage into the mesh (stdin = hook JSON, exit 0)
python3 lib/fleet_mesh.py wake-later <name>     deferred waker (started detached by a deferred wake; one per lane)
python3 lib/fleet_mesh.py pane-state <pane>     "empty <why>" exit 0, or "busy <why>" exit 1: may a wake be typed now
python3 lib/fleet_mesh.py type <pane> <text>    guarded type + Enter (the same checks, lock and Enter rule as a typed
                                                wake): "sent" exit 0, "failed" exit 1, "busy <why>" exit 3 (fleet park)
```
MCP tools: `peer_list {}`, `peer_send {to, text, from?}`, `peer_inbox {name?, all?}`,
`decision_open {question, options?: [str], from?}` (appends decisions.jsonl and delivers to $FLEET_DECIDE),
`decision_list {status?}`, `decision_answer {id, answer}` (marks answered, delivers to the asker only; see Messaging routing),
`decision_report {id, ok, note?}`, `decision_find {text}` (see DECISIONS UI). `decision_open` also takes `allow?: [pick|edit|own|snooze|refuse]`.
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
- claude, any other sender: `tmux [-L sock] send-keys -t <pane> -l "[fleet] message from <frm>: run peer_inbox to read it"`
  then `send-keys -t <pane> Enter`, guarded (see Typed wakes: only into an empty, quiet input box)
  (never the message body itself: typed input is treated as the human's). PROVEN on Claude 2.1.330.
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

**Typed wakes (claude, kiro, agy).** `type_wake()` never types into text a human is writing. Before typing:
1. No attached client whose current pane is the target sent a key in the last `FLEET_WAKE_QUIET` seconds
   (`list-clients -F '#{pane_id} #{client_activity}'`).
2. The pane is not in copy mode, and no vim `-- NORMAL --` line shows. With Claude Code's `editorMode: vim` (`~/.claude.json`, or `$FLEET_CLAUDE_JSON`), a `❯` prompt also needs `-- INSERT --` in the footer, because Claude Code shows nothing in NORMAL mode.
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
exactly the pointer (whitespace ignored, wrapped rows joined). A box that holds nothing but an unsent fleet pointer (an
earlier timeout) gets its Enter on the next wake instead of blocking every later wake. `fleet park` types `/compact`
through the same guard (`fleet_mesh.py type`) and refuses to park when the box is busy.
**Residual race:** a key the human presses in the milliseconds between the last check and `send-keys` still lands next
to the pointer. The post-type check then sees more than the pointer and sends no Enter, so the human's half sentence
is never submitted; the pointer stays in their box for them to delete. The kit cannot remove it without deleting
their keys too. PROVEN on throwaway panes (tmux -L) of Claude Code 2.1.330, kiro-cli 3.5.0 and agy 1.5.13: an empty
prompt got the wake and the lane answered (190-char pointer wrapped on a 120-column pane, too); a pane holding
`good morni` or `abc` with the cursor at the start was left exactly as it was.

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
| claude, from a live fleet Claude lane | none typed: the sender's own `SendMessage` (native cross-session message), typed fallback after `FLEET_NATIVE_GRACE` | PROVEN 2.1.330: a bypass lane's `SendMessage` to a bypass session ran a background turn with `good morni` still in the receiver's box |
| claude, from anyone else | guarded typed pointer line + Enter (`tmux send-keys`), only into an empty input | PROVEN 2.1.330 (hand-launched lane, launcher flags): idle bypass lane ran `peer_inbox`, cursor advanced |
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
