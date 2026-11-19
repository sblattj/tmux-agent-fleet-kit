#!/usr/bin/env bash
# install/opencode.sh: install the fleet kit for OpenCode v1.
#   - clones the goal plugin (public project) at a pinned tag and lists it in opencode.json and tui.json "plugin"
#   - adds the fleet-mesh MCP server under "mcp" in opencode.json
#   - replaces or appends the fleet rules block in ~/.config/opencode/AGENTS.md
#   - merges roles/<role>.opencode.json into .agent["fleet-<role>"] of opencode.json (the
#     "{file:./roles/<role>.md}" prompt path is rewritten to the absolute repo path; FLEET_ROLES_DIR overrides)
#   - symlinks bin/fleet and bin/fleet-goal into <prefix>/bin
# Flags: --dry-run  --prefix DIR (default $HOME/.local)  --bypass
#   --bypass  set "permission": "allow" in opencode.json (lanes run unattended; off by default)
# Env: FLEET_OC_GOAL_PLUGIN_URL (default https://github.com/sblattj/opencode-goal-pro-max-complete-plugin)
#      FLEET_OC_GOAL_PLUGIN_REF (default v1.5.3)
#      FLEET_OC_CONFIG_DIR (default $HOME/.config/opencode)
# Placeholders come from FLEET_HUMAN / FLEET_DESK / FLEET_ORCHESTRATOR.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

BYPASS=0
while [ $# -gt 0 ]; do
  if fc_common_flag "$@"; then shift "$FC_SHIFT"; continue; fi
  case $1 in
    --bypass) BYPASS=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done

fc_need jq
URL=${FLEET_OC_GOAL_PLUGIN_URL:-https://github.com/sblattj/opencode-goal-pro-max-complete-plugin}
REF=${FLEET_OC_GOAL_PLUGIN_REF:-v1.5.3}
PLUGIN_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/opencode-goal-pro-max-complete-plugin
CONF=${FLEET_OC_CONFIG_DIR:-$HOME/.config/opencode}

fc_link_bins

if [ -d "$PLUGIN_DIR/.git" ]; then
  fc_say "unchanged: goal plugin already cloned at $PLUGIN_DIR ($(git -C "$PLUGIN_DIR" describe --tags --always 2>/dev/null || echo unknown ref); wanted $REF)"
elif [ -f "$PLUGIN_DIR/package.json" ]; then
  # installed by the plugin's own installer (no .git): use it as is, never clone over it
  fc_say "unchanged: goal plugin already installed at $PLUGIN_DIR (not a git clone; wanted $REF)"
elif [ -d "$PLUGIN_DIR" ] && [ -n "$(ls -A "$PLUGIN_DIR" 2>/dev/null)" ]; then
  fc_die "$PLUGIN_DIR exists, is not empty and holds no plugin; move it aside or set XDG_DATA_HOME"
elif [ "$FC_DRY" = 1 ]; then
  fc_say "would run: git clone --depth 1 --branch $REF $URL $PLUGIN_DIR"
else
  fc_need git
  mkdir -p "$(dirname "$PLUGIN_DIR")"
  git clone --depth 1 --branch "$REF" "$URL" "$PLUGIN_DIR"
  fc_say "cloned: goal plugin $REF -> $PLUGIN_DIR"
fi

plugin_ref="file://$PLUGIN_DIR"
add_plugin='.plugin = (if ((.plugin // []) | index($p)) then (.plugin // []) else ((.plugin // []) + [$p]) end)'
fc_jq_edit "$CONF/tui.json" "goal plugin in tui.json" --arg p "$plugin_ref" "$add_plugin"

mesh_path="$FC_REPO/lib/fleet_mesh.py"
filter="$add_plugin | .mcp.fleet = {type:\"local\",command:[\"python3\",\$m,\"serve\"],enabled:true}"
if [ "$BYPASS" = 1 ]; then filter="$filter | .permission = \"allow\""; fi
fc_jq_edit "$CONF/opencode.json" "goal plugin + fleet MCP in opencode.json" --arg p "$plugin_ref" --arg m "$mesh_path" "$filter"

fc_install_opencode_roles "$CONF/opencode.json"

fc_upsert_rules "$CONF/AGENTS.md"

fc_say "done: OpenCode install (config in $CONF)"
