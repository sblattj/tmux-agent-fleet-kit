#!/usr/bin/env bash
# install/agy.sh: install the fleet kit for the Antigravity CLI (agy). Needs jq; calls `agy mcp add` when agy is on PATH.
# Everything lands under ${FLEET_AGY_HOME:-$HOME/.gemini}/config:
#   - rules/fleet-rules.md       the fleet rules (agy loads global rules/*.md with trigger: always_on)
#   - mcp_config.json            .mcpServers.fleet = python3 <repo>/lib/fleet_mesh.py serve (via agy mcp add, else jq)
#   - agents/fleet-<role>.md     roles/<role>.agy.md frontmatter + roles/<role>.md as the system prompt
#   - skills/<name>/             a copy of each skills/<name>/ (SKILL.md with name/description frontmatter)
#   - with --trust DIR: DIR added to antigravity-cli/settings.json trustedWorkspaces (lanes also trust per launch)
#   - symlinks bin/fleet and bin/fleet-goal into <prefix>/bin
# Permissions: nothing global. fleet launch runs each lane with --dangerously-skip-permissions and a per-lane
# --settings (toolPermission always-proceed, the lane dir trusted, roles/<role>.agy.json deny rules).
# Flags: --dry-run  --prefix DIR (default $HOME/.local)  --trust DIR
# Env: FLEET_AGY_HOME (default $HOME/.gemini); FLEET_AGY_BIN (agy); FLEET_ROLES_DIR overrides the roles dir.
# Placeholders come from FLEET_HUMAN / FLEET_DECIDE / FLEET_ROUTE.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

while [ $# -gt 0 ]; do
  if fc_common_flag "$@"; then shift "$FC_SHIFT"; continue; fi
  case $1 in
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done

fc_need jq
AH=${FLEET_AGY_HOME:-$HOME/.gemini}
CFG=$AH/config
AGY=${FLEET_AGY_BIN:-agy}

fc_link_bins

# rules: a file of our own (never upsert into config/AGENTS.md or GEMINI.md, which other tools may generate)
fc_render_rules
rules=$FC_TMP
fc_mktemp
{ printf -- '---\ntrigger: always_on\n---\n'; cat "$rules"; } > "$FC_TMP"
rm -f "$rules"
fc_commit "$FC_TMP" "$CFG/rules/fleet-rules.md" "fleet rules (agy global rule)"

mesh_path="$FC_REPO/lib/fleet_mesh.py"
mcp_ok() { jq -e --arg m "$mesh_path" '.mcpServers.fleet.command == "python3" and .mcpServers.fleet.args == [$m, "serve"]' "$CFG/mcp_config.json" >/dev/null 2>&1; }
if mcp_ok; then
  fc_say "unchanged: fleet MCP in agy mcp_config.json ($CFG/mcp_config.json)"
elif [ "$FC_DRY" = 1 ]; then
  fc_say "would add: fleet MCP to $CFG/mcp_config.json ($AGY mcp add fleet python3 $mesh_path serve)"
elif command -v "$AGY" >/dev/null 2>&1 && [ "$AH" = "$HOME/.gemini" ]; then
  "$AGY" mcp add fleet python3 "$mesh_path" serve || fc_die "$AGY mcp add fleet failed"
  mcp_ok || fc_die "$AGY mcp add ran but $CFG/mcp_config.json has no matching fleet server"
  fc_say "added: fleet MCP via $AGY mcp add ($CFG/mcp_config.json)"
else
  # no agy on PATH, or FLEET_AGY_HOME points away from $HOME/.gemini (agy itself always uses $HOME/.gemini)
  fc_jq_edit "$CFG/mcp_config.json" "fleet MCP in agy mcp_config.json" --arg m "$mesh_path" \
    '.mcpServers.fleet = {command:"python3",args:[$m,"serve"],disabled:false}'
fi

fc_install_agy_roles "$CFG/agents"

fc_copy_skills "$CFG/skills"

if [ -n "$FC_TRUST" ]; then
  td=$(fc_trust_dir)
  fc_jq_edit "$AH/antigravity-cli/settings.json" "agy trustedWorkspaces += $td" --arg d "$td" \
    '.trustedWorkspaces = ((.trustedWorkspaces // []) + [$d] | unique)'
fi

fc_say "done: agy install (config in $CFG)"
