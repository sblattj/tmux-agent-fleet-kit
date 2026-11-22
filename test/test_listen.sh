#!/usr/bin/env bash
# test_listen.sh: the LISTENER UI in a real tmux pane. Private tmux server (-L), temp FLEET_STATE,
# synthetic feeds with made-up names only. Exit 1 on any FAIL.
set -u
HERE=$(cd "$(dirname "$0")" && pwd -P)
FLEET=$HERE/../bin/fleet
FX=$HERE/fixtures/listen
T=$(mktemp -d "${TMPDIR:-/tmp}/listentest.XXXXXX")
export FLEET_TMUX_SOCKET=listentest-$$
export FLEET_TMUX_SESSION=listensess
export FLEET_STATE=$T/state
mkdir -p "$FLEET_STATE"
cleanup() { tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
tm() { tmux -L "$FLEET_TMUX_SOCKET" "$@"; }

FAILS=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILS=$((FAILS+1)); }
check() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
cap() { tm capture-pane -p -t "$1" 2>/dev/null; }
wait_for() { # pane text seconds : poll the pane until text shows
  local i=0
  while [ $i -lt $(( $3 * 4 )) ]; do
    cap "$1" | grep -qF -- "$2" && return 0
    sleep 0.25; i=$((i+1))
  done
  return 1
}

# ---- control: the harness reports a failing check
ctl=$(FAILS=0; check "inner" false; echo "n=$FAILS")
check "control: a failing check prints FAIL and counts it" bash -c 'case "$1" in *"FAIL inner"*n=1*) ;; *) exit 1;; esac' _ "$ctl"

cat > "$T/c.json" <<'EOF'
{"me": "Human_Example", "title": "weekly sync", "batchSec": 0,
 "speakerAliases": {"SPEAKER_00": "Bob_Example"},
 "facts": [{"match": ["p95"], "say": "p95 went from 840 ms to 310 ms on the last run", "source": "lane-a, 11:40"}]}
EOF

# a stand-in lane pane, registered in the roster the way fleet launch does
tm new-session -d -s "$FLEET_TMUX_SESSION" -n LANEA -x 220 -y 50 "sleep 600"
lanepane=$(tm list-panes -t "$FLEET_TMUX_SESSION:LANEA" -F '#{pane_id}' | head -1)
printf '{"LANEA": {"cli": "claude", "pane": "%s", "status": "active"}}\n' "$lanepane" > "$FLEET_STATE/roster.json"

# ---- (1) replay in a pane right of a lane: stream, filter row, SAY THIS box, routing
out=$("$FLEET" listen --source replay "$FX/call.jsonl" --speed 4 --config "$T/c.json" --pane-right-of LANEA 2>&1)
lp=$(printf '%s' "$out" | sed -n 's/^OK listener WATCH pane=\(%[0-9]*\)$/\1/p')
check "fleet listen --pane-right-of prints OK with a pane id" test -n "$lp"
check "listener pane shares the lane's window" test "$(tm display -p -t "$lp" '#{window_id}')" = "$(tm display -p -t "$lanepane" '#{window_id}')"
check "pane shows the SAY THIS answer" wait_for "$lp" "p95 went from 840 ms to 310 ms" 30
screen=$(cap "$lp")
for s in "LISTENER: weekly sync" "filter: (everyone)" "SAY THIS" "Alice_Example" "-> ASK  sent to DECIDE as Q1" \
         "-> ACTION  sent to ROUTE (owner: unresolved)" "routed: 1 ask"; do
  check "pane shows '$s'" bash -c 'printf "%s" "$1" | grep -qF -- "$2"' _ "$screen" "$s"
done
check "pane never shows the fake token" bash -c '! printf "%s" "$1" | grep -q FAKE0123' _ "$screen"
check "ask landed in decisions.jsonl with quote, speaker, time" \
  jq -se 'length == 1 and (.[0].question | contains("can we get the retry change into this week?") and contains("Alice_Example") and contains("15:22:58")) and .[0].from == "WATCH"' "$FLEET_STATE/decisions.jsonl"
check "actions reached the orchestrator inbox" grep -qF "I'll need the staging numbers before Friday" "$FLEET_STATE/inbox/ROUTE.jsonl"
check "pane keeps running after the replay ends" wait_for "$lp" "source: replay ended" 30
check "unconfirmed tail renders dimmed" bash -c 'tmux -L "$1" capture-pane -e -p -t "$2" | grep -F "and the dashboard" | grep -qF "$(printf "\033[2m")"' _ "$FLEET_TMUX_SOCKET" "$lp"

# ---- (1b) the issue's own form: `fleet listen --source replay fixture.jsonl`, relative path, default placement
out=$(cd "$FX" && "$FLEET" listen --source replay call.jsonl --speed 0 --config "$T/c.json" --name LISTEN2 2>&1)
lp2=$(printf '%s' "$out" | sed -n 's/^OK listener LISTEN2 pane=\(%[0-9]*\)$/\1/p')
check "default placement opens a window named LISTEN2" test "$(tm display -p -t "$lp2" '#{window_name}' 2>/dev/null)" = LISTEN2
check "relative replay path renders the stream and SAY THIS" wait_for "$lp2" "p95 went from 840 ms to 310 ms" 15
check "LISTEN2 status shows no source error" bash -c '! tmux -L "$1" capture-pane -p -t "$2" | grep -q "source: replay error"' _ "$FLEET_TMUX_SOCKET" "$lp2"
tm kill-pane -t "$lp2" 2>/dev/null

# ---- (2) live filter change with [f]: routing follows, the source is not restarted
cat > "$T/feed.sh" <<EOF
#!/bin/sh
echo start >> "$T/feed.starts"
i=1
while [ \$i -le 400 ]; do
  echo "[00:00:\$(printf %02d \$((i % 60)))] Alice_Example: the build number is \$i"
  echo "[00:00:\$(printf %02d \$((i % 60)))] Bob_Example: I'll take item \$i"
  i=\$((i+1)); sleep 0.3
done
EOF
chmod +x "$T/feed.sh"
jq '. + {"speakers": ["Alice_Example"]}' "$T/c.json" > "$T/c2.json"
tm new-window -d -t "$FLEET_TMUX_SESSION:" -n L2 "env FLEET_STATE='$FLEET_STATE' FLEET_TMUX_SOCKET='$FLEET_TMUX_SOCKET' FLEET_NAME=L2 '$FLEET' listen --source command '$T/feed.sh' --config '$T/c2.json' --name L2 --here"
p2=$(tm list-panes -t "$FLEET_TMUX_SESSION:L2" -F '#{pane_id}' | head -1)
check "second listener shows its feed" wait_for "$p2" "the build number is 3" 20
check "filter starts at Alice_Example" wait_for "$p2" "filter: Alice_Example" 5
check "Bob's lines are hidden while filtered out" bash -c '! tmux -L "$1" capture-pane -p -t "$2" | grep -q "take item"' _ "$FLEET_TMUX_SOCKET" "$p2"
check "Bob's actions are not routed while filtered out" bash -c '! grep -q "take item" "$1" 2>/dev/null' _ "$FLEET_STATE/inbox/ROUTE.jsonl"
tm send-keys -t "$p2" f
check "[f] opens the filter prompt" wait_for "$p2" "filter> Alice_Example_" 5
tm send-keys -t "$p2" C-h C-h C-h C-h C-h C-h C-h C-h C-h C-h C-h C-h C-h
tm send-keys -t "$p2" -l "Bob_Example"
tm send-keys -t "$p2" Enter
check "filter changed live to Bob_Example" wait_for "$p2" "filter: Bob_Example" 5
check "Bob's lines now show in the pane" wait_for "$p2" "take item" 5
check "Bob's actions now reach the orchestrator" bash -c 'for i in $(seq 1 40); do grep -q "take item" "$1" 2>/dev/null && exit 0; sleep 0.25; done; exit 1' _ "$FLEET_STATE/inbox/ROUTE.jsonl"
n1=$(cap "$p2" | sed -n 's/.*take item \([0-9]*\).*/\1/p' | tail -1); sleep 1.5
n2=$(cap "$p2" | sed -n 's/.*take item \([0-9]*\).*/\1/p' | tail -1)
check "the feed keeps flowing after the filter change ($n1 -> $n2)" test "${n2:-0}" -gt "${n1:-0}"
check "the source command was started exactly once" test "$(wc -l < "$T/feed.starts" | tr -d ' ')" = 1

# ---- (2b) fleet cleanup does not treat the listener window as an orphan
cl=$(env FLEET_STATE="$FLEET_STATE" "$HERE/../bin/fleet-lifecycle" cleanup 2>&1)
check "cleanup dry run does not list the listener window as an orphan" bash -c '! printf "%s" "$1" | grep -q "ORPHAN-WINDOW L2"' _ "$cl"

# ---- (3) [o] serves the explainer page on loopback, [m] marks, [q] quits
tm send-keys -t "$p2" o
check "[o] shows the page URL in the status bar" wait_for "$p2" "page: http://127.0.0.1:" 5
url=$(cap "$p2" | grep -o 'http://127\.0\.0\.1:[0-9]*/' | head -1)
check "page serves state.json" bash -c 'curl -fsS --max-time 5 "$1state.json" | jq -e ".say and .routed and .explainer" >/dev/null' _ "$url"
tm send-keys -t "$p2" m
check "[m] marks the moment" wait_for "$p2" "MARK " 5
tm send-keys -t "$p2" q
sleep 1
check "[q] quits the pane" bash -c '! tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx -- "$2"' _ "$FLEET_TMUX_SOCKET" "$p2"
check "quitting stopped the source command" bash -c 'sleep 1; ! pgrep -f "$1" >/dev/null' _ "$T/feed.sh"

# ---- (4) killing the pane (SIGHUP) still flushes the batch and stops the source
cat > "$T/feed3.sh" <<'EOF'
#!/bin/sh
echo "[00:00:01] Bob_Example: I'll rerun the soak test tonight"
sleep 600
EOF
chmod +x "$T/feed3.sh"
jq '. + {"batchSec": 120}' "$T/c.json" > "$T/c3.json"
tm new-window -d -t "$FLEET_TMUX_SESSION:" -n L3 "env FLEET_STATE='$FLEET_STATE' FLEET_TMUX_SOCKET='$FLEET_TMUX_SOCKET' FLEET_NAME=L3 '$FLEET' listen --source command '$T/feed3.sh' --config '$T/c3.json' --name L3 --here"
p3=$(tm list-panes -t "$FLEET_TMUX_SESSION:L3" -F '#{pane_id}' | head -1)
check "L3 routed the action into its batch" wait_for "$p3" "ACTION  sent to ROUTE" 15
check "the batch is still held (batchSec 120)" bash -c '! grep -q "soak test" "$1" 2>/dev/null' _ "$FLEET_STATE/inbox/ROUTE.jsonl"
tm kill-pane -t "$p3"
check "kill-pane flushed the batched action to the orchestrator" bash -c 'for i in $(seq 1 40); do grep -q "soak test" "$1" 2>/dev/null && exit 0; sleep 0.25; done; exit 1' _ "$FLEET_STATE/inbox/ROUTE.jsonl"
check "kill-pane stopped the source command" bash -c 'for i in $(seq 1 20); do pgrep -f "$1" >/dev/null || exit 0; sleep 0.25; done; exit 1' _ "$T/feed3.sh"

echo "---"
if [ $FAILS -gt 0 ]; then echo "FAIL test_listen.sh: $FAILS failure(s)"; exit 1; fi
echo "PASS test_listen.sh"
