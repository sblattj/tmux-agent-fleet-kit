# Roles

One set of files per role (`orchestrator`, `desk`, `listener`, `worker`):

- `<role>.md`: role context. Claude gets it via `--append-system-prompt-file`; the Codex profile embeds it as
  `developer_instructions`.
- `<role>.claude.json`: Claude `--settings` file, `permissions.deny` only (allow rules do nothing in bypass mode).
- `<role>.codex.toml`: Codex profile body (`developer_instructions` plus `[mcp_servers.fleet] enabled_tools`).
  Generated from the `.md`; `test/test_roles.sh` checks they match.
- `<role>.opencode.json`: OpenCode agent object, merged by `install/opencode.sh` into `agent.fleet-<role>`.

Note: the OpenCode v1 agent schema (`description`, `mode`, `prompt`, `tools`, `permission.bash` globs) is UNVERIFIED
against a real OpenCode install; JSON has no comments, so the caveat lives here. `prompt` uses `{file:...}` relative
to the config, so the installer should rewrite it to an absolute path.

Every role denies AskUserQuestion at launch (`--disallowed-tools`). Only the orchestrator, or an unset `FLEET_ROLE`
(the human's own shell), may launch, park, stop or reopen lanes.
