#!/usr/bin/env bash
# install/claude.sh: install the fleet kit for Claude Code.
#   - copies skills/* to ~/.claude/skills/
#   - replaces or appends the fleet rules block in ~/.claude/CLAUDE.md (between the fleet-rules markers)
#   - installs no role files: roles are passed per launch (--settings and --append-system-prompt-file
#     point at roles/<role>.claude.json and roles/<role>.md in this repo)
#   - symlinks bin/fleet and bin/fleet-goal into <prefix>/bin
# Flags: --dry-run  --no-desks (skip the closing `fleet desks up --cli claude`)  --prefix DIR (default $HOME/.local)  --mesh  --trust DIR  --trust-only
#   --mesh   also register the fleet-mesh MCP server (needed for mixed fleets: Claude lanes reaching Codex/OpenCode)
#   --trust  mark DIR as trusted in ~/.claude.json (/goal needs a trusted workspace)
#   --trust-only  with --trust: only mark the dir trusted, install nothing else
# Placeholders come from FLEET_HUMAN / FLEET_DECIDE / FLEET_ROUTE.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

MESH=0 TRUST_ONLY=0
while [ $# -gt 0 ]; do
  if fc_common_flag "$@"; then shift "$FC_SHIFT"; continue; fi
  case $1 in
    --mesh) MESH=1; shift ;;
    --trust-only) TRUST_ONLY=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done

fc_need jq
CLAUDE_BIN=${FLEET_CLAUDE_BIN:-claude}

if [ "$TRUST_ONLY" = 1 ]; then
  [ -n "$FC_TRUST" ] || fc_die "--trust-only needs --trust DIR"
  dir=$(fc_trust_dir)
  fc_jq_edit "$HOME/.claude.json" "trust $dir" --arg d "$dir" '.projects[$d].hasTrustDialogAccepted = true'
  fc_say "done: trusted $dir"; exit 0
fi

fc_link_bins
fc_copy_skills "$HOME/.claude/skills"
fc_upsert_rules "$HOME/.claude/CLAUDE.md"

if [ "$MESH" = 1 ]; then
  mesh_cmd="$CLAUDE_BIN mcp add --scope user fleet -- python3 $FC_REPO/lib/fleet_mesh.py serve"
  if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    if "$CLAUDE_BIN" mcp list 2>/dev/null | grep -Eq '^fleet[: ]'; then
      fc_say "unchanged: MCP server fleet already registered with claude"
    elif [ "$FC_DRY" = 1 ]; then
      fc_say "would run: $mesh_cmd"
    else
      "$CLAUDE_BIN" mcp add --scope user fleet -- python3 "$FC_REPO/lib/fleet_mesh.py" serve
      fc_say "ran: $mesh_cmd"
    fi
  elif [ "$FC_DRY" = 1 ]; then
    fc_say "would run (claude not on PATH now): $mesh_cmd"
  else
    fc_die "--mesh needs the claude CLI on PATH (or FLEET_CLAUDE_BIN)"
  fi
fi

if [ -n "$FC_TRUST" ]; then
  dir=$(fc_trust_dir)
  # UNVERIFIED: the key name `hasTrustDialogAccepted` under .projects["<dir>"] is not proven on this
  # Claude Code version. If /goal still reports an untrusted workspace, run claude once in the dir and accept.
  fc_jq_edit "$HOME/.claude.json" "trust $dir" --arg d "$dir" '.projects[$d].hasTrustDialogAccepted = true'
fi

fc_desks_up claude
fc_say "done: Claude Code install (rules in ~/.claude/CLAUDE.md, skills in ~/.claude/skills)"
