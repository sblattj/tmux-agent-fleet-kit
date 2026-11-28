#!/usr/bin/env bash
# install/codex.sh: install the fleet kit for Codex CLI.
#   - replaces or appends the fleet rules block in $CODEX_HOME/AGENTS.md (CODEX_HOME defaults to ~/.codex)
#   - registers the fleet-mesh MCP server (codex mcp add fleet ...) unless `codex mcp list` already shows it
#   - makes sure FLEET_NAME is forwarded to the server (env_vars = ["FLEET_NAME"] in $CODEX_HOME/config.toml)
#   - writes ${CODEX_HOME:-~/.codex}/fleet-<role>.config.toml from roles/<role>.codex.toml (prints
#     `no role profiles found` when none exist); FLEET_ROLES_DIR overrides the roles directory
#   - symlinks bin/fleet, bin/fleet-goal and bin/tafk into <prefix>/bin
# Flags: --dry-run  --no-desks (skip the closing `fleet desks up --cli codex`)  --prefix DIR (default $HOME/.local)  --trust DIR
#   --trust  append [projects."DIR"] trust_level = "trusted" to $CODEX_HOME/config.toml (Codex asks otherwise)
# Placeholders come from FLEET_HUMAN / FLEET_DECIDE / FLEET_ROUTE.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

while [ $# -gt 0 ]; do
  if fc_common_flag "$@"; then shift "$FC_SHIFT"; continue; fi
  case $1 in
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done

CODEX_BIN=${FLEET_CODEX_BIN:-codex}
CXH=${CODEX_HOME:-$HOME/.codex}
CFG=$CXH/config.toml
STATE=${FLEET_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/fleet}

fc_link_bins
fc_write_fleet_md
fc_upsert_rules "$CXH/AGENTS.md"  # Codex has no include: the rendered rules are merged into the block
fc_install_codex_roles

add_cmd="$CODEX_BIN mcp add fleet --env FLEET_STATE=$STATE -- python3 $FC_REPO/lib/fleet_mesh.py serve"
if command -v "$CODEX_BIN" >/dev/null 2>&1; then
  if "$CODEX_BIN" mcp list 2>/dev/null | grep -Eq '(^|[[:space:]])fleet([[:space:]]|$)'; then
    fc_say "unchanged: MCP server fleet already registered with codex"
  elif [ "$FC_DRY" = 1 ]; then
    fc_say "would run: $add_cmd"
  else
    "$CODEX_BIN" mcp add fleet --env "FLEET_STATE=$STATE" -- python3 "$FC_REPO/lib/fleet_mesh.py" serve
    fc_say "ran: $add_cmd"
  fi
elif [ "$FC_DRY" = 1 ]; then
  fc_say "would run (codex not on PATH now): $add_cmd"
else
  fc_die "codex CLI not found on PATH (or FLEET_CODEX_BIN)"
fi

# Forward FLEET_NAME to the server: add env_vars right under [mcp_servers.fleet] when no env_vars line is in that section.
if [ -f "$CFG" ] && grep -q '^\[mcp_servers\.fleet\]' "$CFG"; then
  has=$(awk '
    /^\[/ { insec = ($0 == "[mcp_servers.fleet]") }
    insec && /^[[:space:]]*env_vars[[:space:]]*=/ { found = 1 }
    END { print found ? 1 : 0 }' "$CFG")
  if [ "$has" = 1 ]; then
    if awk '/^\[/ { insec = ($0 == "[mcp_servers.fleet]") } insec && /env_vars/ && /FLEET_NAME/ { ok = 1 } END { exit ok ? 0 : 1 }' "$CFG"; then
      fc_say "unchanged: env_vars already forwards FLEET_NAME ($CFG)"
    else
      fc_warn "[mcp_servers.fleet] has an env_vars line without FLEET_NAME in $CFG; add \"FLEET_NAME\" to it by hand"
    fi
  else
    fc_mktemp; out=$FC_TMP
    awk '{ print } $0 == "[mcp_servers.fleet]" { print "env_vars = [\"FLEET_NAME\"]" }' "$CFG" > "$out"
    fc_commit "$out" "$CFG" "forward FLEET_NAME to the fleet MCP server"
  fi
elif [ "$FC_DRY" = 1 ]; then
  fc_say "would ensure env_vars = [\"FLEET_NAME\"] under [mcp_servers.fleet] in $CFG (after the server is registered)"
else
  fc_warn "no [mcp_servers.fleet] section in $CFG after registration; add: env_vars = [\"FLEET_NAME\"]"
fi

if [ -n "$FC_TRUST" ]; then
  dir=$(fc_trust_dir)
  header="[projects.\"$dir\"]"
  if [ -f "$CFG" ] && grep -Fxq "$header" "$CFG"; then
    fc_say "unchanged: $dir already has a [projects] entry in $CFG"
  else
    fc_mktemp; out=$FC_TMP
    { if [ -f "$CFG" ]; then cat "$CFG"; if [ -s "$CFG" ]; then printf '\n'; fi; fi
      printf '%s\ntrust_level = "trusted"\n' "$header"; } > "$out"
    fc_commit "$out" "$CFG" "trust $dir"
  fi
fi

fc_desks_up codex
fc_say "done: Codex install (rules in $CXH/AGENTS.md)"
