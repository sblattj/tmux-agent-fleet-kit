# Roles

One set of files per role: the five desks `route`, `decide`, `watch`, `learn` (docs/design/four-desks.md), `auth`
(docs/design/auth-desk.md) and `worker`. `route` has the orchestrator's old deny list; `decide`, `watch`, `learn` and
`auth` have the desk's. The old names
`orchestrator`, `desk` and `listener` have no files: they were removed once the pre-rename sessions retired, and
`fleet launch` refuses them in every mode, resume included (docs/design/four-desks.md section 5.3).
`probe` is a reserved name with no files until phase 4.

- `<role>.md`: role context. Claude gets it via `--append-system-prompt-file`; the Codex profile embeds it as
  `developer_instructions`.
- `<role>.claude.json`: Claude `--settings` file, `permissions.deny` only (allow rules do nothing in bypass mode).
- `<role>.codex.toml`: Codex profile body (`developer_instructions` plus `[mcp_servers.fleet] enabled_tools`).
  Generated from the `.md`; `test/test_roles.sh` checks they match.
- `<role>.opencode.json`: OpenCode agent object, merged by `install/opencode.sh` into `agent.fleet-<role>`.
- `<role>.kiro.json`: Kiro V3 agent (`permissions.rules` deny sets), written by `install/kiro.sh` to `$KIRO_HOME/agents/fleet-<role>.json` with `name` set and `prompt` replaced by the inline text of `<role>.md` (Kiro rejects absolute `file://` prompts and silently falls back to the default agent, dropping the deny rules). Known hole: a deny on `fleet park*` splits `&&` chains but does not look inside `sh -c "..."`, so a Kiro role is guidance plus a guardrail, not a hard fence.
- `<role>.agy.json`: Antigravity CLI (`agy`) `--settings` fragment, `permissions.deny` only (`command(...)` and `write_file(*)` rules). `fleet launch --cli agy` merges it into a per-lane settings file with `toolPermission` always-proceed and the lane dir trusted (role keys win). PROVEN on agy 1.5.13: a `command(fleet stop)` deny blocked `fleet stop nothing` under `--dangerously-skip-permissions`. UNVERIFIED: whether `write_file(*)` also stops edit tools and artifacts, and whether `sh -c` wrappers are caught.
- `<role>.agy.md`: agy agent frontmatter (`name: fleet-<role>`, `description`, `mainAgent: true`, `inheritMcp: true`, `tools`; route, decide, watch and learn list no write or edit tools). `install/agy.sh` writes it plus the body of `<role>.md` to `$FLEET_AGY_HOME/config/agents/fleet-<role>.md`; lanes start with `--agent fleet-<role>`.

Note: the OpenCode v1 agent schema (`description`, `mode`, `prompt`, `tools`, `permission.bash` globs) is UNVERIFIED
against a real OpenCode install; JSON has no comments, so the caveat lives here. `prompt` uses `{file:...}` relative
to the config, so the installer should rewrite it to an absolute path.

Every role denies AskUserQuestion at launch (`--disallowed-tools`). Claude worker lanes also deny Workflow, ScheduleWakeup and ReportFindings, except tools named in the goal's `keepTools` or `--keep-tools`. Only route, or an unset `FLEET_ROLE`
(the human's own shell), may launch, park, stop or reopen lanes.

## Note for the `watch` role (WATCH desk; replaces `listener`)

A watcher that reads a chat web app (for example Teams in the browser) through CDP should expect its tab to "hop":
the app reopens itself as a new tab and closes the old one, often within 1 to 10 seconds of opening. A target id
grabbed right after `new_page` can be gone a moment later. So a watcher should:

- wait about 10 seconds after opening the app before it reads anything;
- then re-find its tab with `list_pages` (by URL or title), not by the id it opened;
- keep and reuse one tab per lane, and re-find it the same way whenever a call reports the target missing.
