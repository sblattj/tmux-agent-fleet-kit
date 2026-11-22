# Changelog

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
