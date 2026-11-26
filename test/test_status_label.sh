#!/usr/bin/env bash
# test_status_label.sh: issue #44, `fleet status-label` (the fleet session's status-left). Private tmux server
# (tmux -L kit44test-PID), temp FLEET_STATE, stub claude; never the live fleet (every FLEET_* var from the calling
# lane is dropped first). Covers default (no option set), set (byte-exact, sizing), reset, the env override,
# survival of a server restart, a format that renders, no-session, and wrong usage. Exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
HERE=$(cd "$(dirname "$0")" && pwd -P)
ROOT=$(cd "$HERE/.." && pwd -P)
FLEET=$ROOT/bin/fleet
T=$(mktemp -d "${TMPDIR:-/tmp}/statuslabel.XXXXXX")
SOCK=kit44test-$$
cleanup() { tmux -L "$SOCK" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
export FLEET_CLAUDE_BIN=$HERE/stubs/claude FLEET_DIR=$T
S=$T/state; SESS=lblsess
printf '%s\n' 'Do the thing.' > "$T/prompt.txt"

FAILS=0; PASSES=0
t() { local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; PASSES=$((PASSES+1)); else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi; }
tm() { tmux -L "$SOCK" "$@"; }
fl() { env FLEET_STATE=$S FLEET_TMUX_SOCKET=$SOCK FLEET_TMUX_SESSION=$SESS "$FLEET" "$@"; }
launch() { fl launch --no-proof --no-desks --dir "$T" "$1" prompt "$T/prompt.txt"; }
sval() { tm show-options -v -t "=$SESS:" "$1" 2>/dev/null; }          # session scope only (no -g)
gval() { tm show-options -gv "$1" 2>/dev/null; }
hex() { printf '%s' "$1" | od -An -c | tr -s ' '; }
base() { tm kill-server >/dev/null 2>&1; tm new-session -d -s base -c "$T"; tm set-option -g status-left '[g] '; }

# ---- control: the harness reports a failing check
ctl=$(FAILS=0; PASSES=0; t "inner" false; echo "inner_fails=$FAILS")
t "control: a failing check prints FAIL and counts it" bash -c 'printf "%s" "$1" | grep -q "^FAIL inner" && printf "%s" "$1" | grep -q "inner_fails=1"' _ "$ctl"

# ===== 1. default: no file, no env, launch sets no status option
base
G0=$(gval status-left); GL0=$(gval status-left-length)
out=$(launch W1 2>&1); rc=$?
t "default: launch exits 0" test "$rc" = 0
t "default: session status-left is not set at session scope (empty)" test -z "$(sval status-left)"
t "default: session status-left-length is not set at session scope (empty)" test -z "$(sval status-left-length)"
t "default: global status-left unchanged" test "$(gval status-left)" = "$G0"
t "default: global status-left-length unchanged" test "$(gval status-left-length)" = "$GL0"
t "control: the session-scope probe does see a value once one is set" bash -c 'tmux -L "$1" set-option -t "=$2:" status-left X && test "$(tmux -L "$1" show-options -v -t "=$2:" status-left)" = X; tmux -L "$1" set-option -u -t "=$2:" status-left' _ "$SOCK" "$SESS"
t "default: no-arg shows saved: none" bash -c 'fl() { env FLEET_STATE=$1 FLEET_TMUX_SOCKET=$2 FLEET_TMUX_SESSION=$3 "$4" "${@:5}"; }; fl "$1" "$2" "$3" "$4" status-label | grep -qx "saved: none"' _ "$S" "$SOCK" "$SESS" "$FLEET"

# ===== 2. set: byte-exact, sizing
LONG='[#{session_name}] a fairly long label '
out=$(fl status-label "$LONG" 2>&1); rc=$?
t "set: exits 0 and says applied" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "saved and applied"' _ "$rc" "$out"
t "set: the file holds the label verbatim, trailing space kept (od)" test "$(od -An -c < "$S/status-label" | tr -s ' ')" = "$(hex "$LONG")"
t "set: session status-left is byte-exact, trailing space kept (od)" test "$(hex "$(sval status-left; printf x)")" = "$(hex "$LONG"$'\n'x)"
RENDERED="[$SESS] a fairly long label "
t "set: status-left-length = rendered width + 1 for a long label (got $(sval status-left-length), want $((${#RENDERED}+1)))" test "$(sval status-left-length)" = "$((${#RENDERED}+1))"
t "set: global status-left untouched" test "$(gval status-left)" = "$G0"
t "set: global status-left-length untouched" test "$(gval status-left-length)" = "$GL0"
fl status-label 'ab ' >/dev/null 2>&1
t "set: a short label keeps status-left-length at 10 (got $(sval status-left-length))" test "$(sval status-left-length)" = 10
fl status-label 'ünï€ ' >/dev/null 2>&1
t "set: width counts characters not bytes (5 chars, length 10, got $(sval status-left-length))" test "$(sval status-left-length)" = 10
UNI=$(printf 'é%.0s' 1 2 3 4 5 6 7 8 9 10 11 12)  # 12 chars, 24 bytes
fl status-label "$UNI" >/dev/null 2>&1
t "set: 12 two-byte chars give length 13, not 25 (got $(sval status-left-length))" test "$(sval status-left-length)" = 13
t "set: no-arg prints saved and live" bash -c 'o=$(env FLEET_STATE=$1 FLEET_TMUX_SOCKET=$2 FLEET_TMUX_SESSION=$3 "$4" status-label); printf "%s" "$o" | grep -q "^saved: " && printf "%s" "$o" | grep -q "^live: "' _ "$S" "$SOCK" "$SESS" "$FLEET"

# ===== 3. a format renders
fl status-label '[#{session_name}:#{window_index}] ' >/dev/null 2>&1
t "format: status-left holds the raw format" test "$(sval status-left)" = '[#{session_name}:#{window_index}] '
t "format: it renders (expanded to [$SESS:N] )" bash -c 'tmux -L "$1" display-message -p -t "=$2:" "$(tmux -L "$1" show-options -v -t "=$2:" status-left)" | grep -Eq "^\[$2:[0-9]+\] \$"' _ "$SOCK" "$SESS"
FMTR="[$SESS:$(tm list-windows -t "=$SESS:" -F '#{window_index}' | head -1)] "
t "format: length is rendered width + 1 (got $(sval status-left-length), want $((${#FMTR}+1)))" test "$(sval status-left-length)" = "$((${#FMTR}+1))"

# ===== 4. reset
fl status-label "$LONG" >/dev/null 2>&1
out=$(fl status-label --reset 2>&1); rc=$?
t "reset: exits 0" test "$rc" = 0
t "reset: the file is gone" test ! -e "$S/status-label"
t "reset: session status-left is unset" test -z "$(sval status-left)"
t "reset: session status-left-length is unset" test -z "$(sval status-left-length)"
t "reset: global status-left byte-identical to before (od)" test "$(hex "$(gval status-left)")" = "$(hex "$G0")"
t "reset: global status-left-length byte-identical to before" test "$(gval status-left-length)" = "$GL0"
t "reset: the effective value falls back to the global one" test "$(tm show-options -Av -t "=$SESS:" status-left 2>/dev/null)" = "$G0"

# ===== 5. env override
fl status-label 'file ' >/dev/null 2>&1
t "env: launch with FLEET_STATUS_LABEL applies the env label over the file" bash -c 'env FLEET_STATUS_LABEL="envlbl " FLEET_STATE=$1 FLEET_TMUX_SOCKET=$2 FLEET_TMUX_SESSION=$3 FLEET_CLAUDE_BIN=$4 FLEET_DIR=$5 "$6" launch --no-proof --no-desks W2 prompt "$5/prompt.txt" >/dev/null 2>&1; test "$(tmux -L "$2" show-options -v -t "=$3:" status-left)" = "envlbl "' _ "$S" "$SOCK" "$SESS" "$FLEET_CLAUDE_BIN" "$T" "$FLEET"
t "control: without the env, a launch re-applies the file label" bash -c 'env FLEET_STATE=$1 FLEET_TMUX_SOCKET=$2 FLEET_TMUX_SESSION=$3 FLEET_CLAUDE_BIN=$4 FLEET_DIR=$5 "$6" launch --no-proof --no-desks W3 prompt "$5/prompt.txt" >/dev/null 2>&1; test "$(tmux -L "$2" show-options -v -t "=$3:" status-left)" = "file "' _ "$S" "$SOCK" "$SESS" "$FLEET_CLAUDE_BIN" "$T" "$FLEET"
fl status-label --reset >/dev/null 2>&1

# ===== 6. survives a server restart
fl status-label 'persist-#{session_name} ' >/dev/null 2>&1
tm kill-server >/dev/null 2>&1
t "restart: the server is gone (control)" bash -c '! tmux -L "$1" has-session 2>/dev/null' _ "$SOCK"
out=$(launch W4 2>&1); rc=$?
t "restart: a fresh launch exits 0" test "$rc" = 0
t "restart: the fresh session shows the saved label" test "$(sval status-left)" = 'persist-#{session_name} '
t "restart: and its length is sized (got $(sval status-left-length))" test "$(sval status-left-length)" = "$((8+${#SESS}+1+1))"
fl status-label --reset >/dev/null 2>&1

# ===== 7. no session yet: save only, and say so
tm kill-server >/dev/null 2>&1
out=$(fl status-label 'later ' 2>&1); rc=$?
t "no session: exits 0, saves, says it applies on the next launch" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "no tmux session" && test "$(cat "$3")" = "later "' _ "$rc" "$out" "$S/status-label"
t "no session: --reset still works and removes the file" bash -c 'env FLEET_STATE=$1 FLEET_TMUX_SOCKET=$2 FLEET_TMUX_SESSION=$3 "$4" status-label --reset >/dev/null 2>&1 && test ! -e "$1/status-label"' _ "$S" "$SOCK" "$SESS" "$FLEET"

# ===== 8. wrong usage exits non-zero
fl status-label a b >/dev/null 2>&1; rc=$?
t "usage: two arguments exit non-zero (rc $rc)" test "$rc" != 0
fl status-label --bogus >/dev/null 2>&1; rc=$?
t "usage: an unknown flag exits non-zero (rc $rc)" test "$rc" != 0
fl status-label --reset extra >/dev/null 2>&1; rc=$?
t "usage: --reset with an extra argument exits non-zero (rc $rc)" test "$rc" != 0
t "control: a valid form exits zero" fl status-label --reset

echo "status-label: $PASSES passed, $FAILS failed"
[ "$FAILS" = 0 ]
