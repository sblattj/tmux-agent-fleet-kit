#!/usr/bin/env bash
# Q105 stall park in bin/fleet-lifecycle (LANDER-NOTES-fd-p4-probe.md patch 2): a live probe lane whose pane has
# shown no output for 15 min gets action park-stalled in `fleet tick --json` and is listed by `fleet cleanup`.
# Controls that must differ: the same lane at 14 min, a worker lane at the same age, a parked probe.
# Needs tmux; uses a private tmux socket and a temp FLEET_STATE, never the real fleet. Launches no lane.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
pass=0 fail=0
ok() { if [ "$1" = "$2" ]; then echo "PASS $3"; pass=$((pass+1)); else echo "FAIL $3 (want $2, got $1)"; fail=$((fail+1)); fi; }
command -v tmux >/dev/null || { echo "SKIP no tmux"; exit 0; }
export FLEET_STATE=$(mktemp -d) FLEET_TMUX_SOCKET=fd-p4-stall-$$ FLEET_TMUX_SESSION=fd-p4-stall-$$
export FLEET_CLAUDE_HOME=$(mktemp -d) CODEX_HOME=$(mktemp -d) KIRO_HOME=$(mktemp -d) FLEET_AGY_HOME=$(mktemp -d)
trap 'tmux -L "$FLEET_TMUX_SOCKET" kill-server 2>/dev/null; rm -rf "$FLEET_STATE"' EXIT
tmux -L "$FLEET_TMUX_SOCKET" new-session -d -s "$FLEET_TMUX_SESSION" -n p 'sleep 600'
tmux -L "$FLEET_TMUX_SOCKET" new-window -t "$FLEET_TMUX_SESSION" -n w 'sleep 600'
tmux -L "$FLEET_TMUX_SOCKET" new-window -t "$FLEET_TMUX_SESSION" -n q 'sleep 600'
panes=($(tmux -L "$FLEET_TMUX_SOCKET" list-panes -s -t "$FLEET_TMUX_SESSION" -F '#{pane_id}'))
act=$(tmux -L "$FLEET_TMUX_SOCKET" display -p -t "${panes[0]}" '#{window_activity}')
cat > "$FLEET_STATE/roster.json" <<EOF
{"probe-1": {"cli": "opencode", "role": "probe", "status": "live", "pane": "${panes[0]}"},
 "worker-1": {"cli": "opencode", "role": "worker", "status": "live", "pane": "${panes[1]}"},
 "probe-2": {"cli": "opencode", "role": "probe", "status": "parked", "pane": "${panes[2]}"}}
EOF
action() { FLEET_NOW=$1 "$ROOT/bin/fleet" tick --json | python3 -c "import json,sys; d=json.load(sys.stdin); d=d.get('lanes', d) if isinstance(d, dict) else d; print([x['action'] for x in d if x['name']=='$2'][0])"; }
ok "$(action $((act + 901)) probe-1)" park-stalled "probe idle 15m01s -> park-stalled"
ok "$(action $((act + 840)) probe-1)" - "control: probe idle 14m -> no action"
ok "$(action $((act + 901)) worker-1)" orchestrator-decides "control: worker at the same age is not park-stalled"
ok "$(action $((act + 901)) probe-2)" - "control: parked probe -> no action"
out=$(FLEET_NOW=$((act + 901)) "$ROOT/bin/fleet" cleanup 2>&1)
ok "$(printf '%s\n' "$out" | grep -c '^  STALLED-PROBE probe-1 ')" 1 "cleanup lists the stalled probe"
ok "$(printf '%s\n' "$out" | grep -c 'STALLED-PROBE worker-1\|STALLED-PROBE probe-2')" 0 "control: cleanup lists no other lane"
ok "$(jq -r '."probe-1".status' "$FLEET_STATE/roster.json")" live "dry cleanup parked nothing (probe-1 still live)"
out=$(FLEET_NOW=$((act + 840)) "$ROOT/bin/fleet" cleanup 2>&1)
ok "$(printf '%s\n' "$out" | grep -c 'STALLED-PROBE')" 0 "control: nothing stalled at 14m"
echo "probe stall park: $pass passed, $fail failed"
[ "$fail" = 0 ]
