# Changelog

## Unreleased

AUTH, a fifth standing desk, owns every login and auth problem for the fleet (#45).

- `fleet desks up` starts five desks. AUTH sits in window deskA, always open right below DECIDE (ROUTE | DECIDE over AUTH), so the human reads and types the numbers there while DECIDE shows the heads-up. Adding AUTH to a four-desk fleet leaves the other panes alone; a dead DECIDE comes back above AUTH. Why there: `docs/design/auth-desk.md`.
- `fleet auth` (`lib/fleet_auth.py`): lanes hand auth failures over with `ask` and keep working. Only AUTH (or the human's own shell) runs `begin`, `number` and `end`. One sign-in at a time, under a lock; a stale slot is broken after `FLEET_AUTH_SLOT_SECS` (900). `begin` posts a one-line heads-up to DECIDE before it logs `started`, and nothing starts if that can't be written. Sign-in order, the same for every tool: a cached token first, then the human's own signed-in browser over its debug port (device-code confirm or portal, `--flow session`, no MFA), and only then a fresh-profile injector sign-in (`--flow password`) that waits on the push or number page, with the stored TOTP code as the fallback. A fresh or empty profile never signs in as the human outside that last step.
- `fleet auth number NN` prints the number to match as a big block banner in AUTH's pane, logs it (time, sign-in, who asked) and copies it to DECIDE. A push during an announced sign-in is expected; `fleet auth unexpected-push` logs one nobody announced, with the log rows around it, and routes it to DECIDE.
- An append-only auth log (`$FLEET_STATE/auth/auth-log.jsonl`) with `log`, `trace TIME` (who asked for the sign-in behind a prompt) and `status`. Secret-looking text is refused everywhere. A sign-in passes only on the human's confirmation that the prompts were the announced ones, never on a log flag. `freeze` / `thaw` (human, ROUTE, DECIDE). `helpers` lists the human's own sign-in scripts by reference; the kit ships none.
- Role files for AUTH on every CLI (no Edit or Write, no git commit or push, no lifecycle verbs, no `/goal`), and hand-off lines in the worker, ROUTE, DECIDE, WATCH and LEARN roles. The fleet-rules block has a "Sign-ins and auth" section.
- `fleet park` refuses every standing desk (route, decide, watch, learn, auth); `fleet tick` names a missing AUTH in `WARN desks down:`.
- Tests: `test/test_auth.py` (33 tests), plus five-desk layout, four-desk upgrade, park refusal and role checks in `test/test_roles.sh`, `test/test_desks_default.sh` and `test/test_installs.sh`.

## v0.9.1 (2026-11-27)

`tafk update` recognizes bin links that reach the kit through a symlinked directory.

- `fleet update` compared each link's target to the kit's physical path as a plain string, so a link made through a symlinked dir (on macOS, `/tmp/...` for a kit under `/private/tmp`) or a relative link counted as "not this kit": nothing was relinked and `tafk` was not added. Link targets now have their directory resolved (`pwd -P`) before the compare. Links the installers make were already physical and are unaffected. Found by the v0.9.0 end-to-end run.
- Tests: `test/test_update.sh` links its fixture through an alias dir, one link relative (64 assertions; 6 FAIL on the v0.9.0 updater).

## v0.9.0 (2026-11-27)

`tafk` is a second name for `fleet`, and `tafk update` (or `fleet update`) moves the kit to the latest release.

- `bin/tafk` is a symlink to `fleet`, so `tafk <anything>` runs `fleet <anything>` (same dispatch, same help). Every installer (and so `fleet bootstrap`) links `tafk` into `<prefix>/bin` next to `fleet` and `fleet-goal`.
- `fleet update [--check] [--dry-run]` (`bin/fleet-update`): a git checkout fast-forwards to the highest `vX.Y.Z` tag on `origin` (tag-only fetch, `git merge --ff-only`; a detached HEAD moves to the tag); a copied tree with a `VERSION` file downloads the release tarball and swaps it in, keeping the old tree as `<dir>.prev-vOLD`. Then the bin links that point into the kit are refreshed and `tafk` is added beside `fleet`. Prints `updated: vOLD -> vNEW`, or `already at vX.Y.Z` (exit 0, nothing changed). A dirty tree, a diverged branch or no network: a clear message, exit 1, nothing changed. Prerelease tags don't count, versions compare numerically, and it never downgrades. `--check` reports only; `--dry-run` prints every step and writes nothing. It never runs an installer or touches tmux.
- `fleet version` prints the kit's version (`git describe`, else the new `VERSION` file).
- Role deny lists: every deny on `fleet launch`, `fleet park`, `fleet stop` and `fleet reopen` now has a `tafk` twin in the claude, kiro, opencode and agy role files, and the role texts say the twins are off limits too.
- Tests: `test/test_update.sh` (63 assertions: current, newer, dirty, untracked, diverged, offline, detached, prerelease, numeric order, no downgrade, copied tree with a bad tarball; local bare origin and `file://` fixtures, no network), tafk twin checks with controls in `test/test_roles.sh`, tafk link checks in `test/test_installs.sh` and `test/test_bootstrap.sh`.

LEARN keeps two card stacks: auto (generated) and manual (asked for by the human), and the manual stack comes first.

- Every ledger row and card now has `stack`: `auto` or `manual`. `fleet learn add ... --manual` makes a manual card (never filtered as plumbing: the human asked for it); `fleet learn stack L<a>-L<b> --manual|--auto` moves rows already in the ledger (cards follow in place, cid and schedule kept), and a bare `fleet learn stack` lists the manual stack. LEARN or the human only.
- Manual cards serve ahead of every auto card in `fleet learn lessons`, a bare `fleet learn lesson`, `due` and `quiz`: the newest manual request (a topic's latest `stackAt`) first, its rows in the order they were made, so a new request goes on top like an unshift. A topic with rows on both stacks is taught as two lessons, the manual batch on its own. `snapshot` adds `byStack` counts.
- `fleet learn session` no longer needs `--topic`: a bare session takes the first lesson waiting (the manual stack first), else the topic of the first due card.
- `fleet learn rebuild` keeps the human's manual cards (Q78): `learn/manual.jsonl` records each request; a rebuild re-marks manual rows the sources bring back and re-adds the hand-made ones, and a dropped manual row stays dropped.
- Manual cards guide auto cards: auto rows that share a topic or work subject with a manual row come before other auto rows, `fleet learn examples` lists the manual cards as few-shot examples (prompt and quote) for LEARN, and an optional `learn.json` `autoPrompt` command (off by default, bounded by `timeoutSec`, run outside the ledger lock) writes questions for new untitled auto rows from those examples; anything that is not a clean question leaves the template prompt.
- Tests: `test/test_learn_stacks.sh` (65 assertions with controls).

## v0.8.1 (2026-11-26)

`fleet show` draws real images by default: passthrough is on for the whole fleet server unless you opt out.

- `FLEET_DESK_PASSTHROUGH` now defaults to `1`. `fleet launch` (every lane, every desk) and `fleet desks up` (live desks too) run `tmux set-option -g allow-passthrough on` on the fleet's tmux server, so every pane there, not only the DECIDE, WATCH and LEARN desks, can show a real image with `fleet show`. v0.8.0 set it per desk pane, and only with `FLEET_DESK_PASSTHROUGH=1`. It is a server-wide option: with no `FLEET_TMUX_SOCKET` the fleet shares the default tmux server, so other sessions on it get it too.
- `FLEET_DESK_PASSTHROUGH=0` (any value other than `1`) opts out. The kit then makes no passthrough call at all, so `fleet desks up` output and every tmux option are byte for byte what v0.8.0 gave with the variable unset.
- The kit only ever turns passthrough on. It never writes `off`, never unsets it and never uses `all`, so a launch or `fleet desks up`, opted out or not, can't flip a global that is already on back off. `test/test_show.sh` checks this statically (with controls) and on throwaway servers.
- A tmux older than 3.3 has no `allow-passthrough`: the call prints a WARN and the launch goes on.
- The `fleet show` hint for a pane with passthrough off now says the kit turns it on unless `FLEET_DESK_PASSTHROUGH=0`. The README covers mosh: an attached mosh client (Blink, for example) still gets block art in auto mode, because mosh carries only text.

## v0.8.0 (2026-11-26)

Pictures in panes, a call on another machine followed live, and a status label you can change.

- `fleet show FILE` (`bin/fleet-show`) draws an image in a pane: Kitty Unicode placeholders when the client and tmux can pass graphics through, block art otherwise, plain text as the last fallback (`FLEET_GFX=auto|kitty|symbols|text`, `FLEET_CHAFA_BIN`, `FLEET_SHOW_COLORS`). chafa runs behind a tmux shim that drops its `set-option` calls, so a draw never changes server options. A new kit setting, `FLEET_DESK_PASSTHROUGH=1` (off by default), sets pane-level `allow-passthrough on` for the DECIDE, WATCH and LEARN panes only when the desks start; with it off nothing changes. Fixes #41.
- `fleet feed start URL --token-file F` follows a call transcript served on another machine (whosaid `live serve`, or any `?from=N` lines endpoint with `--protocol lines`) into `$FLEET_STATE/feeds/<name>/transcript.jsonl`, which `fleet listen --source file` and the WATCH `call` source follow. It runs detached under the side-run supervisor, keeps its position in `state.json` so a restart resumes with no duplicate and no gap, reads the token only from a mode-600 file and never writes it anywhere, and reports `ok`, `stalled (N failed polls)`, `auth refused`, `gone` or `stopped` once per change as a pane note. A wrong token stops the feed. `fleet feed ls|status|stop` manage it, and `--bound` (default 12h) ends it. Proven against the real whosaid v1.52.0 serve. Fixes #37.
- `fleet status-label FORMAT|--reset` sets the fleet session's tmux status-left (session-scoped, live and at every `fleet launch`, saved in `$FLEET_STATE/status-label`, or `FLEET_STATUS_LABEL`), and sizes status-left-length to the rendered width. With nothing set the status line is byte-identical to before. Fixes #44.

## v0.7.1 (2026-11-26)

LEARN's source drain stops at a wall-clock bound and spaces its requests like WATCH, and work lanes named for a desk are taught again.

- LEARN no longer skips a work lane whose name ends in "desk". The plumbing-lane rule split a lane name into tokens and skipped it when any token was `desk` or `desks`, so every `goal:` row from a lane like `questions-desk` or `buy-desk` was dropped as `plumbing lane`, even rows that named a ticket, and had to be re-added with `--teach`. `desk`/`desks` now mark a plumbing lane only as the lead token (`desk-ui`, `desks-layout`); kit and tooling tokens (`kit`, `fleet`, `fleetkit`, `tmux`, `mesh`, `mcp`, ...) still count anywhere in the name, and the fleet's own desks are matched by whole name. Tests: `test_learn.sh` #43 (two work lanes kept, five kit or desk-setup lanes still skipped). Fixes #43.
- LEARN's source drain is bounded by wall clock and rate, not only by rounds. Each source's drain in `fleet learn sync` and `fleet learn rebuild` stops at 300 s (`DRAIN_SECONDS`) as well as at 50 polls, and every poll goes through WATCH's rate gate (`lib/fleet_watch_rate.py`: a token bucket at the source's `ratePerMin`, short waits slept, longer ones deferred), so a drain spaces its requests the way WATCH does. A deferral that fits in the 300 s is waited out and the same cursor polled again. A rebuild that stops early at any bound fails closed: rc 1, `FAIL source <name> failed: <bound> after N polls (M event(s) read, partial); nothing changed`. A sync keeps what it read and prints `sources: PARTIAL <name>: <why>; the next sync reads on`. Tests: `test_learn_sources.TestDrainBounds` (time bound, WATCH-equal spacing, deferral inside and past the bound, 310 of 310 with `--engine local`).

## v0.7.0 (2026-11-26)

One page for every desk: the `fleet ui serve` hub.

- `fleet ui serve` (`lib/fleet_ui.py`): one loopback port (default 7343) and one token for every desk. The DECISIONS, LEARN and WATCH pages are tabs at `/decide/`, `/learn/` and `/watch/`, each mounted from the page's own request handler (no copy of their code or HTML), with a tab strip on every page; `/route/` is a read-only ROUTE board of the roster and the last 30 answers in `decisions-log.jsonl`, lane text escaped. The DECIDE tab keeps its live updates and `notifyCommand`, which now deep-link into the hub. Security is each page's, once: loopback bind, Host and Origin checks, the token as `?t=` for GET and the header only for POST, the URL in `$FLEET_STATE/ui.url` (0600, removed on exit). The guard is LEARN's human-only check, so any `FLEET_ROLE` is refused (exit 4), `decide` and `watch` included, since the one token opens every tab. `fleet ui url [--tab T]` prints the live URL. Q700.
- `fleet decisions serve`, `fleet learn serve` and `fleet watch serve` are aliases of the hub: they print the hub URL for their tab when it answers, else start it. The standalone pages stay as modules (`python3 lib/fleet_learn.py serve` and so on) for their own tests. The pages prefix every request with `window.FLEET_BASE`, empty when served alone.
- Fleet Desk points at the hub: `fleet app urls|serve|tunnel` read and start the one hub, the Desks menu gains Route (Cmd-5), and `fleet app tunnel` prints a single `-L`. The app reads its menu from `fleet app urls`, so no rebuild is needed. Tests: `test/test_ui.py` (token required, every lane role refused, each tab 200, one keep-alive connection across tabs, POSTs through a mount, SSE through the hub, the board escapes lane text, the aliases), `test/test_app.py` updated for one port.

## v0.6.1 (2026-11-26)

Side runs that stay running until their child exits.

- `fleet side run` rows no longer end early. The side watcher's "result file unchanged for 2 polls" rule also fired on `fleet side run` rows, so a host that writes its result record early (ach under claude, codex, gemini, agy) was marked `done` about 15 s in, with `exit` null, while the child was still working; the parent got its message with no tokens. That rule now skips `source=run` rows, whose supervisor already waits on the real child: the row stays `running` until the child exits, then carries its exit code, real `endedAt` and the tokens the host reported. Test: `test_side_run.test_1c_stable_result_file_does_not_end_a_live_child`. Fixes #42.

## v0.6.0 (2026-11-26)

One command for a new computer, LEARN that teaches before it tests, and the Fleet Desk Mac app.

- `fleet bootstrap` and a curlable `bootstrap.sh` (`curl -fsSL .../bootstrap.sh | bash`). The no-sudo half installs whichever of tmux, mosh and jq are missing with brew, runs `install/<cli>.sh --no-desks` for each agent CLI it finds, puts Homebrew's bin dir on the non-interactive ssh PATH with one marker-guarded block in `~/.zshenv` (printed instead when the file is a symlink or looks dotfiles-managed), optionally authorizes a phone key, and runs `fleet desks up` once. The sudo half never runs: bootstrap writes `~/.local/state/fleet/bootstrap-root.sh` (host keys, the key-only sshd drop-in checked with `sshd -t`/`sshd -T` and rolled back on failure before sshd is switched on, Remote Login, and the mosh-server firewall allow for UDP 60000-61000) and prints the `sudo bash` line for you. `fleet bootstrap --verify` checks the effect without privileges: sshd listening, the drop-in, a bare `ssh localhost tmux -V` with your key, mosh-server on the ssh PATH, and the firewall allow. Idempotent, `--dry-run` writes nothing, and the piped form is one function called on its last line, so a cut-off download runs nothing. README "New computer" has the Blink host fields. Fixes #40.
- New `fleet remote` subcommands behind bootstrap, none of which run sudo: `root-script FILE|-`, `verify`, `ssh-path [--check|--brew-bin]`, `add-key KEY|FILE` and `attach-line`. `setup` and `status` print the same lines as in v0.5.1, and the attach line stays the plain `tmux attach -t fleet` with `window-size latest` (refs #39).
- LEARN teaches before it tests. New verbs `fleet learn lesson|lessons|session|digest|brief|curate|entity|rebuild|sources`. A card comes due only after its row's lesson was seen, and the 24 h clock starts at seen. Every lesson line is a verbatim quote plus its source; lines no source backs are dropped and counted. `fleet learn rebuild` regenerates every derived file from the sources and keeps only `reviews.jsonl` and `seen.jsonl`. Typed entity links (part-of, ships-in, blocked-by, decided-by, owner) are rebuilt from the adapters. `fleet learn serve` gains a Lesson tab, and lesson, quiz, review and seen are human-only. Role text (`roles/learn.md`, ROUTE's done lines through goal files), README and contracts follow; the design is in DESIGN-NOTES.md and `openspec/specs` (10 capabilities).
- WATCH and LEARN sources: whosaid (call decisions, always `--engine local`) and Linear (GraphQL, read-only), set in one shared `sources.json` that holds references to credentials, never the credentials (`docs/learn-sources.example.json`). A poll drains each source until it has nothing more, up to 50 polls, and fails closed at that bound, so a 60-meeting rebuild returns all 60. WATCH reads `sources.json` when it has no `watch.json`. LEARN's polled events go through WATCH's normalize, secret and drop filters and its cap.
- The `fleet learn serve` page is a superset of the pane: every `fleet learn` verb has a page route (a lesson for any waiting topic, a one-topic session with the pane's result line, and `POST /api/cli` for the rest), plus clickable per-topic bars for waiting lessons. A parity test checks every pane verb against the page.
- Fleet Desk, a Mac app for the desk pages (`app/desk-ui`, Tauri v2): its own window, a Dock icon and native notifications for new DECIDE calls and lessons waiting, deduped by call id and lesson id in `$FLEET_STATE/app-notified.json`. It forks none of the pages: `fleet app urls|serve|notify|tunnel|build|open` (`lib/fleet_app.py`) are the pane side of every app action, and the window shows the live 127.0.0.1 page. `fleet app tunnel` prints the one `ssh -N -L` command (same ports, 127.0.0.1 on both ends) that brings the pages to a phone. `fleet app build` makes an unsigned local `.app` (Bun for the Tauri CLI, cargo for Rust), its cargo target dir outside the repo. Tests: `test/test_app.py`. The README notes to relaunch the app once after allowing notifications.

## v0.5.1 (2026-11-25)

Typed wakes that arrive whole in a vim-mode Claude pane, and a phone attach that finds tmux and mosh-server.

- A typed wake into a Claude pane is one bracketed paste (`tmux set-buffer` + `paste-buffer -p`), then Enter, instead of `send-keys -l`. In Claude Code's vim NORMAL mode typed keys ran as vim commands and ate `[fleet] mes`, leaving `sage from ROUTE: ...` stuck in the box with no Enter, which also blocked `fleet park` and serve reconnects. v0.5.0's NORMAL-mode guard never fired because it read `editorMode` only from `~/.claude.json`; Claude Code 2.1.33x keeps it in `~/.claude/settings.json`. The kit now reads both (`$FLEET_CLAUDE_SETTINGS` overrides). If the box then holds only a piece of the pointer, the kit clears that piece (`C-a C-k`, or `0 D` in NORMAL mode) and retries, up to `FLEET_WAKE_TYPE_TRIES` (3); after the last try the box is left empty, Enter is not sent and the message waits in the inbox. A box holding anything else is never cleared. `fleet park` pastes `/compact` the same way (`fleet_mesh.py type <pane> <text> [cli]`). Live loop on Claude 2.1.332 in NORMAL mode, N=20: v0.5.0 left 20 fragments, v0.5.1 submitted all 20. Fixes #38.
- `fleet remote setup` checks `tmux` and `mosh-server` on the non-interactive ssh PATH (what a phone's `ssh` or `mosh` gets: the system PATH plus `~/.zshenv` for zsh), and when one is missing prints the `export PATH="<brew bin>:$PATH"` line to add to `~/.zshenv`. It never edits shell files. It prints a bare `ssh <user>@<ip> 'tmux -V'` check, and the phone attach line (full paths to `mosh-server` and `tmux`) as its last line. `fleet remote status` reports both tools on the ssh PATH. Fixes #39.

## v0.5.0 (2026-11-25)

Phone and tablet attach (`fleet remote`), the four desks by default, and fleet-mesh wakes that never type into the decision desk or run stale code after an upgrade.

- fleet-mesh never types into the decision desk pane. Panes whose roster role is in `FLEET_NO_TYPE_ROLES` (default `decide`) get native `SendMessage` from Claude senders, with no typed fallback after `FLEET_NATIVE_GRACE`. Every other wake path leaves a tmux notice and `@fleet_unread`; the inbox line stays the record. Fixes #34.
- `fleet remote setup|status|qr|off`: attach to the fleet from a phone or tablet over ssh + mosh on ZeroTier, Tailscale or another private overlay. `setup` is idempotent and takes `--pubkey`, `--ip` and `--dry-run`. It writes a key-only sshd drop-in, validated with `sshd -t` and `sshd -T` and rolled back on failure, before sshd is switched on. It also allows the resolved `mosh-server` path in the macOS firewall (with a rerun-after-upgrade warning) and sets `window-size latest`. `qr` prints the Blink/Termius attach command. The docs warn never to expose the tmux socket over raw TCP. Fixes #22.
- fleet-mesh servers notice a kit upgrade. Each `serve` process records the hash of the code it loaded (`$FLEET_STATE/mesh/<pid>.json`). Before a request, a stale server self-checks the new file and re-execs in place, keeping its pid, its stdio pipes, the MCP session and the request in flight. When it can't, it refuses typed wakes: inbox plus a tmux notice, never keys. `fleet tick` lists stale servers with their lane (`fleet_mesh.py stale`), including servers started before this change. Fixes #31.
- `fleet listen --source teams-transcript [PORT|CMD]` (or `source.kind` `teams-transcript`) follows a Teams call's live transcript panel through `whosaid teams transcript-live --jsonl` via the command adapter. Its status shows once per change. A closed panel adds one note naming `whosaid teams open-transcript`, and a gap adds a note. A numeric argument is the DevTools port. Lines without a speaker show as `unknown` with a one-time note; they stay visible under a speaker filter and are routed only with no filter. Fixes #21.
- Standing desks (roles route, decide, watch, learn and `desk` mode) launch Claude with `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF=1`, shown in `--dry-run`. The launch name check also reads the `name` field of `~/.claude/sessions/<pid>.json` for a live pid and FAILs `already live (pid N, ...)`, and `fleet cleanup` (4) lists such a holder of a pane-less lane as an orphan process. Fixes #5.
- `fleet launch --env KEY=VALUE` (repeatable) exports per-lane vars in the window script, stored in `launch/<name>.launch.json` and reapplied on resume and `fleet reopen`; `FLEET_PRELAUNCH_HOOK` prints more `KEY=VALUE` lines (nonzero exit refuses the launch); `FLEET_STRIP_EXTRA` appends vars to the `unset` line. All show in `--dry-run`; KEY is validated, VALUE shell-quoted, and the Claude env proof checks each key reached the CLI. Fixes #4.
- Goal files: the open-calls list is now `openCalls` (calls waiting on DECIDE, each with its Q id). `fleet-goal new` writes it, `condition` names it, and `check`, `desk.sh` and the DECISIONS UI still read a file's legacy `openForHuman` (desk.sh keeps writing that key on such a file). `fleet-goal check` also accepts `goal` as an object with `title` or `objective` and phases with only an `id`, and its FAIL line names the missing field. Fixes #3.
- deskA and deskB are the default. Every installer ends by running `fleet desks up --cli <its CLI>` (idempotent; `--no-desks` or `FLEET_NO_DESKS=1` skips it, `--dry-run` prints it). Fixes #9.
- `fleet launch` of a worker or probe lane (goal, prompt and resume, so `fleet reopen` too) first runs `fleet desks up` when the roster has no live ROUTE or DECIDE, and FAILs with `FAIL no desks: ...` (no lane launched) if that does not leave them live. `--no-desks` (or `FLEET_NO_DESKS=1`) skips the guard; `--dry-run` prints the step. Desk launches are exempt. Fixes #9.
- `fleet tick` prints `WARN desks down: deskA (ROUTE dead, DECIDE missing); run fleet desks up` when a desk of deskA or deskB has a dead or missing pane, also on a fleet with no lanes; `--json` is unchanged. Fixes #9.
- `fleet desks up --handoff DIR` hands each desk it launches its predecessor's handoff (`DIR/<DESK>.md`, else `ORCHESTRATOR.md` for ROUTE, `DECISIONS.md` for DECIDE, `LISTENER.md` for WATCH), and refuses while a pre-kit ORCHESTRATOR, DECISIONS or LISTENER is still live, so two decision desks never answer at once. Fixes #9.

- Resume proof for Claude lanes (`fleet reopen`, `fleet launch ... resume`): the launcher watches the pane for the workspace-trust and Bypass Permissions dialogs, like a fresh launch, and prints OK only after Claude writes `sessions/<pid>.json` for the lane's pid. A blocking dialog FAILs naming it; no registration within `FLEET_PROOF_TIMEOUT` FAILs naming the file. `FLEET_RESUME_SETTLE` is gone. Fixes #8.
- LEARN teaches meeting material, not fleet plumbing. Sync and `fleet learn add` skip lane lifecycle, desk and kit setup, routing, launch or park, tooling installs, route-done lines of kit and desk work, and goal steps that are process evidence; `add` prints `SKIP <source> (<reason>)` and adds nothing. A question-shaped `--title` marks a curated card and is never skipped. `--teach`, `learn: teach` in a line or `"learn": "teach"` in a goal file keeps one, `learn: skip` drops one. Fixes #24.
- Only answer-bearing cards: bare option letters (`a`, `13-a`, `recs on all = a`) and one-word step names are skipped, and a bare answer folds into the row that holds its full option text (linked, not a second card). A repeat of an earlier answer folds too. Fixes #24.
- Cards ask a plain question. A `--title` is the prompt, verbatim; with none, the prompt asks about the row's work subject (`What was decided about PROJ-7?`), never "What did <lane> say about". The source and lane show only after grading, in the quiz and the page. Existing cards keep their id and schedule and get the new prompt on the next sync; cards of skipped rows retire. Fixes #23.
- Multiple-choice distractors come from the same topic; a row with fewer than 3 same-topic neighbours gets no choice card. Rows are grouped by work subject (ticket, PR, release) rather than lane, and `fleet learn quiz --topic <subject>` / `fleet learn due --topic <subject>` quiz one subject. Fixes #23.

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
