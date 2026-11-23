# Changelog

## v0.1.1 (2026-11-22)

Docs and wording now match the four desks.

- README Quick start (Claude Code): `fleet desks up` replaces the hand-launched ORCHESTRATOR and DECISIONS sessions, and the goal examples report to ROUTE.
- Fleet rules (`rules/fleet-rules.md`): the placeholders are now `{{ROUTE}}` and `{{DECIDE}}` (the installers fill them the same way, from `FLEET_ROUTE` and `FLEET_DECIDE`); the rules start the desks with `fleet desks up` and list the standing desks as ROUTE, DECIDE, WATCH and LEARN.
- The listener pane is now the WATCH UI: pane title `WATCH: <title>`, page title, `--help` text and docs. Wire names stay: `fleet listen`, `listener.json`, the `[listener]` message prefix and `OK listener` output.
- Worker role text, the README, INTERFACES.md and all four fleet skills say ROUTE and DECIDE instead of "the orchestrator"; the decisions desk skill restarts DECIDE with `fleet desks up` (or resumes it with `--role decide --split-right-of ROUTE`), and the retire skill starts the next ROUTE with `fleet desks up`.
- `docs/design/four-desks.md`: the status line now says phase 1 shipped in v0.1.0 and the rest is a design record.

## v0.1.0 (2026-11-22)

First tagged release.

- Four desks in two split windows: `fleet desks up` builds `deskA` (ROUTE | DECIDE) and `deskB` (WATCH | LEARN). The older ORCHESTRATOR, DECISIONS and LISTENER desks are retired.
- Goal lanes for five CLIs: Claude Code, Codex CLI, OpenCode v1, Kiro CLI (v3 engine) and Antigravity CLI (`fleet launch --cli agy`), alone or mixed, each started inside a goal locked to a living goal file.
- fleet-mesh MCP server: `peer_send`, `peer_inbox`, `peer_list`, and the decisions desk calls (`decision_open` and friends), with wake-on-message for idle lanes.
- Decisions UI (`lib/fleet_decisions.py`): a local web page over the desk's own state for answering calls without typing into the desk pane.
- Listener (`lib/fleet_listen.py`): parses live call transcripts, routes asks to the desks and drops any line that carries a secret-shaped string.
- Lane lifecycle (`bin/fleet-lifecycle`): tick, park, reopen, handoff and cleanup.
- Per-CLI installers (`install/*.sh`) and role files for every desk and the worker.
- Test suites on private tmux sockets with stub CLIs, plus a privacy scan (`test/test_privacy.sh`).
