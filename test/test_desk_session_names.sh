#!/usr/bin/env bash
# Issue #5: standing desks launch with CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF=1, and the launch liveness check also
# reads the `name` field of Claude's sessions/<pid>.json for a live pid. Private tmux server, scratch state and a
# scratch Claude home (FLEET_CLAUDE_HOME), stub CLIs. Exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
HERE=$(cd "$(dirname "$0")" && pwd -P)
FLEET=$HERE/../bin/fleet
T=$(mktemp -d "${TMPDIR:-/tmp}/fleetsn.XXXXXX")
export FLEET_TMUX_SOCKET=fleetsn-$$
export FLEET_TMUX_SESSION=snsess
export FLEET_STATE=$T/state
export FLEET_CLAUDE_BIN=$HERE/stubs/claude
export FLEET_DIR=$T
export FLEET_NO_DESKS=1
export FLEET_CLAUDE_HOME=$T/claude-home
export FLEET_PROOF_TIMEOUT=3
mkdir -p "$FLEET_STATE" "$FLEET_CLAUDE_HOME/sessions"
HOLDER=
cleanup() { [ -z "$HOLDER" ] || kill "$HOLDER" 2>/dev/null; tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
tm() { tmux -L "$FLEET_TMUX_SOCKET" "$@"; }

FAILS=0
t() { # label cmd...
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi
}
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnt() { ! printf '%s' "$1" | grep -qF -- "$2"; }
printf '%s\n' 'Do the thing.' > "$T/prompt.txt"
V=CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF=1

# ---- (1) the export, shown in --dry-run: desk mode and the four standing roles get it, a worker does not
out=$("$FLEET" launch --dry-run --cli claude --dir "$T" D1 desk 2>&1)
t "dry-run desk mode exports $V" has "$out" "$V"
for r in route decide watch learn; do
  out=$("$FLEET" launch --dry-run --cli claude --role $r --dir "$T" "R_$r" prompt "$T/prompt.txt" 2>&1)
  t "dry-run --role $r exports $V" has "$out" "$V"
done
out=$("$FLEET" launch --dry-run --cli claude --role decide --dir "$T" R_res resume 11111111-2222-3333-4444-555555555555 2>&1)
t "dry-run --role decide resume exports $V" has "$out" "$V"
out=$("$FLEET" launch --dry-run --cli claude --dir "$T" W1 prompt "$T/prompt.txt" 2>&1)
t "control: dry-run worker does not export it" hasnt "$out" "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF"
t "control: dry-run worker still exports the skip var" has "$out" "CLAUDE_CODE_EVEN_MORE_DANGEROUSLY_SKIP_PERMISSIONS=1"

# ---- (2) a live pid's sessions/<pid>.json holding the name refuses the launch
sleep 300 & HOLDER=$!
printf '{"pid":%s,"sessionId":"x","name":"HELD","kind":"background"}\n' "$HOLDER" > "$FLEET_CLAUDE_HOME/sessions/$HOLDER.json"
before=$(tm list-panes -a -F '#{pane_id}' 2>/dev/null | wc -l | tr -d ' ')
out=$("$FLEET" launch --cli claude --dir "$T" HELD prompt "$T/prompt.txt" 2>&1); rc=$?
t "session-file name held by a live pid: exit 1" test $rc -eq 1
t "session-file name held: FAIL ... already live (pid N" bash -c 'printf "%s" "$1" | grep -q "^FAIL .*already live (pid $2"' _ "$out" "$HOLDER" \
  || printf '    launcher said: %s\n' "$out"
t "session-file name held: no pane opened" test "$(tm list-panes -a -F '#{pane_id}' 2>/dev/null | wc -l | tr -d ' ')" = "$before"
t "session-file name held: no roster row" bash -c '! jq -e ".HELD" "$1/roster.json" >/dev/null 2>&1' _ "$FLEET_STATE"

# controls: a dead pid, or another name, does not hold the name
printf '{"pid":%s,"sessionId":"y","name":"GONE"}\n' 999999 > "$FLEET_CLAUDE_HOME/sessions/999999.json"
out=$("$FLEET" launch --cli claude --dir "$T" GONE prompt "$T/prompt.txt" 2>&1)
t "control: a dead pid's session file does not refuse the name" hasnt "$out" "already live"
out=$("$FLEET" launch --cli claude --dir "$T" HELD2 prompt "$T/prompt.txt" 2>&1)
t "control: a live session file under another name does not refuse" hasnt "$out" "already live"
printf 'not json "OTHER"' > "$FLEET_CLAUDE_HOME/sessions/1.json"
out=$("$FLEET" launch --cli claude --dir "$T" OTHER prompt "$T/prompt.txt" 2>&1)
t "control: an unreadable session file is skipped" hasnt "$out" "already live"

# ---- (3) fleet cleanup reports a session-file name holder of a pane-less lane as an orphan process (category 4)
jq --argjson h "$HOLDER" '. + {GHOST: {pane: "%9999", cli: "claude", status: "parked", session_id: "22222222-2222-4222-8222-222222222222", dir: "/tmp"}}' \
  "$FLEET_STATE/roster.json" > "$T/r.json" && mv "$T/r.json" "$FLEET_STATE/roster.json"
printf '{"pid":%s,"sessionId":"z","name":"GHOST"}\n' "$HOLDER" > "$FLEET_CLAUDE_HOME/sessions/$HOLDER.json"
out=$("$FLEET" cleanup 2>&1)
t "cleanup (4) lists the live session-file holder of a pane-less lane" bash -c 'printf "%s" "$1" | grep -q "ORPHAN-PROC pid=$2 lane=GHOST (session file"' _ "$out" "$HOLDER" \
  || printf '    cleanup said: %s\n' "$out"
t "cleanup stays a dry run (holder still alive)" kill -0 "$HOLDER"
printf '{"pid":%s,"sessionId":"z","name":"NOLANE"}\n' "$HOLDER" > "$FLEET_CLAUDE_HOME/sessions/$HOLDER.json"
out=$("$FLEET" cleanup 2>&1)
t "control: a session-file name with no roster lane is not listed" hasnt "$out" "pid=$HOLDER"

[ "$FAILS" = 0 ] && echo "ALL PASS" || { echo "$FAILS FAILED"; exit 1; }
