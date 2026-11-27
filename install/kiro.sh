#!/usr/bin/env bash
# install/kiro.sh: install the fleet kit for Kiro CLI (v3 engine). Uses jq only, never calls kiro-cli.
# Everything lands under ${KIRO_HOME:-$HOME/.kiro}:
#   - steering/fleet-rules.md      the fleet rules (Kiro loads global steering *.md)
#   - settings/mcp.json            .mcpServers.fleet = python3 <repo>/lib/fleet_mesh.py serve
#   - agents/fleet-<role>.json     from roles/<role>.kiro.json, prompt = inline text of roles/<role>.md
#   - settings/cli.json            "chat.disableTrustAllConfirmation": true (lanes always run trust-all)
#   - symlinks bin/fleet, bin/fleet-goal and bin/tafk into <prefix>/bin
# Flags: --dry-run  --no-desks (skip the closing `fleet desks up --cli kiro`)  --prefix DIR (default $HOME/.local)
# Env: KIRO_HOME (default $HOME/.kiro); FLEET_ROLES_DIR overrides the roles dir.
# Placeholders come from FLEET_HUMAN / FLEET_DECIDE / FLEET_ROUTE.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

while [ $# -gt 0 ]; do
  if fc_common_flag "$@"; then shift "$FC_SHIFT"; continue; fi
  case $1 in
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done

fc_need jq
KH=${KIRO_HOME:-$HOME/.kiro}

fc_link_bins

fc_say "note: fleet lanes always run Kiro trust-all, so this sets chat.disableTrustAllConfirmation=true in $KH/settings/cli.json"
fc_jq_edit "$KH/settings/cli.json" "kiro cli.json trust-all confirmation off" '."chat.disableTrustAllConfirmation" = true'

mesh_path="$FC_REPO/lib/fleet_mesh.py"
fc_jq_edit "$KH/settings/mcp.json" "fleet MCP in kiro mcp.json" --arg m "$mesh_path" \
  '.mcpServers.fleet = {command:"python3",args:[$m,"serve"]}'

fc_install_kiro_roles "$KH/agents"

fc_upsert_rules "$KH/steering/fleet-rules.md"

fc_desks_up kiro
fc_say "done: Kiro install (config in $KH)"
