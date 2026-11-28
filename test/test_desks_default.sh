#!/usr/bin/env bash
# test_desks_default.sh: issue #9, deskA and deskB are the default. Covers the installer step (--no-desks, --dry-run),
# the fleet launch desks guard (brings the desks up, idempotent, --no-desks, FAIL with a hint), the fleet tick
# `desks down` flag, and fleet desks up --handoff DIR. Private tmux sockets, temp state, stub CLIs; never the live
# fleet (every FLEET_* var from the calling lane is dropped first). Exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
HERE=$(cd "$(dirname "$0")" && pwd -P)
ROOT=$(cd "$HERE/.." && pwd -P)
FLEET=$ROOT/bin/fleet
T=$(mktemp -d "${TMPDIR:-/tmp}/deskdefault.XXXXXX")
SOCKS="inst-$$ launch-$$ nodesk-$$ fail-$$ tick-$$ hand-$$"  # every private tmux socket this test uses
sock() { printf '%s' "$1"; }
cleanup() { for s in $SOCKS; do tmux -L "$s" kill-server >/dev/null 2>&1; done; rm -rf "$T"; }
trap cleanup EXIT
export FLEET_CLAUDE_BIN=$HERE/stubs/claude FLEET_DIR=$T

FAILS=0
t() { local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi; }
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnot() { ! printf '%s' "$1" | grep -qF -- "$2"; }
live_pane() { [ -n "$2" ] && tmux -L "$1" list-panes -a -F '#{pane_id} #{pane_dead}' 2>/dev/null | grep -qx "$2 0"; }
rget() { jq -r --arg n "$2" --arg f "$3" '.[$n][$f] // empty' "$1/roster.json" 2>/dev/null; }
wins() { tmux -L "$1" list-windows -t "=$2" -F '#{window_index}:#{window_name}' 2>/dev/null | LC_ALL=C sort -n | tr '\n' ' '; }

# ---- control: the harness reports a failing check
ctl=$(FAILS=0; t "inner" false; echo "inner_fails=$FAILS")
t "control: a failing check prints FAIL and counts it" bash -c 'printf "%s" "$1" | grep -q "^FAIL inner" && printf "%s" "$1" | grep -q "inner_fails=1"' _ "$ctl"

# =========================================================== 1. installer step
IH=$T/home; mkdir -p "$IH/.claude"
FAKE=$T/fakefleet; FLOG=$T/fakefleet.log
printf '#!/usr/bin/env bash\necho "$*" >> %q\nexit ${FAKE_RC:-0}\n' "$FLOG" > "$FAKE"; chmod +x "$FAKE"
inst() { # script args... : scratch HOME, scratch socket and state, fake fleet; FLEET_NO_DESKS dropped
  local s=$1; shift
  ( unset CODEX_HOME KIRO_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME FLEET_NO_DESKS
    export HOME=$IH FLEET_BIN=$FAKE FLEET_TMUX_SOCKET=$(sock inst-$$) FLEET_TMUX_SESSION=isess FLEET_STATE=$T/istate
    bash "$ROOT/install/$s" --prefix "$IH/.local" "$@" ) 2>&1
}
: > "$FLOG"; out=$(inst claude.sh); rc=$?
t "install/claude.sh exits 0 and ends by running fleet desks up --cli claude" bash -c 'test "$1" = 0 && test "$(cat "$2")" = "desks up --cli claude"' _ "$rc" "$FLOG"
t "install/claude.sh: the desks step is the last step (after the rules block)" bash -c 'printf "%s\n" "$1" | grep -n "fleet rules block\|desks up" | tail -1 | grep -q "desks up"' _ "$out"
: > "$FLOG"; out=$(inst claude.sh --no-desks); rc=$?
t "install/claude.sh --no-desks exits 0, runs no desks up and says so" bash -c 'test "$1" = 0 && test ! -s "$2" && printf "%s" "$3" | grep -q "skipped: fleet desks up (--no-desks)"' _ "$rc" "$FLOG" "$out"
: > "$FLOG"; out=$(inst claude.sh --dry-run); rc=$?
t "install/claude.sh --dry-run prints the desks up step and runs nothing" bash -c 'test "$1" = 0 && test ! -s "$2" && printf "%s" "$3" | grep -q "^\[dry-run\] would run: .* desks up --cli claude$"' _ "$rc" "$FLOG" "$out"
for s in codex kiro opencode agy; do
  : > "$FLOG"; out=$(inst $s.sh --dry-run); rc=$?
  t "install/$s.sh --dry-run prints fleet desks up --cli $s" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "would run: .* desks up --cli $3$"' _ "$rc" "$out" "$s"
done
: > "$FLOG"; out=$(FAKE_RC=1 inst claude.sh); rc=$?
t "install/claude.sh: a failed desks up FAILs with the rerun hint" bash -c 'test "$1" != 0 && printf "%s" "$2" | grep -q "^FAIL fleet desks up failed .*--no-desks"' _ "$rc" "$out"
: > "$FLOG"; out=$(inst claude.sh --trust "$T" --trust-only); rc=$?
t "install/claude.sh --trust-only runs no desks up" bash -c 'test "$1" = 0 && test ! -s "$2"' _ "$rc" "$FLOG"

# =========================================================== 2. fleet launch desks guard
printf '%s\n' 'Do the thing.' > "$T/prompt.txt"
L=$T/lstate; LS=$(sock launch-$$)
fl() { env FLEET_STATE=$L FLEET_TMUX_SOCKET=$LS FLEET_TMUX_SESSION=lsess "$FLEET" "$@"; }
out=$(fl launch --dry-run --dir "$T" W0 prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch --dry-run with no desks prints the desks up step" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "^would run: fleet desks up --dir .* (no live ROUTE or DECIDE)"' _ "$rc" "$out"
t "launch --dry-run touches no state" test ! -e "$L/roster.json"
out=$(fl launch --dry-run --no-desks --dir "$T" W0 prompt "$T/prompt.txt" 2>&1)
t "launch --dry-run --no-desks prints no desks step" hasnot "$out" "desks up"
out=$(fl launch --no-proof --dir "$T" W1 prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch into a fleet with no desks exits 0 and launches the lane" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "^OK W1 "' _ "$rc" "$out"
t "launch brought all five desks up first (live panes)" bash -c 'for d in ROUTE DECIDE AUTH WATCH LEARN; do p=$(jq -r --arg d $d ".[\$d].pane // empty" "$1/roster.json"); tmux -L "$2" list-panes -a -F "#{pane_id} #{pane_dead}" | grep -qx "$p 0" || exit 1; done' _ "$L" "$LS"
t "launch: desks at windows 0 deskA and 1 deskB, the lane at 2 (got: $(wins "$LS" lsess))" test "$(wins "$LS" lsess)" = "0:deskA 1:deskB 2:W1 "
t "launch says why it brought the desks up" has "$out" "no live ROUTE or DECIDE: bringing the desks up first"
r1=$(rget "$L" ROUTE pane); d1=$(rget "$L" DECIDE pane)
out=$(fl launch --no-proof --dir "$T" W2 prompt "$T/prompt.txt" 2>&1); rc=$?
t "second launch with live desks: OK, desks untouched (same ROUTE and DECIDE panes, no desks step)" bash -c 'test "$1" = 0 && test "$2" = "$3" && test "$4" = "$5" && ! printf "%s" "$6" | grep -q "bringing the desks up"' _ "$rc" "$r1" "$(rget "$L" ROUTE pane)" "$d1" "$(rget "$L" DECIDE pane)" "$out"
# a dead DECIDE alone is enough: the guard relaunches it (idempotent desks up), ROUTE keeps its pane
tmux -L "$LS" kill-pane -t "$d1"
out=$(fl launch --no-proof --dir "$T" W3 prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch with a dead DECIDE relaunches it and keeps ROUTE" bash -c 'test "$1" = 0 && test "$2" = "$3" && test "$4" != "$5" && tmux -L "$6" list-panes -a -F "#{pane_id}" | grep -qx "$5"' _ "$rc" "$r1" "$(rget "$L" ROUTE pane)" "$d1" "$(rget "$L" DECIDE pane)" "$LS"
# --no-desks and FLEET_NO_DESKS=1: a fleet with no desks stays so
N=$T/nstate; NS=$(sock nodesk-$$)
fn() { env FLEET_STATE=$N FLEET_TMUX_SOCKET=$NS FLEET_TMUX_SESSION=nsess "$FLEET" "$@"; }
out=$(fn launch --no-proof --no-desks --dir "$T" W1 prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch --no-desks launches the lane and no desk" bash -c 'test "$1" = 0 && test "$(jq -r "keys|join(\",\")" "$2/roster.json")" = W1' _ "$rc" "$N"
out=$(FLEET_NO_DESKS=1 fn launch --no-proof --dir "$T" W2 prompt "$T/prompt.txt" 2>&1); rc=$?
t "FLEET_NO_DESKS=1 is the env twin of --no-desks" bash -c 'test "$1" = 0 && test "$(jq -r "keys|join(\",\")" "$2/roster.json")" = W1,W2' _ "$rc" "$N"
out=$(fn launch --no-proof --dir "$T" D9 desk 2>&1); rc=$?
t "a desk-mode launch is exempt from the guard (no recursion, no desks step)" bash -c 'test "$1" = 0 && ! printf "%s" "$2" | grep -q "desks up" && ! jq -e ".ROUTE" "$3/roster.json"' _ "$rc" "$out" "$N"
# desks up fails -> FAIL no desks, no lane launched
F=$T/fstate; FS=$(sock fail-$$)
out=$(env FLEET_STATE=$F FLEET_TMUX_SOCKET=$FS FLEET_TMUX_SESSION=fsess FLEET_CLAUDE_BIN=$T/no-such-claude "$FLEET" launch --no-proof --cli codex --dir "$T" W1 prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch FAILs with the no-desks hint when desks up fails" bash -c 'test "$1" != 0 && printf "%s" "$2" | grep -q "^FAIL no desks: .*run fleet desks up.*--no-desks"' _ "$rc" "$out"
t "the failed guard launched no lane" bash -c '! jq -e ".W1" "$1/roster.json"' _ "$F"

# =========================================================== 3. fleet tick flags desks down
K=$T/kstate; KS=$(sock tick-$$); mkdir -p "$K"
fk() { env FLEET_STATE=$K FLEET_TMUX_SOCKET=$KS FLEET_TMUX_SESSION=ksess "$FLEET" "$@"; }
tk=$(fk tick 2>/dev/null)
t "tick on a fleet with no lanes and no desks flags both windows" has "$tk" "WARN desks down: deskA (ROUTE missing, DECIDE missing, AUTH missing); deskB (WATCH missing, LEARN missing); run fleet desks up"
fk desks up --no-proof --dir "$T" >/dev/null 2>&1
tk=$(fk tick 2>/dev/null)
t "tick with all five desks live: no desks warning" hasnot "$tk" "desks down"
tmux -L "$KS" kill-pane -t "$(rget "$K" LEARN pane)"
tk=$(fk tick 2>/dev/null)
t "tick names a dead desk pane in its window" bash -c 'printf "%s" "$1" | grep -qx "WARN desks down: deskB (LEARN dead); run fleet desks up"' _ "$tk"
tj=$(fk tick --json 2>/dev/null)
t "tick --json stays pure JSON (no warning line)" bash -c 'printf "%s" "$1" | jq -e . && ! printf "%s" "$1" | grep -q "desks down"' _ "$tj"

# =========================================================== 4. desks up --handoff DIR
H=$T/handoffs; mkdir -p "$H"
printf 'Was: ORCHESTRATOR\nopen: route PROJ-1 to lane-a\n' > "$H/ORCHESTRATOR.md"
printf 'Was: DECIDE\nopen: Q12 waiting\n' > "$H/DECIDE.md"
P=$T/pstate; PS=$(sock hand-$$); mkdir -p "$P"
fp() { env FLEET_STATE=$P FLEET_TMUX_SOCKET=$PS FLEET_TMUX_SESSION=psess "$FLEET" "$@"; }
# a live pre-kit DECISIONS blocks the handoff migration
tmux -L "$PS" new-session -d -s psess -n DECISIONS 'sleep 600'
op=$(tmux -L "$PS" list-panes -t "=psess:DECISIONS" -F '#{pane_id}')
jq -n --arg p "$op" '{DECISIONS:{cli:"claude",role:"decide",mode:"desk",pane:$p}}' > "$P/roster.json"
out=$(fp desks up --no-proof --dir "$T" --handoff "$H" 2>&1); rc=$?
t "desks up --handoff refuses while a pre-kit DECISIONS is live" bash -c 'test "$1" != 0 && printf "%s" "$2" | grep -q "^FAIL --handoff: pre-kit desk DECISIONS is still live"' _ "$rc" "$out"
t "the refused run launched no desk" bash -c '! jq -e ".ROUTE // .DECIDE" "$1/roster.json"' _ "$P"
tmux -L "$PS" kill-pane -t "$op"
out=$(fp desks up --no-proof --dir "$T" --handoff "$H" --dry-run 2>&1); rc=$?
t "desks up --handoff --dry-run names each pickup" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "^ROUTE picks up handoff .*/ORCHESTRATOR.md" && printf "%s" "$2" | grep -q "^DECIDE picks up handoff .*/DECIDE.md"' _ "$rc" "$out"
out=$(fp desks up --no-proof --dir "$T" --handoff "$H" 2>&1); rc=$?
t "desks up --handoff exits 0" test "$rc" = 0
t "ROUTE starts on its predecessor ORCHESTRATOR's handoff" bash -c 'grep -q "predecessor.s handoff is .*/handoffs/ORCHESTRATOR.md: read it first" "$1/launch/ROUTE.txt"' _ "$P"
t "DECIDE starts on DECIDE.md (own name wins)" bash -c 'grep -q "handoffs/DECIDE.md: read it first" "$1/launch/DECIDE.txt"' _ "$P"
t "WATCH and LEARN have no handoff in DIR: plain brief" bash -c '! grep -q "predecessor" "$1/launch/WATCH.txt" && ! grep -q "predecessor" "$1/launch/LEARN.txt"' _ "$P"
t "a desk brief with a handoff still never starts with /goal" bash -c '! head -c 5 "$1/launch/ROUTE.txt" | grep -q "^/goal"' _ "$P"
cp "$P/launch/ROUTE.txt" "$T/route1.txt"
out=$(fp desks up --no-proof --dir "$T" --handoff "$H" 2>&1); rc=$?
t "rerun with live desks: nothing relaunched, no pickup line" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "^ROUTE already live" && ! printf "%s" "$2" | grep -q "picks up"' _ "$rc" "$out"
out=$(fp desks up --no-proof --dir "$T" --handoff "$T/nope" 2>&1); rc=$?
t "desks up --handoff with a missing dir FAILs" bash -c 'test "$1" != 0 && printf "%s" "$2" | grep -q "^FAIL --handoff: not a directory"' _ "$rc" "$out"

if [ "$FAILS" = 0 ]; then echo "PASS test_desks_default.sh: all assertions"; exit 0; fi
echo "FAIL test_desks_default.sh: $FAILS failed"; exit 1
