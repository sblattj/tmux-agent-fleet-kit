#!/usr/bin/env bash
# test_forget.sh: `fleet forget NAME [--force]` and the tick `forget` action for retired-role rows.
# Private tmux socket and a temp FLEET_STATE; never touches a real fleet. One PASS/FAIL line per assertion.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
command -v jq >/dev/null 2>&1 || { echo "FAIL jq is required"; exit 1; }
command -v tmux >/dev/null 2>&1 || { echo "FAIL tmux is required"; exit 1; }
T=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-forget.XXXXXX")
SOCK=fleet-test-forget-$$
export FLEET_STATE=$T/state FLEET_TMUX_SOCKET=$SOCK FLEET_TMUX_SESSION=forgettest
unset FLEET_ROLE FLEET_NAME
# fleet ls mesh=: keep every CLI config lookup inside the temp dir
export FLEET_CLAUDE_JSON=$T/claude.json CODEX_HOME=$T/codex KIRO_HOME=$T/kiro FLEET_AGY_HOME=$T/agy XDG_CONFIG_HOME=$T/xdg
trap 'tmux -L "$SOCK" kill-server 2>/dev/null; rm -rf "$T"' EXIT
fails=0
t() { local l=$1; shift; if "$@" >/dev/null 2>&1; then echo "PASS $l"; else echo "FAIL $l"; fails=$((fails + 1)); fi; }
eq() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1 (want '$3' got '$2')"; fails=$((fails + 1)); fi; }
F=$ROOT/bin/fleet

tmux -L "$SOCK" new-session -d -s forgettest -n home 'sleep 600'
live=$(tmux -L "$SOCK" list-panes -t forgettest -F '#{pane_id}' | head -1)
mkdir -p "$FLEET_STATE/inbox"
jq -n --arg live "$live" '{
  OLDDESK: {name:"OLDDESK", cli:"claude", role:"desk", status:"live", pane:"%9999", session_id:"s1", dir:"/tmp"},
  OLDORCH: {name:"OLDORCH", cli:"claude", role:"orchestrator", status:"live", pane:"%9998", session_id:"s2", dir:"/tmp"},
  LIVE1:   {name:"LIVE1", cli:"claude", role:"worker", status:"live", pane:$live, session_id:"s3", dir:"/tmp"},
  ROUTE:   {name:"ROUTE", cli:"claude", role:"route", status:"live", pane:"%9997", session_id:"s4", dir:"/tmp"},
  LIVEDESK: {name:"LIVEDESK", cli:"claude", role:"desk", status:"live", pane:$live, session_id:"s5", dir:"/tmp"}
}' > "$FLEET_STATE/roster.json"
printf '{"from":"x","text":"old mail"}\n' > "$FLEET_STATE/inbox/OLDDESK.jsonl"
printf '1\n' > "$FLEET_STATE/inbox/OLDDESK.cursor"
printf '{"from":"x","text":"keep me"}\n' > "$FLEET_STATE/inbox/ROUTE.jsonl"

# tick: retired roles show forget, the standing desk does not
tick=$("$F" tick --json 2>/dev/null)
eq "tick: a retired desk row shows action forget" "$(printf '%s' "$tick" | jq -r '.[] | select(.name=="OLDDESK") | .action' 2>/dev/null)" forget
eq "tick: a retired orchestrator row shows action forget" "$(printf '%s' "$tick" | jq -r '.[] | select(.name=="OLDORCH") | .action' 2>/dev/null)" forget
eq "tick: control: a retired desk row whose pane is alive is left alone" "$(printf '%s' "$tick" | jq -r '.[] | select(.name=="LIVEDESK") | .action' 2>/dev/null)" -
eq "tick: control: the ROUTE desk row is not marked forget" "$(printf '%s' "$tick" | jq -r '.[] | select(.name=="ROUTE") | .action' 2>/dev/null)" -

# fleet ls mesh column: a launch record passing fleet_mesh.py is mesh=yes; a lane without one is mesh=no
mkdir -p "$FLEET_STATE/launch"
printf 'exec claude --mcp-config x.json # fleet_mesh.py serve\n' > "$FLEET_STATE/launch/ROUTE.sh"
printf 'exec claude\n' > "$FLEET_STATE/launch/LIVE1.sh"
ls_out=$("$F" ls 2>&1)
t "ls: a lane launched with the fleet MCP shows mesh=yes" bash -c 'printf "%s\n" "$1" | grep -q "^ROUTE .* mesh=yes "' _ "$ls_out"
t "ls: control: a lane launched without it shows mesh=no" bash -c 'printf "%s\n" "$1" | grep -q "^LIVE1 .* alive mesh=no "' _ "$ls_out"

# role guard: a worker lane can't forget
out=$(FLEET_ROLE=worker "$F" forget OLDDESK 2>&1); rc=$?
t "a worker lane is refused" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "role worker can.t forget"' _ "$rc" "$out"
t "the refused forget left the row" jq -e 'has("OLDDESK")' "$FLEET_STATE/roster.json"

# forget a dead lane
t "control: fleet ls lists OLDDESK before the forget" bash -c '"$1" ls 2>&1 | grep -q OLDDESK' _ "$F"
out=$("$F" forget OLDDESK 2>&1); rc=$?
eq "forget of a dead lane exits 0" "$rc" 0
t "the row is gone from the roster" jq -e 'has("OLDDESK") | not' "$FLEET_STATE/roster.json"
t "control: the other rows are still there" jq -e 'has("OLDORCH") and has("LIVE1") and has("ROUTE") and has("LIVEDESK")' "$FLEET_STATE/roster.json"
d=$(find "$FLEET_STATE/archive/forgotten" -maxdepth 1 -type d -name 'OLDDESK-*' | head -1)
t "archived under archive/forgotten/OLDDESK-<ts>" test -n "$d"
t "the archived roster entry is the old row" jq -e '.name == "OLDDESK" and .role == "desk" and .session_id == "s1"' "$d/roster-entry.json"
t "the inbox moved into the archive" bash -c 'grep -q "old mail" "$1/OLDDESK.jsonl" && [ ! -e "$2/inbox/OLDDESK.jsonl" ]' _ "$d" "$FLEET_STATE"
t "the cursor moved into the archive" bash -c '[ -f "$1/OLDDESK.cursor" ] && [ ! -e "$2/inbox/OLDDESK.cursor" ]' _ "$d" "$FLEET_STATE"
t "control: another lane's inbox is untouched" grep -q "keep me" "$FLEET_STATE/inbox/ROUTE.jsonl"
t "the fleet log records it" grep -q "OLDDESK forgotten" "$FLEET_STATE/log/fleet.log"
t "fleet ls no longer lists it" bash -c '! "$1" ls 2>&1 | grep -q OLDDESK' _ "$F"

# a live lane is refused without --force
out=$("$F" forget LIVE1 2>&1); rc=$?
t "forget of a live lane is refused" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "LIVE1 is live (pane"' _ "$rc" "$out"
t "the refused live lane keeps its row" jq -e 'has("LIVE1")' "$FLEET_STATE/roster.json"
t "the refused live lane wrote no archive" bash -c '! ls -d "$1"/archive/forgotten/LIVE1-* >/dev/null 2>&1' _ "$FLEET_STATE"
out=$("$F" forget LIVE1 --force 2>&1); rc=$?
eq "--force forgets a live lane" "$rc" 0
t "--force warns and leaves the pane running" bash -c 'printf "%s" "$1" | grep -q "WARN LIVE1" && tmux -L "$2" list-panes -a -F "#{pane_id}" | grep -qx -- "$3"' _ "$out" "$SOCK" "$live"
t "--force dropped the row" jq -e 'has("LIVE1") | not' "$FLEET_STATE/roster.json"

# unknown name, bad flag
out=$("$F" forget NOPE 2>&1); rc=$?
t "an unknown name fails" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "no lane named NOPE"' _ "$rc" "$out"
out=$("$F" forget OLDORCH --bogus 2>&1); rc=$?
t "an unknown flag fails and keeps the row" bash -c '[ "$1" != 0 ] && jq -e "has(\"OLDORCH\")" "$2"' _ "$rc" "$FLEET_STATE/roster.json"

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_forget.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_forget.sh: all assertions"
