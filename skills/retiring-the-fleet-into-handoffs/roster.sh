#!/usr/bin/env bash
# Read-only: which retiring lanes have filed a successor handoff.
# Each handoff should carry a line "Was: <old-session-name>".
# Usage: roster.sh [peers-dir] [expected-old-name ...]
# Default dir: ${FLEET_HANDOFF_DIR:-$HOME/.local/state/fleet/handoff}/peers
# bash 3.2 safe: no associative arrays.
set -eu
dir="${1:-${FLEET_HANDOFF_DIR:-$HOME/.local/state/fleet/handoff}/peers}"; [ $# -gt 0 ] && shift
[ -d "$dir" ] || { echo "no handoff dir: $dir"; exit 1; }
seen=$(mktemp)
trap 'rm -f "$seen"' EXIT
echo "successor handoffs in $dir:"
found=0
for f in "$dir"/*.md; do
  [ -e "$f" ] || { echo "  (none yet)"; break; }
  found=1
  name=$(basename "$f" .md)
  was=$(grep -m1 -E '^\**Was:?\**:? ' "$f" | sed -E 's/^\**Was:?\**:? *//; s/[`*]//g' || true)
  [ -n "$was" ] && printf '%s\n' "$was" >> "$seen"
  mt=$(date -r "$f" '+%m-%d %H:%M' 2>/dev/null || stat -c '%y' "$f" 2>/dev/null | cut -c6-16)
  printf '  %-50s was=%-30s %s\n' "$name" "${was:-?(no Was: line)}" "$mt"
done
if [ $# -gt 0 ]; then
  echo "expected retirees with no handoff (by Was: line):"
  missing=0
  for old in "$@"; do
    if ! grep -Fxq -- "$old" "$seen"; then echo "  $old"; missing=1; fi
  done
  [ "$missing" -eq 0 ] && echo "  (all filed)"
fi
exit 0
