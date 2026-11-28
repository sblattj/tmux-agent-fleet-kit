#!/usr/bin/env bash
# test_show.sh: issue #41, `fleet show IMAGE [--text SUMMARY]` and FLEET_DESK_PASSTHROUGH. Every mode (kitty,
# symbols for a mosh viewer, symbols with passthrough off and the option still off afterwards, no tmux by TERM,
# FLEET_GFX overrides, chafa missing, no clients), the tmux shim dropping set-option, sizing from the real pane,
# the static "allow-passthrough is only ever set on (-p -t <pane> or -g), never off" rule, and the v0.8.1 default:
# fleet launch and fleet desks up turn it on server-wide unless FLEET_DESK_PASSTHROUGH=0, and never turn it off.
# Throwaway tmux servers (tmux -L), a stub chafa that tries `tmux set-option allow-passthrough on` like the real one,
# temp state; never the live fleet. Exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
unset TMUX TMUX_PANE TERM_PROGRAM KITTY_WINDOW_ID
HERE=$(cd "$(dirname "$0")" && pwd -P)
ROOT=$(cd "$HERE/.." && pwd -P)
FLEET=$ROOT/bin/fleet
SHIM=$ROOT/lib/fleet-show-shim/tmux
T=$(mktemp -d "${TMPDIR:-/tmp}/fleetshow.XXXXXX")
S=show-$$ D1=showdesk1-$$ D2=showdesk2-$$ D3=showlaunch3-$$ D4=showlaunch4-$$
cleanup() { for s in $S $D1 $D2 $D3 $D4; do tmux -L "$s" kill-server >/dev/null 2>&1; done; rm -rf "$T"; }
trap cleanup EXIT

FAILS=0
t() { local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi; }
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnot() { ! printf '%s' "$1" | grep -qF -- "$2"; }

# ---- control: the harness reports a failing check
ctl=$(FAILS=0; t "inner" false; echo "inner_fails=$FAILS")
t "control: a failing check prints FAIL and counts it" bash -c 'printf "%s" "$1" | grep -q "^FAIL inner" && printf "%s" "$1" | grep -q "inner_fails=1"' _ "$ctl"

# ---- fixtures: an image, a stub chafa, a fake process table
IMG=$T/chart.png
printf '\211PNG\r\n\032\n' > "$IMG"   # the stub never decodes it; the real chafa is used by the live proof only
mkdir -p "$T/bin"
CLOG=$T/chafa.log
cat > "$T/bin/chafa" <<EOF
#!/usr/bin/env bash
# stub chafa: log argv, draw a marker, then do what the real chafa does when passthrough is off
printf 'CHAFA %s\n' "\$*" >> "$CLOG"
printf 'CHAFA-DREW %s\n' "\$*"
if [ -n "\${TMUX:-}" ]; then tmux set-option allow-passthrough on; fi
exit 0
EOF
chmod +x "$T/bin/chafa"
STUB=$T/bin/chafa
cat > "$T/ps-plain" <<'EOF'
  111   100 tmux
  100     1 -zsh
EOF
cat > "$T/ps-mosh" <<'EOF'
  111   100 tmux
  100     1 -zsh
  222   220 tmux
  220   219 -bash
  219     1 /opt/homebrew/bin/mosh-server
EOF

# ======================================================== 1. no tmux: TERM decides
nt() { env -u TMUX -u TMUX_PANE -u TERM_PROGRAM -u KITTY_WINDOW_ID FLEET_CHAFA_BIN=$STUB FLEET_SHOW_SIZE=40x12 "$@"; }
: > "$CLOG"; out=$(nt TERM=xterm-ghostty "$FLEET" show "$IMG" --text "p95 down" 2>"$T/e1"); err=$(cat "$T/e1")
t "no tmux, TERM=xterm-ghostty -> mode=kitty" has "$err" "fleet-show: mode=kitty (no tmux, TERM=xterm-ghostty)"
t "no tmux kitty: chafa -f kitty --passthrough none, sized 40x9" has "$(cat "$CLOG")" "-f kitty --passthrough none --size 40x9 $IMG"
t "the text line is printed after the image" bash -c 'test "$(printf "%s\n" "$1" | tail -1)" = "p95 down"' _ "$out"
out=$(nt TERM=xterm-kitty "$FLEET" show "$IMG" 2>&1)
t "no tmux, TERM=xterm-kitty -> kitty" has "$out" "mode=kitty (no tmux, TERM=xterm-kitty)"
out=$(nt TERM=xterm-256color TERM_PROGRAM=ghostty "$FLEET" show "$IMG" 2>&1)
t "no tmux, TERM_PROGRAM=ghostty -> kitty" has "$out" "mode=kitty (no tmux, TERM_PROGRAM=ghostty)"
out=$(nt TERM=xterm-256color KITTY_WINDOW_ID=3 "$FLEET" show "$IMG" 2>&1)
t "no tmux, KITTY_WINDOW_ID -> kitty" has "$out" "mode=kitty (no tmux, KITTY_WINDOW_ID set)"
: > "$CLOG"; out=$(nt TERM=xterm-256color "$FLEET" show "$IMG" 2>&1)
t "no tmux, TERM=xterm-256color (control: not Kitty) -> symbols" has "$out" "mode=symbols (no tmux, TERM=xterm-256color is not Kitty-capable)"
t "symbols: chafa -f symbols -c full --passthrough none" has "$(cat "$CLOG")" "-f symbols -c full --passthrough none --size 40x9"

# ======================================================== 2. FLEET_GFX overrides, chafa missing, text sources
: > "$CLOG"; out=$(nt TERM=xterm-ghostty FLEET_GFX=text "$FLEET" show "$IMG" --text "only words" 2>&1)
t "FLEET_GFX=text -> mode=text, text line printed" bash -c 'printf "%s" "$1" | grep -qF "mode=text (FLEET_GFX=text)" && printf "%s" "$1" | grep -qx "only words"' _ "$out"
t "FLEET_GFX=text runs no chafa" test ! -s "$CLOG"
out=$(nt TERM=xterm-ghostty FLEET_GFX=symbols "$FLEET" show "$IMG" 2>&1)
t "FLEET_GFX=symbols overrides a Kitty TERM" has "$out" "mode=symbols (FLEET_GFX=symbols)"
out=$(nt TERM=xterm-256color FLEET_GFX=kitty "$FLEET" show "$IMG" 2>&1)
t "FLEET_GFX=kitty forces kitty on a plain TERM" has "$out" "mode=kitty (FLEET_GFX=kitty)"
out=$(nt TERM=xterm-ghostty FLEET_GFX=bogus "$FLEET" show "$IMG" 2>&1); rc=$?
t "FLEET_GFX=bogus FAILs (exit 2)" bash -c 'test "$1" = 2 && printf "%s" "$2" | grep -q "FLEET_GFX must be"' _ "$rc" "$out"
out=$(nt TERM=xterm-ghostty FLEET_CHAFA_BIN=$T/no-such-chafa "$FLEET" show "$IMG" --text "still here" 2>&1); rc=$?
t "chafa missing -> mode=text (chafa not installed), text printed, exit 0" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -qF "mode=text (chafa not installed (wanted kitty" && printf "%s" "$2" | grep -qx "still here"' _ "$rc" "$out"
printf 'sidecar summary\nsecond line\n' > "$T/chart.txt"
out=$(nt TERM=xterm-ghostty FLEET_GFX=text "$FLEET" show "$IMG" 2>/dev/null)
t "no --text: the sidecar chart.txt first line is the text line" test "$out" = "sidecar summary"
rm -f "$T/chart.txt"
out=$(nt TERM=xterm-ghostty FLEET_GFX=text "$FLEET" show "$IMG" 2>/dev/null)
t "no --text, no sidecar: 'image: chart.png'" test "$out" = "image: chart.png"
out=$(nt "$FLEET" show "$T/nope.png" 2>&1); rc=$?
t "missing image FAILs (exit 2)" bash -c 'test "$1" = 2 && printf "%s" "$2" | grep -q "no such image"' _ "$rc" "$out"
cat > "$T/bin/chafa-fail" <<'EOF'
#!/usr/bin/env bash
exit 3
EOF
chmod +x "$T/bin/chafa-fail"
out=$(nt TERM=xterm-ghostty FLEET_CHAFA_BIN=$T/bin/chafa-fail "$FLEET" show "$IMG" --text x 2>&1)
t "chafa failing: a last mode=text line, text still printed" bash -c 'printf "%s\n" "$1" | grep "^fleet-show:" | tail -1 | grep -qF "mode=text (chafa failed with exit 3 in kitty mode)" && printf "%s" "$1" | grep -qx x' _ "$out"

# ======================================================== 3. the shim
REALLOG=$T/real-tmux.log
cat > "$T/bin/realtmux" <<EOF
#!/usr/bin/env bash
printf 'REAL %s\n' "\$*" >> "$REALLOG"
EOF
chmod +x "$T/bin/realtmux"
SLOG=$T/shim.log
sh_run() { : > "$REALLOG"; : > "$SLOG"; FLEET_SHOW_REAL_TMUX=$T/bin/realtmux FLEET_SHOW_SHIM_LOG=$SLOG "$SHIM" "$@"; }
sh_run show -pv allow-passthrough
t "shim control: a read (show -pv) reaches the real tmux" has "$(cat "$REALLOG")" "REAL show -pv allow-passthrough"
for c in "set-option allow-passthrough on" "set -u allow-passthrough" "setw allow-passthrough on" "set-window-option allow-passthrough on" "-L x set-option -p allow-passthrough on" "-S /tmp/s set -g allow-passthrough all" "display -p hi ; set-option allow-passthrough on"; do
  # shellcheck disable=SC2086
  sh_run $c
  t "shim drops 'tmux $c' (never reaches tmux, logged)" bash -c 'test ! -s "$1" && grep -qF "dropped: tmux" "$2"' _ "$REALLOG" "$SLOG"
done
sh_run -L x display-message -p '#{pane_id}'
t "shim control: -L x display-message passes through" has "$(cat "$REALLOG")" "REAL -L x display-message -p #{pane_id}"

# ======================================================== 4. in a real tmux pane (throwaway server)
tmux -L "$S" -f /dev/null new-session -d -s s -x 54 -y 20 'sleep 600'
tmux -L "$S" set -g default-size 54x20
tmux -L "$S" set -g allow-passthrough off
inpane() { # name [VAR=value...] : a pane that waits for $T/<name>.go, runs fleet show, records out/err; prints the pane id
  local n=$1; shift
  { echo '#!/usr/bin/env bash'
    echo "while [ ! -e $T/$n.go ]; do sleep 0.1; done"
    printf 'env PATH=%q' "$PATH"
    for kv in "$@"; do printf ' %q' "$kv"; done
    printf ' %q show %q --text %q >%q 2>%q\n' "$FLEET" "$IMG" "summary $n" "$T/$n.out" "$T/$n.err"
    echo "touch $T/$n.done; sleep 600"
  } > "$T/$n.sh"
  tmux -L "$S" new-window -d -t s: -P -F '#{pane_id}' "bash $T/$n.sh"
}
go() { local i=0; touch "$T/$1.go"; while [ ! -e "$T/$1.done" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done; [ -e "$T/$1.done" ]; }
anyset() { # socket -> every pane/window/global allow-passthrough value set on that server, one line each
  local s=$1 x
  printf 'global=%s\n' "$(tmux -L "$s" show -gv allow-passthrough 2>/dev/null)"
  for x in $(tmux -L "$s" list-windows -a -F '#{window_id}'); do v=$(tmux -L "$s" show -wv -t "$x" allow-passthrough 2>/dev/null); [ -z "$v" ] || echo "window $x=$v"; done
  for x in $(tmux -L "$s" list-panes -a -F '#{pane_id}'); do v=$(tmux -L "$s" show -pv -t "$x" allow-passthrough 2>/dev/null); [ -z "$v" ] || echo "pane $x=$v"; done
}
nowin() { ! anyset "$1" | grep -q "^window"; }  # no window-level value anywhere on the server
popt() { tmux -L "$S" show -Apv -t "$1" allow-passthrough 2>/dev/null; }
gopt() { tmux -L "$S" show -gv allow-passthrough 2>/dev/null; }
base=(FLEET_CHAFA_BIN=$STUB FLEET_SHOW_PS=$T/ps-plain "FLEET_SHOW_CLIENTS=111 xterm-ghostty" FLEET_SHOW_SHIM_LOG=$T/pane-shim.log)

# 4a control for the shim: the stub chafa run bare in a pane does flip the option (what the issue saw)
cp0=$(tmux -L "$S" new-window -d -t s: -P -F '#{pane_id}' "env PATH=$(printf %q "$PATH") $STUB $IMG >/dev/null; touch $T/bare.done; sleep 600")
i=0; while [ ! -e "$T/bare.done" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
# chafa's bare `tmux set-option` has no -t: it lands on the session's active window, not on the pane it ran in
t "control: bare stub chafa in a pane turns allow-passthrough on for a window (got: $(anyset "$S" | tr '\n' ' '))" bash -c 'printf "%s" "$1" | grep -q "^window @[0-9]*=on$"' _ "$(anyset "$S")"
tmux -L "$S" kill-pane -t "$cp0"
tmux -L "$S" set -u allow-passthrough 2>/dev/null; tmux -L "$S" set -g allow-passthrough off
for w in $(tmux -L "$S" list-windows -a -F '#{window_id}'); do tmux -L "$S" set -w -u -t "$w" allow-passthrough 2>/dev/null; done

# 4b kitty: every client Kitty-capable, passthrough on (pane level) -> kitty placeholders through tmux
: > "$CLOG"; : > "$T/pane-shim.log"
pk=$(inpane kitty "${base[@]}")
tmux -L "$S" set-option -p -t "$pk" allow-passthrough on
t "kitty pane ran" go kitty
t "kitty: mode=kitty, every client Kitty-capable and passthrough on" has "$(cat "$T/kitty.err")" "fleet-show: mode=kitty (1 Kitty-capable client(s), allow-passthrough on on this pane)"
t "kitty: chafa -f kitty --passthrough tmux, sized from the real 54-column pane" has "$(cat "$CLOG")" "-f kitty --passthrough tmux --size 54x17"
t "kitty: text line printed" bash -c 'tail -1 "$1" | grep -qx "summary kitty"' _ "$T/kitty.out"

# 4c symbols with passthrough off: option still off afterwards, chafa's set-option dropped by the shim
: > "$CLOG"; : > "$T/pane-shim.log"
po=$(inpane off "${base[@]}")
t "passthrough-off pane: option off before the run (got: $(popt "$po"))" test "$(popt "$po")" = off
t "off pane ran" go off
t "passthrough off -> mode=symbols with the hint" has "$(cat "$T/off.err")" "mode=symbols (allow-passthrough is off on this pane, so no real image; fleet launch and fleet desks up turn it on unless FLEET_DESK_PASSTHROUGH=0; fleet show never turns it on)"
t "passthrough off: chafa -f symbols" has "$(cat "$CLOG")" "-f symbols"
t "passthrough off: option still off on that pane afterwards (got: $(popt "$po"))" test "$(popt "$po")" = off
t "passthrough off: global still off (got: $(gopt))" test "$(gopt)" = off
t "passthrough off: no window-level value anywhere on the server (got: $(anyset "$S" | tr '\n' ' '))" nowin "$S"
t "the shim dropped chafa's set-option" grep -qF "dropped: tmux set-option allow-passthrough on" "$T/pane-shim.log"

# 4d kitty forced with passthrough off: kitty runs, chafa's set-option dropped, option stays off
: > "$T/pane-shim.log"
pf=$(inpane forced "${base[@]}" FLEET_GFX=kitty)
t "forced pane ran" go forced
t "FLEET_GFX=kitty in tmux -> mode=kitty" has "$(cat "$T/forced.err")" "mode=kitty (FLEET_GFX=kitty)"
t "forced kitty, passthrough off: option still off afterwards (got: $(popt "$pf"))" test "$(popt "$pf")" = off
t "forced kitty: no window-level value anywhere on the server" nowin "$S"
t "forced kitty: the shim dropped chafa's set-option" grep -qF "dropped: tmux set-option allow-passthrough on" "$T/pane-shim.log"

# 4e mosh viewer (mosh-server two levels up, comm a full path), passthrough on -> symbols for everyone
pm=$(inpane mosh FLEET_CHAFA_BIN=$STUB FLEET_SHOW_PS=$T/ps-mosh "FLEET_SHOW_CLIENTS=111 xterm-ghostty
222 xterm-256color")
tmux -L "$S" set-option -p -t "$pm" allow-passthrough on
t "mosh pane ran" go mosh
t "a mosh viewer -> mode=symbols (mosh viewer, client pid 222)" has "$(cat "$T/mosh.err")" "mode=symbols (mosh viewer (client pid 222)"
pm2=$(inpane moshghostty FLEET_CHAFA_BIN=$STUB FLEET_SHOW_PS=$T/ps-mosh "FLEET_SHOW_CLIENTS=111 xterm-ghostty
222 xterm-ghostty")
tmux -L "$S" set-option -p -t "$pm2" allow-passthrough on
t "mosh2 pane ran" go moshghostty
t "a mosh viewer whose TERM says ghostty is still mosh -> symbols" has "$(cat "$T/moshghostty.err")" "mode=symbols (mosh viewer (client pid 222)"

# 4f a non-Kitty client and no clients
pn=$(inpane nonkitty FLEET_CHAFA_BIN=$STUB FLEET_SHOW_PS=$T/ps-plain "FLEET_SHOW_CLIENTS=111 xterm-ghostty
333 screen-256color")
tmux -L "$S" set-option -p -t "$pn" allow-passthrough on
t "nonkitty pane ran" go nonkitty
t "a non-Kitty client -> symbols" has "$(cat "$T/nonkitty.err")" "mode=symbols (client pid 333 TERM=screen-256color is not Kitty-capable)"
pz=$(inpane none FLEET_CHAFA_BIN=$STUB FLEET_SHOW_PS=$T/ps-plain "FLEET_SHOW_CLIENTS=")
tmux -L "$S" set-option -p -t "$pz" allow-passthrough on
t "no-clients pane ran" go none
t "no client attached -> symbols" has "$(cat "$T/none.err")" "mode=symbols (no client attached to this session)"
# real list-clients (no hook): the throwaway server has no client attached
pr=$(inpane realclients FLEET_CHAFA_BIN=$STUB)
tmux -L "$S" set-option -p -t "$pr" allow-passthrough on
t "real list-clients pane ran" go realclients
t "real list-clients on a server with no client -> symbols (no client attached)" has "$(cat "$T/realclients.err")" "mode=symbols (no client attached to this session)"
t "passthrough on stays pane-level: global still off (got: $(gopt))" test "$(gopt)" = off

# ======================================================== 5. static rule: allow-passthrough is only ever set to on
# allowed: set-option -p -t <pane> allow-passthrough on, set-option -g allow-passthrough on; anything else (a bare
# set, setw, off, all, -u, a variable value) is flagged. Comments are skipped.
tmset() { grep -E '(^|[^a-z_-])(tm|tq|tmux)( -[LS] [^ ]+)? +(set-option|set|setw|set-window-option) '; }  # a tmux option write, not prose
rule() { tmset | grep 'allow-passthrough' | grep -vE 'set-option (-p -t (<[^>]*>|[^ ]+)|-g) allow-passthrough on( |$|")' | grep -vE '^[^:]*:[0-9]+: *#'; }
bad=$(cd "$ROOT" && grep -rnE 'allow-passthrough' bin lib | grep -v '^lib/fleet-show-shim/' | rule)
t "no kit code in bin/ or lib/ sets allow-passthrough other than on (-p -t <pane> or -g) (got: $bad)" test -z "$bad"
ctlbad=$(printf '%s\n' 'bin/x:2:# set-option allow-passthrough on in a comment' 'bin/x:3:  tm set-option allow-passthrough on' 'bin/x:4:  tm set -g allow-passthrough all' 'bin/x:5:  tm set-option -p -t "$p" allow-passthrough on' 'bin/x:6:  tm set-option -g allow-passthrough off' 'bin/x:7:  tm set-option -g allow-passthrough on 2>/dev/null' 'bin/x:8:  tm set-option -g allow-passthrough "$v"' | rule)
t "control: the rule flags a bare set-option, set -g all, -g off and a variable value, and passes -p -t on, -g on and comments (got: $(printf '%s' "$ctlbad" | cut -d: -f2 | tr '\n' ' '))" bash -c 'test "$(printf "%s\n" "$1" | cut -d: -f2 | tr "\n" " ")" = "3 4 6 8 "' _ "$ctlbad"
offrule() { grep -E 'allow-passthrough +(off|"?\$)|-u +allow-passthrough' | tmset | grep -vE '^[^:]*:[0-9]+: *#'; }
bad=$(cd "$ROOT" && grep -rnE 'allow-passthrough' bin lib | grep -v '^lib/fleet-show-shim/' | offrule)
t "no kit code in bin/ or lib/ writes allow-passthrough off or unsets it (got: $bad)" test -z "$bad"
ctloff=$(printf '%s\n' 'bin/x:2:  tm set-option -g allow-passthrough off' 'bin/x:3:  tm set -u allow-passthrough' 'bin/x:4:  tm set-option -g allow-passthrough "$v"' 'bin/x:5:# tm set -g allow-passthrough off' 'bin/x:6:  why="allow-passthrough $pt on"' 'bin/x:7:  tm set-option -g allow-passthrough on' | offrule)
t "control: the off rule flags -g off, -u and a variable value, and passes a comment, prose and -g on (got: $(printf '%s' "$ctloff" | cut -d: -f2 | tr '\n' ' '))" bash -c 'test "$(printf "%s\n" "$1" | cut -d: -f2 | tr "\n" " ")" = "2 3 4 "' _ "$ctloff"

# ======================================================== 6. FLEET_DESK_PASSTHROUGH: default on (v0.8.1), 0 opts out
export FLEET_CLAUDE_BIN=$HERE/stubs/claude FLEET_DIR=$T
dry() { env FLEET_STATE=$T/dry FLEET_TMUX_SOCKET=$D1 FLEET_TMUX_SESSION=dsess "$@" "$FLEET" desks up --no-proof --dry-run 2>&1; }
out=$(dry env)
t "desks --dry-run, unset (default): exactly one step, set-option -g allow-passthrough on" test "$(printf '%s\n' "$out" | grep allow-passthrough)" = "would run: tmux set-option -g allow-passthrough on"
out1=$(dry env FLEET_DESK_PASSTHROUGH=1)
t "desks --dry-run, FLEET_DESK_PASSTHROUGH=1: identical to unset" test "$out1" = "$out"
out0=$(dry env FLEET_DESK_PASSTHROUGH=0)
t "desks --dry-run, FLEET_DESK_PASSTHROUGH=0: no allow-passthrough step" hasnot "$out0" "allow-passthrough"
t "desks --dry-run, =0 is the default output minus the one passthrough line" test "$out0" = "$(printf '%s\n' "$out" | grep -v allow-passthrough)"
out=$(dry env FLEET_DESK_PASSTHROUGH=yes)
t "desks --dry-run, FLEET_DESK_PASSTHROUGH=yes (only unset or 1 turn it on): no step" hasnot "$out" "allow-passthrough"

rget() { jq -r --arg n "$2" --arg f "$3" '.[$n][$f] // empty' "$1/roster.json" 2>/dev/null; }
D1S=$T/d1; mkdir -p "$D1S"
fd1() { env FLEET_STATE=$D1S FLEET_TMUX_SOCKET=$D1 FLEET_TMUX_SESSION=dsess "$@" "$FLEET" desks up --no-proof 2>&1; }
out=$(fd1 env FLEET_DESK_PASSTHROUGH=0); rc=$?
t "desks up, =0: exit 0 and five desks" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "^OK desks up"' _ "$rc" "$out"
t "desks up, =0: no passthrough line in the output" hasnot "$out" "allow-passthrough"
t "desks up, =0: no allow-passthrough set anywhere, global still the tmux default off (got: $(anyset "$D1" | tr '\n' ' '))" test "$(anyset "$D1")" = "global=off"
out=$(fd1 env); rc=$?
t "desks rerun, unset (default): exit 0, live desks left alone, one OK global line" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "^ROUTE already live" && test "$(printf "%s\n" "$2" | grep -c "^OK allow-passthrough on (global, every fleet pane)$")" = 1' _ "$rc" "$out"
t "desks rerun, unset: global on, no window- or pane-level value (got: $(anyset "$D1" | tr '\n' ' '))" test "$(anyset "$D1")" = "global=on"
for d in ROUTE DECIDE AUTH WATCH LEARN; do
  p=$(rget "$D1S" "$d" pane)
  t "desks rerun, unset: live $d pane $p sees allow-passthrough on (inherited)" test "$(tmux -L "$D1" show -Apv -t "$p" allow-passthrough 2>/dev/null | tail -1)" = on
done
sp=$(tmux -L "$D1" list-panes -a -F '#{pane_id} #{@fleet-watch-status}' | awk '$2==1{print $1}')
t "the WATCH status pane ($sp) sees it on too" bash -c 'test -n "$1" && test "$(tmux -L "$2" show -Apv -t "$1" allow-passthrough 2>/dev/null | tail -1)" = on' _ "$sp" "$D1"
out=$(fd1 env FLEET_DESK_PASSTHROUGH=0); rc=$?
t "desks rerun, =0 on a server where it is on: exit 0, no passthrough line, global stays on (never flipped off; got: $(anyset "$D1" | tr '\n' ' '))" bash -c 'test "$1" = 0 && ! printf "%s" "$2" | grep -q allow-passthrough && test "$3" = "global=on"' _ "$rc" "$out" "$(anyset "$D1")"

D2S=$T/d2; mkdir -p "$D2S"
out=$(env FLEET_STATE=$D2S FLEET_TMUX_SOCKET=$D2 FLEET_TMUX_SESSION=dsess2 "$FLEET" desks up --no-proof 2>&1); rc=$?
t "fresh desks up, unset: exit 0, global on, nothing pane- or window-level (got: $(anyset "$D2" | tr '\n' ' '))" bash -c 'test "$1" = 0 && test "$2" = "global=on"' _ "$rc" "$(anyset "$D2")"

# fleet launch alone (a lane, no desks): the same default, on the server the lane opens on
printf 'hello\n' > "$T/lane-prompt.txt"
lan() { env FLEET_STATE=$T/$1 FLEET_TMUX_SOCKET=$2 FLEET_TMUX_SESSION=lsess "${@:3}" "$FLEET" launch --no-proof --dir "$T" L1 prompt "$T/lane-prompt.txt" 2>&1; }
out=$(lan l3 "$D3" env); rc=$?
t "fleet launch, unset: exit 0 and the fresh server's global is on (got: $(anyset "$D3" | tr '\n' ' '))" bash -c 'test "$1" = 0 && test "$2" = "global=on"' _ "$rc" "$(anyset "$D3")"
out=$(lan l4 "$D4" env FLEET_DESK_PASSTHROUGH=0); rc=$?
t "fleet launch, =0: exit 0 and the fresh server's global stays off (got: $(anyset "$D4" | tr '\n' ' '))" bash -c 'test "$1" = 0 && test "$2" = "global=off"' _ "$rc" "$(anyset "$D4")"
tmux -L "$D4" set -g allow-passthrough on
out=$(env FLEET_STATE=$T/l4 FLEET_TMUX_SOCKET=$D4 FLEET_TMUX_SESSION=lsess FLEET_DESK_PASSTHROUGH=0 "$FLEET" launch --no-proof --dir "$T" L2 prompt "$T/lane-prompt.txt" 2>&1); rc=$?
t "fleet launch, =0 on a server where it is on (live global): global stays on" bash -c 'test "$1" = 0 && test "$2" = "global=on"' _ "$rc" "$(anyset "$D4")"

if [ "$FAILS" = 0 ]; then echo "PASS test_show.sh: all assertions"; exit 0; fi
echo "FAIL test_show.sh: $FAILS failed"; exit 1
