#!/usr/bin/env bash
# install/rules.sh: the fleet rules layer (AGENTS.fleet.md, issue #55) on its own, for every CLI at once.
#   refresh    re-render ~/.config/fleet/AGENTS.fleet.md from this kit with the names saved at install
#              (rules.env), and refresh the copies that are merged rather than imported: the Codex block, the
#              Kiro steering file and the Antigravity rule, each only where it is already installed
#   uninstall  take the layer out: the Claude @ import and the Codex and OpenCode blocks (with their markers),
#              the OpenCode instructions entry, the Kiro steering file, the Antigravity rule, then the rendered
#              file and rules.env. The user's own text around a block is kept. Only the rules layer: skills,
#              role agents, MCP entries and bin links stay.
#   path       print where the rendered file lives
# Flags: --dry-run (print, change nothing)  --cli LIST (comma list of claude,codex,opencode,kiro,agy; default all)
set -eu
cmd=${1:-}; [ $# -gt 0 ] && shift
FLEET_CONFIG_DIR=${FLEET_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/fleet}
# the names the layer was installed with win over defaults; an explicit FLEET_* in the env wins over both
if [ -f "$FLEET_CONFIG_DIR/rules.env" ]; then
  _h=${FLEET_HUMAN-} _d=${FLEET_DECIDE-} _r=${FLEET_ROUTE-}
  # shellcheck disable=SC1091
  . "$FLEET_CONFIG_DIR/rules.env"
  [ -z "$_h" ] || FLEET_HUMAN=$_h; [ -z "$_d" ] || FLEET_DECIDE=$_d; [ -z "$_r" ] || FLEET_ROUTE=$_r
fi
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

CLIS=claude,codex,opencode,kiro,agy
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) FC_DRY=1; shift ;;
    --cli) [ $# -ge 2 ] || fc_die "--cli needs a list"; CLIS=$2; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) fc_die "unknown flag $1 (see --help)" ;;
  esac
done
want() { case ",$CLIS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

CLAUDE_MD=$HOME/.claude/CLAUDE.md
CODEX_MD=${CODEX_HOME:-$HOME/.codex}/AGENTS.md
OC=${FLEET_OC_CONFIG_DIR:-$HOME/.config/opencode}
KIRO_STEER=${KIRO_HOME:-$HOME/.kiro}/steering/AGENTS.fleet.md
AGY_RULE=${FLEET_AGY_HOME:-$HOME/.gemini}/config/rules/fleet-rules.md

has_block() { [ -f "$1" ] && grep -q 'fleet-rules:begin' "$1"; }

agy_render() {
  local r
  fc_render_rules; r=$FC_TMP
  fc_mktemp
  { printf -- '---\ntrigger: always_on\n---\n'; cat "$r"; } > "$FC_TMP"
  rm -f "$r"
  fc_commit "$FC_TMP" "$AGY_RULE" "fleet rules (agy global rule)"
}

case $cmd in
  path) printf '%s\n' "$FC_FLEET_MD" ;;
  refresh)
    if [ ! -f "$FC_FLEET_MD" ] && ! has_block "$CODEX_MD" && [ ! -f "$KIRO_STEER" ] && [ ! -f "$AGY_RULE" ]; then
      fc_say "no fleet rules layer installed; nothing to refresh (run an installer)"; exit 0
    fi
    fc_write_fleet_md
    if want codex && has_block "$CODEX_MD"; then fc_upsert_rules "$CODEX_MD"; fi
    if want kiro && [ -f "$KIRO_STEER" ]; then fc_render_rules; fc_commit "$FC_TMP" "$KIRO_STEER" "fleet rules (Kiro steering)"; fi
    if want agy && [ -f "$AGY_RULE" ]; then agy_render; fi
    fc_say "done: fleet rules refreshed ($FC_FLEET_MD)"
    ;;
  uninstall)
    if want claude; then fc_remove_block "$CLAUDE_MD"; fi
    if want codex; then fc_remove_block "$CODEX_MD"; fi
    if want opencode; then
      fc_remove_block "$OC/AGENTS.md"
      if [ -f "$OC/opencode.json" ] && jq -e --arg f "$FC_FLEET_MD" '(.instructions // []) | index($f) != null' "$OC/opencode.json" >/dev/null 2>&1; then
        fc_jq_edit "$OC/opencode.json" "AGENTS.fleet.md out of opencode.json instructions" --arg f "$FC_FLEET_MD" \
          '.instructions |= map(select(. != $f)) | if .instructions == [] then del(.instructions) else . end'
      fi
    fi
    if want kiro; then fc_rm "$KIRO_STEER" "fleet rules (Kiro steering)"; fi
    if want agy; then fc_rm "$AGY_RULE" "fleet rules (agy global rule)"; fi
    if [ "$CLIS" = claude,codex,opencode,kiro,agy ]; then
      fc_rm "$FC_FLEET_MD" "AGENTS.fleet.md"
      fc_rm "$FLEET_CONFIG_DIR/rules.env" "fleet rules placeholder values"
    else
      fc_say "kept: $FC_FLEET_MD (other CLIs may still load it; uninstall without --cli removes it)"
    fi
    fc_say "done: fleet rules layer removed ($CLIS)"
    ;;
  *) fc_die "usage: fleet rules refresh|uninstall|path [--dry-run] [--cli LIST]" ;;
esac
