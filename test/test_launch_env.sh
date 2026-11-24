#!/usr/bin/env bash
# Issue #4: fleet launch --env KEY=VALUE (repeatable), FLEET_STRIP_EXTRA, FLEET_PRELAUNCH_HOOK and the env proof.
# Private tmux server, scratch state, stub CLIs. Exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
HERE=$(cd "$(dirname "$0")" && pwd -P)
FLEET=$HERE/../bin/fleet
T=$(mktemp -d "${TMPDIR:-/tmp}/fleetenv.XXXXXX")
export FLEET_TMUX_SOCKET=fleetenv-$$
export FLEET_TMUX_SESSION=envsess
export FLEET_STATE=$T/state
export FLEET_CLAUDE_BIN=$HERE/stubs/claude
export FLEET_DIR=$T
export FLEET_NO_DESKS=1
export FLEET_CLAUDE_HOME=$T/claude-home
# a parent-shell-only var: the tmux server started by the first launch inherits it, so a lane gets it unless stripped
export HELPER_MARK_X=1
mkdir -p "$FLEET_STATE"
cleanup() { tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT

FAILS=0
t() { # label cmd...
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi
}
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnt() { ! printf '%s' "$1" | grep -qF -- "$2"; }
printf '%s\n' 'Do the thing.' > "$T/prompt.txt"
dry() { "$FLEET" launch --dry-run --cli claude --dir "$T" "$@" 2>&1; }
SENV=$FLEET_STATE/stub-claude.env
stubenv() { # wait for the claude stub's env dump of the latest launch
  local i; for i in $(seq 40); do [ -s "$SENV" ] && return 0; sleep 0.25; done; return 1
}
launch() { rm -f "$SENV"; "$FLEET" launch --no-proof --cli claude --dir "$T" "$@" >/dev/null 2>&1 && stubenv; }

# ---- (1) --dry-run shows the export lines; VALUE is shell-quoted
out=$(dry --env A=1 W1 prompt "$T/prompt.txt")
t "dry-run --env A=1 shows export A=1" bash -c 'printf "%s\n" "$1" | grep -qx "export A=1"' _ "$out"
NASTY='two words; $(touch /tmp/never-$$) `x` "q" '\''s'\'''
out=$(dry --env A=1 --env "B=$NASTY" W1 prompt "$T/prompt.txt")
line=$(printf '%s\n' "$out" | grep '^export B=')
t "dry-run: a VALUE with spaces, quotes and \$( ) is quoted (eval gives it back verbatim)" \
  bash -c 'eval "$1"; [ "$B" = "$2" ]' _ "$line" "$NASTY"
t "dry-run: two --env give two export lines in order" bash -c 'printf "%s\n" "$1" | grep "^export [AB]=" | head -1 | grep -qx "export A=1"' _ "$out"
out=$(dry --env A=1 --env A=2 W1 prompt "$T/prompt.txt")
t "dry-run: a repeated KEY keeps the last value, once" bash -c 'test "$(printf "%s\n" "$1" | grep -c "^export A=")" = 1 && printf "%s\n" "$1" | grep -qx "export A=2"' _ "$out"
out=$(dry --env 'E=' W1 prompt "$T/prompt.txt")
t "dry-run: an empty VALUE is allowed" bash -c 'printf "%s\n" "$1" | grep -qx "export E='"''"'"' _ "$out"
out=$(dry W1 prompt "$T/prompt.txt")
t "control: no --env, no extra export line" bash -c '! printf "%s\n" "$1" | grep -q "^export [A-Z]=" ' _ "$out"

# ---- (2) FLEET_STRIP_EXTRA is appended to the unset line, shown in --dry-run
out=$(FLEET_STRIP_EXTRA="HELPER_MARK_X OTHER_Y" dry W1 prompt "$T/prompt.txt")
t "dry-run: FLEET_STRIP_EXTRA vars end the unset line" bash -c 'printf "%s\n" "$1" | grep -q "^unset CLAUDE_CODE_CHILD_SESSION .* HELPER_MARK_X OTHER_Y$"' _ "$out"
out=$(dry W1 prompt "$T/prompt.txt")
t "control: without it the unset line has neither" bash -c '! printf "%s\n" "$1" | grep "^unset " | grep -q "HELPER_MARK_X"' _ "$out"

# ---- (3) validation
for bad in '1A=x' 'A-B=x' ' A=x' '=x'; do
  out=$(dry --env "$bad" W1 prompt "$T/prompt.txt"); rc=$?
  t "--env '$bad': FAIL not an env var name" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL --env: not an env var name"' _ "$out" "$rc"
done
out=$(dry --env NOEQ W1 prompt "$T/prompt.txt"); rc=$?
t "--env without = : FAIL want KEY=VALUE" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL --env: want KEY=VALUE"' _ "$out" "$rc"
out=$(dry --env FLEET_NAME=x W1 prompt "$T/prompt.txt"); rc=$?
t "--env FLEET_NAME=x: FAIL set by fleet launch itself" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL --env: FLEET_NAME is set by fleet launch itself"' _ "$out" "$rc"
out=$(FLEET_STRIP_EXTRA='OK_1 bad-name' dry W1 prompt "$T/prompt.txt"); rc=$?
t "FLEET_STRIP_EXTRA with a bad name: FAIL" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL FLEET_STRIP_EXTRA: not an env var name: bad-name"' _ "$out" "$rc"
t "no state written by the dry runs" test ! -e "$FLEET_STATE/roster.json"

# ---- (4) a real launch: the CLI gets the exports, the stripped var is gone, launch.json stores the set
launch E0 prompt "$T/prompt.txt"
t "control: lane E0 (no strip) sees HELPER_MARK_X=1" grep -qx 'HELPER_MARK_X=1' "$SENV"
FLEET_STRIP_EXTRA=HELPER_MARK_X launch --env A=1 --env "B=x y" E1 prompt "$T/prompt.txt"
t "E1's CLI got A=1" grep -qx 'A=1' "$SENV"
t "E1's CLI got B=x y" grep -qx 'B=x y' "$SENV"
t "E1's CLI did not get the stripped HELPER_MARK_X" bash -c '! grep -q "^HELPER_MARK_X=" "$1"' _ "$SENV"
t "launch/E1.launch.json stores env {A:1, B:x y}" jq -e '.env == {"A":"1","B":"x y"}' "$FLEET_STATE/launch/E1.launch.json"

# ---- (5) resume reapplies the stored set; an explicit --env replaces it
"$FLEET" stop E1 >/dev/null 2>&1
launch E1 resume 00000000-0000-4000-8000-0000000000e1
t "resume with no --env reapplies A=1" grep -qx 'A=1' "$SENV"
t "resume with no --env reapplies B=x y" grep -qx 'B=x y' "$SENV"
t "resume keeps the stored set in launch.json" jq -e '.env == {"A":"1","B":"x y"}' "$FLEET_STATE/launch/E1.launch.json"
"$FLEET" stop E1 >/dev/null 2>&1
launch --env A=2 E1 resume 00000000-0000-4000-8000-0000000000e1
t "resume with --env A=2 uses it" grep -qx 'A=2' "$SENV"
t "resume with an explicit --env set replaces the stored one" bash -c '! grep -q "^B=" "$1"' _ "$SENV"
"$FLEET" stop E1 >/dev/null 2>&1

# ---- (6) FLEET_PRELAUNCH_HOOK: prints KEY=VALUE lines; nonzero refuses; --env wins on a clash
out=$(FLEET_PRELAUNCH_HOOK='printf "# c\n\nH_NAME=%s\nH_MODE=%s\nA=hook\n" "$1" "$2"' dry --env A=cli W1 prompt "$T/prompt.txt")
t "hook: its KEY=VALUE lines are exported (args name, mode)" bash -c 'printf "%s\n" "$1" | grep -qx "export H_NAME=W1" && printf "%s\n" "$1" | grep -qx "export H_MODE=prompt"' _ "$out"
t "hook: --env wins over the hook on the same KEY" bash -c 'printf "%s\n" "$1" | grep -qx "export A=cli" && ! printf "%s\n" "$1" | grep -q "^export A=hook"' _ "$out"
out=$(FLEET_PRELAUNCH_HOOK='echo no >&2; exit 3' dry W1 prompt "$T/prompt.txt"); rc=$?
t "hook exit 3: launch refused with FAIL" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL FLEET_PRELAUNCH_HOOK refused the launch of W1 (exit 3)"' _ "$out" "$rc"
out=$(FLEET_PRELAUNCH_HOOK='echo "bad line"' dry W1 prompt "$T/prompt.txt"); rc=$?
t "hook printing a non KEY=VALUE line: FAIL" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL FLEET_PRELAUNCH_HOOK: want KEY=VALUE"' _ "$out" "$rc"

printf '#!/usr/bin/env bash\nprintf "HP_ARGS=%%s|%%s|%%s\\n" "$1" "$2" "$3"\n' > "$T/hook.sh"; chmod +x "$T/hook.sh"
out=$(FLEET_PRELAUNCH_HOOK=$T/hook.sh dry W1 prompt "$T/prompt.txt")
t "hook as an executable path gets NAME MODE ARG" bash -c 'printf "%s\n" "$1" | grep -qxF "export HP_ARGS=W1\\|prompt\\|$2"' _ "$out" "$T/prompt.txt" \
  || printf '    launcher said: %s\n' "$(printf '%s\n' "$out" | grep HP_)"

# ---- (7) env proof: a key that never reaches the CLI process fails the launch, naming it
pr() { FLEET_CLAUDE_BIN=$HERE/stubs/claude-dialog FLEET_PROOF_TIMEOUT=4 "$FLEET" launch --cli claude --dir "$T" "$@" 2>&1; }
out=$(pr --env DROPME_X=1 DENVDROP prompt "$T/prompt.txt"); rc=$?
t "env proof: a dropped --env key FAILs naming env:DROPME_X" bash -c 'test "$2" = 1 && printf "%s" "$1" | grep -q "^FAIL .*env check failed:.* env:DROPME_X"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
out=$(pr --env KEEP_X=1 DPLAIN prompt "$T/prompt.txt")
t "control: a key that arrives passes the env proof (fails later, on the transcript)" bash -c 'printf "%s" "$1" | grep -q "transcript never appeared" && ! printf "%s" "$1" | grep -q "env:KEEP_X"' _ "$out" \
  || printf '    launcher said: %s\n' "$out"

[ "$FAILS" = 0 ] && echo "ALL PASS" || { echo "$FAILS FAILED"; exit 1; }
