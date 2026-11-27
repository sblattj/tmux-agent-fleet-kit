#!/usr/bin/env bash
# install/opencode.sh: install the fleet kit for OpenCode v1.
#   - clones the goal plugin (public project) at a pinned tag and lists it in opencode.json and tui.json "plugin"
#   - clones the compact plugin (public project) at a pinned tag and lists it in opencode.json "plugin" (server
#     plugin, not in tui.json); it compacts a session when its context reaches a token limit (default 200k)
#   - adds the fleet-mesh MCP server under "mcp" in opencode.json
#   - replaces or appends the fleet rules block in ~/.config/opencode/AGENTS.md
#   - merges roles/<role>.opencode.json into .agent["fleet-<role>"] of opencode.json (the
#     "{file:./roles/<role>.md}" prompt path is rewritten to the absolute repo path; FLEET_ROLES_DIR overrides)
#   - symlinks bin/fleet, bin/fleet-goal and bin/tafk into <prefix>/bin
# Flags: --dry-run  --no-desks (skip the closing `fleet desks up --cli opencode`)  --prefix DIR (default $HOME/.local)  --bypass
#   --bypass  set "permission": "allow" in opencode.json (lanes run unattended; off by default)
# Env: FLEET_OC_GOAL_PLUGIN_URL (default https://github.com/sblattj/opencode-goal-pro-max-complete-plugin)
#      FLEET_OC_GOAL_PLUGIN_REF (default v1.5.3)
#      FLEET_OC_COMPACT_PLUGIN_URL (default https://github.com/sblattj/opencode-compact-at-token-limit)
#      FLEET_OC_COMPACT_PLUGIN_REF (default v0.1.2)
#      FLEET_OC_COMPACT_LIMIT (150000, 150k, 1.5m; 0 = no forced compaction at all; unset keeps the limit already
#        in opencode.json, else 200000). The only limit knob: it is written as the plugin option, which beats
#        OPENCODE_COMPACT_AT_TOKENS. The goal plugin's own compactAtTokens is set to 0 (one compactor per lane).
#      FLEET_OC_CONFIG_DIR (default $HOME/.config/opencode)
# Placeholders come from FLEET_HUMAN / FLEET_DECIDE / FLEET_ROUTE.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

BYPASS=0
while [ $# -gt 0 ]; do
  if fc_common_flag "$@"; then shift "$FC_SHIFT"; continue; fi
  case $1 in
    --bypass) BYPASS=1; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done

fc_need jq
URL=${FLEET_OC_GOAL_PLUGIN_URL:-https://github.com/sblattj/opencode-goal-pro-max-complete-plugin}
REF=${FLEET_OC_GOAL_PLUGIN_REF:-v1.5.3}
PLUGIN_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/opencode-goal-pro-max-complete-plugin
C_URL=${FLEET_OC_COMPACT_PLUGIN_URL:-https://github.com/sblattj/opencode-compact-at-token-limit}
C_REF=${FLEET_OC_COMPACT_PLUGIN_REF:-v0.1.2}
C_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/opencode-compact-at-token-limit
C_LIMIT=${FLEET_OC_COMPACT_LIMIT-}
CONF=${FLEET_OC_CONFIG_DIR:-$HOME/.config/opencode}

# same grammar as the plugin's parseLimit: a whole or decimal number, optional k/m suffix
if [ -n "$C_LIMIT" ] && ! printf '%s' "$C_LIMIT" | grep -Eq '^[0-9]+(\.[0-9]+)?[kKmM]?$'; then
  fc_die "FLEET_OC_COMPACT_LIMIT=$C_LIMIT is not a token count (150000, 150k, 1.5m, 0 = off)"
fi

# oc_plugin LABEL URL REF DIR: clone a plugin at REF into DIR, or reuse what is there
oc_plugin() {
  local label=$1 url=$2 ref=$3 dir=$4
  if [ -d "$dir/.git" ]; then
    fc_say "unchanged: $label already cloned at $dir ($(git -C "$dir" describe --tags --always 2>/dev/null || echo unknown ref); wanted $ref)"
  elif [ -f "$dir/package.json" ]; then
    # installed by the plugin's own installer (no .git): use it as is, never clone over it
    fc_say "unchanged: $label already installed at $dir (not a git clone; wanted $ref)"
  elif [ -d "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    fc_die "$dir exists, is not empty and holds no plugin; move it aside or set XDG_DATA_HOME"
  elif [ "$FC_DRY" = 1 ]; then
    fc_say "would run: git clone --depth 1 --branch $ref $url $dir"
  else
    fc_need git
    mkdir -p "$(dirname "$dir")"
    git clone --depth 1 --branch "$ref" "$url" "$dir"
    fc_say "cloned: $label $ref -> $dir"
  fi
}

fc_link_bins

oc_plugin "goal plugin" "$URL" "$REF" "$PLUGIN_DIR"
oc_plugin "compact plugin" "$C_URL" "$C_REF" "$C_DIR"

plugin_ref="file://$PLUGIN_DIR"
compact_ref="file://$C_DIR"
add_plugin='.plugin = (if ((.plugin // []) | index($p)) then (.plugin // []) else ((.plugin // []) + [$p]) end)'
fc_jq_edit "$CONF/tui.json" "goal plugin in tui.json" --arg p "$plugin_ref" "$add_plugin"

# One compactor per lane: the compact plugin owns forced compaction, so the goal plugin's entry in
# opencode.json is [ref, {..., "compactAtTokens": 0}] (goal plugin v1.5.3: 0 turns its forcing off, and
# plugin options seed every goal). The compact entry always carries its limit as a plugin option
# ([ref, {"limit": N}], default 200000), and an option beats OPENCODE_COMPACT_AT_TOKENS, so
# FLEET_OC_COMPACT_LIMIT is the only knob and the tmux server's env never decides it.
# Both entries may be a plain string or a [ref, options] tuple; other options are kept, the slot is kept.
oc_plugins='
  def ref: if type == "array" then .[0] else . end;
  def opts: if type == "array" then (.[1] // {}) else {} end;
  def upsert($r; f): if any(.[]; ref == $r) then map(if ref == $r then f else . end) else . + [$r | f] end;
  .plugin = (.plugin // [])
  | .plugin |= upsert($p; [ref, (opts + {compactAtTokens: 0})])
  | .plugin |= upsert($c; [ref, (opts
      | if $l != "" then .limit = ($l | if test("^[0-9]+$") then tonumber else . end)
        elif has("limit") then . else .limit = 200000 end)])'

mesh_path="$FC_REPO/lib/fleet_mesh.py"
filter="$oc_plugins | .mcp.fleet = {type:\"local\",command:[\"python3\",\$m,\"serve\"],enabled:true}"
# the read-only probe verbs; only agent fleet-probe enables its tools (roles/probe.opencode.json "fleet-probe_*")
filter="$filter | .mcp[\"fleet-probe\"] = {type:\"local\",command:[\"python3\",\$q,\"serve\"],enabled:true}"
filter="$filter | .tools[\"fleet-probe_*\"] = false"
if [ "$BYPASS" = 1 ]; then filter="$filter | .permission = \"allow\""; fi
fc_jq_edit "$CONF/opencode.json" "goal + compact plugins + fleet MCP in opencode.json" --arg p "$plugin_ref" --arg c "$compact_ref" --arg l "$C_LIMIT" --arg m "$mesh_path" --arg q "$FC_REPO/lib/fleet_probe.py" "$filter"

fc_install_opencode_roles "$CONF/opencode.json"

fc_upsert_rules "$CONF/AGENTS.md"

fc_desks_up opencode
fc_say "done: OpenCode install (config in $CONF)"
