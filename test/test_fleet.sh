#!/usr/bin/env bash
# Tests for bin/fleet. Private tmux server, temp state, stub CLIs. Exit 1 on any FAIL.
set -u
HERE=$(cd "$(dirname "$0")" && pwd -P)
FLEET=$HERE/../bin/fleet
T=$(mktemp -d "${TMPDIR:-/tmp}/fleettest.XXXXXX")
export FLEET_TMUX_SOCKET=fleettest-$$
export FLEET_TMUX_SESSION=fleetsess
export FLEET_STATE=$T/state
export FLEET_CLAUDE_BIN=$HERE/stubs/claude
export FLEET_CODEX_BIN=$HERE/stubs/codex
export FLEET_OPENCODE_BIN=$HERE/stubs/opencode
export FLEET_DIR=$T
mkdir -p "$FLEET_STATE"
cleanup() { tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
tm() { tmux -L "$FLEET_TMUX_SOCKET" "$@"; }

FAILS=0
t() { # label cmd...
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi
}
has() { printf '%s' "$1" | grep -qF -- "$2"; }

# ---- control: the harness really reports a failing check
ctl=$(FAILS=0; t "inner" false; echo "inner_fails=$FAILS")
t "control: a failing check prints FAIL and counts it" bash -c '
  printf "%s" "$1" | grep -q "^FAIL inner" && printf "%s" "$1" | grep -q "inner_fails=1"' _ "$ctl"

GOOD='/goal Work from goal.json; the file wins over memory, then stop after 5 turns.'
printf '%s\n' "$GOOD" > "$T/good.txt"
printf '%s\n' 'Do the thing.' > "$T/prompt.txt"

# ---- (1) dry-run flags per cli x mode
dry() { "$FLEET" launch --dry-run --cli "$@" 2>&1; }
for cli in claude codex opencode; do
  for mode in goal prompt desk resume; do
    case $mode in
      goal) a=$T/good.txt ;; prompt) a=$T/prompt.txt ;; desk) a= ;;
      resume) if [ $cli = claude ]; then a=11111111-2222-3333-4444-555555555555; else a=ses_abc123; fi ;;
    esac
    out=$(dry $cli --dir "$T" N1 $mode $a); rc=$?
    t "dry-run $cli/$mode exits 0" test $rc -eq 0
    case $cli in
      claude)
        t "dry-run claude/$mode has --dangerously-skip-permissions" has "$out" '--dangerously-skip-permissions'
        t "dry-run claude/$mode has --disallowed-tools=AskUserQuestion" has "$out" '--disallowed-tools=AskUserQuestion'
        t "dry-run claude/$mode has -n N1" has "$out" '-n N1' ;;
      codex)
        t "dry-run codex/$mode has --dangerously-bypass-approvals-and-sandbox" has "$out" '--dangerously-bypass-approvals-and-sandbox' ;;
      opencode)
        t "dry-run opencode/$mode has --port" has "$out" '--port'
        if [ $mode = resume ]; then t "dry-run opencode/resume has -s" has "$out" ' -s ses_abc123'
        else t "dry-run opencode/$mode has --prompt" has "$out" '--prompt'; fi ;;
    esac
  done
done
t "dry-run desk default text names the desk skill" has "$(dry claude --dir "$T" D1 desk)" 'standing decisions desk'
t "dry-run touches no state" test ! -e "$FLEET_STATE/roster.json" -a ! -d "$FLEET_STATE/launch"
t "dry-run starts no tmux server" bash -c '! tmux -L "$1" has-session 2>/dev/null' _ "$FLEET_TMUX_SOCKET"

# ---- (2) lint
rej() { # label expected-substring args...  (must exit 1 and print FAIL ... substring)
  local label=$1 want=$2; shift 2
  local out rc
  out=$("$FLEET" launch --dry-run "$@" 2>&1); rc=$?
  t "$label: exit 1" test $rc -eq 1
  t "$label: FAIL names '$want'" has "$out" "FAIL"
  t "$label: reason mentions '$want'" has "$out" "$want"
}
printf '%s\n' 'Work from goal.json; the file wins. Stop after 5 turns.' > "$T/c1.txt"
printf '%s\n' '/goal Work from goal.json. Stop after 5 turns.' > "$T/c2.txt"
printf '%s\n' '/goal Work from goal.json; the file wins.' > "$T/c3.txt"
{ printf '%s' '/goal goal.json file wins stop after 5 '; head -c 4100 /dev/zero | tr '\0' x; printf '\n'; } > "$T/c4.txt"
printf '%s\n' '/goal Work from notes; the file wins. Stop after 5 turns.' > "$T/c5.txt"
rej "lint: missing '/goal '" "/goal " L1 goal "$T/c1.txt"
rej "lint: missing 'file wins'" "file wins" L2 goal "$T/c2.txt"
rej "lint: missing 'stop after'" "stop after" L3 goal "$T/c3.txt"
rej "lint: over 4000 chars" "max 4000" L4 goal "$T/c4.txt"
rej "lint: no *.json named" ".json" L5 goal "$T/c5.txt"
rej "lint: goal file absent" "condition file" L6 goal "$T/nope.txt"
rej "desk rejects /goal text" "/goal" L7 desk "/goal do it"
rej "name with a space rejected" "name must match" "bad name" prompt hi
rej "name with a slash rejected" "name must match" a/b prompt hi
t "lint control: a valid condition is accepted" "$FLEET" launch --dry-run L8 goal "$T/good.txt"

# ---- (3) real launch of a stub
export CLAUDECODE=1 CLAUDE_CODE_CHILD_SESSION=1
# control: a plain tmux window of this server does inherit the leaking var
tm new-session -d -s ctl -n ctl "env > $T/leak.env; sleep 30"
for i in 1 2 3 4 5 6 7 8 9 10; do [ -s "$T/leak.env" ] && break; sleep 0.5; done
t "control: unstripped tmux window inherits CLAUDECODE=1" grep -q '^CLAUDECODE=1$' "$T/leak.env"

rm -f "$FLEET_STATE/stub-claude.argv" "$FLEET_STATE/stub-claude.env"
out=$("$FLEET" launch --no-proof --dir "$T" --model opus A prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch A exits 0" test $rc -eq 0
t "launch A prints one OK line" bash -c 'test "$(printf "%s\n" "$1" | grep -c "^OK A cli=claude mode=prompt pane=%")" = 1 && test "$(printf "%s\n" "$1" | wc -l | tr -d " ")" = 1' _ "$out"
t "session $FLEET_TMUX_SESSION exists" tm has-session -t "=$FLEET_TMUX_SESSION"
t "window named A exists in session" bash -c 'tmux -L "$1" list-windows -t "$2" -F "#{window_name}" | grep -qx A' _ "$FLEET_TMUX_SOCKET" "$FLEET_TMUX_SESSION"
t "roster.json has A cli=claude" bash -c 'test "$(jq -r .A.cli "$1")" = claude' _ "$FLEET_STATE/roster.json"
t "roster.json has A pane %N" bash -c 'jq -r .A.pane "$1" | grep -Eq "^%[0-9]+$"' _ "$FLEET_STATE/roster.json"
t "roster pane for A is a live pane" bash -c 'tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$(jq -r .A.pane "$2")"' _ "$FLEET_TMUX_SOCKET" "$FLEET_STATE/roster.json"
t "roster A session_id is a uuid" bash -c 'jq -r .A.session_id "$1" | grep -Eq "^[0-9a-f-]{36}$"' _ "$FLEET_STATE/roster.json"
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$FLEET_STATE/stub-claude.env" ] && break; sleep 0.5; done
argv=$(cat "$FLEET_STATE/stub-claude.argv" 2>/dev/null)
t "stub argv has --dangerously-skip-permissions" has "$argv" '--dangerously-skip-permissions'
t "stub argv has --disallowed-tools=AskUserQuestion" has "$argv" '--disallowed-tools=AskUserQuestion'
t "stub argv has -n then A" bash -c 'printf "%s\n" "$1" | grep -A1 -x -e "-n" | grep -qx A' _ "$argv"
t "stub argv has the prompt text" has "$argv" 'Do the thing.'
t "stub argv has --session-id" has "$argv" '--session-id'
t "window env has FLEET_NAME=A" grep -q '^FLEET_NAME=A$' "$FLEET_STATE/stub-claude.env"
t "window env has the skip-permissions var" grep -q '^CLAUDE_CODE_EVEN_MORE_DANGEROUSLY_SKIP_PERMISSIONS=1$' "$FLEET_STATE/stub-claude.env"
t "window env lacks CLAUDECODE (launcher exported CLAUDECODE=1)" bash -c '! grep -q "^CLAUDECODE=" "$1"' _ "$FLEET_STATE/stub-claude.env"
t "window env lacks CLAUDE_CODE_CHILD_SESSION" bash -c '! grep -q "^CLAUDE_CODE_CHILD_SESSION=" "$1"' _ "$FLEET_STATE/stub-claude.env"
t "log has a started line for A" grep -q ' A started cli=claude' "$FLEET_STATE/log/fleet.log"

# ---- (4) split right
rm -f "$FLEET_STATE/stub-codex.argv"
out=$("$FLEET" launch --no-proof --cli codex --split-right-of A --dir "$T" B prompt "$T/prompt.txt" 2>&1); rc=$?
t "launch B (codex, split) exits 0" test $rc -eq 0
t "A's window now has 2 panes" test "$(tm list-panes -t "$FLEET_TMUX_SESSION:A" -F x | wc -l | tr -d ' ')" = 2
t "B's pane is inside A's window" bash -c 'tmux -L "$1" list-panes -t "$2:A" -F "#{pane_id}" | grep -qx "$(jq -r .B.pane "$3")"' _ "$FLEET_TMUX_SOCKET" "$FLEET_TMUX_SESSION" "$FLEET_STATE/roster.json"
t "roster B cli=codex" bash -c 'test "$(jq -r .B.cli "$1")" = codex' _ "$FLEET_STATE/roster.json"
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$FLEET_STATE/stub-codex.argv" ] && break; sleep 0.5; done
t "codex stub argv has --dangerously-bypass-approvals-and-sandbox" grep -qx -e '--dangerously-bypass-approvals-and-sandbox' "$FLEET_STATE/stub-codex.argv"
t "split onto a missing lane is refused" bash -c '! "$1" launch --no-proof --split-right-of NOPE X prompt hi 2>/dev/null' _ "$FLEET"

# opencode window
rm -f "$FLEET_STATE/stub-opencode.argv"
t "launch C (opencode) exits 0" "$FLEET" launch --no-proof --cli opencode --dir "$T" C prompt "$T/prompt.txt"
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$FLEET_STATE/stub-opencode.argv" ] && break; sleep 0.5; done
t "opencode stub argv has --port" grep -qx -e '--port' "$FLEET_STATE/stub-opencode.argv"
t "opencode stub argv has --prompt" grep -qx -e '--prompt' "$FLEET_STATE/stub-opencode.argv"
t "roster C has an integer port" bash -c 'jq -e ".C.port | type == \"number\"" "$1"' _ "$FLEET_STATE/roster.json"

# ---- ls / layout honour the private socket
t "fleet ls shows A alive" bash -c '"$1" ls | grep -q "^A .*alive"' _ "$FLEET"
t "fleet layout lists window=A" bash -c '"$1" layout | grep -q "window=A"' _ "$FLEET"

# ---- (5) duplicate refused
before=$(tm list-panes -a -F '#{pane_id}' | wc -l | tr -d ' ')
out=$("$FLEET" launch --no-proof --dir "$T" A prompt "$T/prompt.txt" 2>&1); rc=$?
t "second live A is refused (exit 1)" test $rc -eq 1
t "refusal says FAIL and already live" bash -c 'printf "%s" "$1" | grep -q "^FAIL .*already live"' _ "$out"
t "refusal opened no pane" test "$(tm list-panes -a -F '#{pane_id}' | wc -l | tr -d ' ')" = "$before"

# ---- (6) stop
paneB=$(jq -r .B.pane "$FLEET_STATE/roster.json")
t "fleet stop B succeeds" "$FLEET" stop B
t "B's pane is gone" bash -c '! tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$paneB"
t "fleet ls shows B dead" bash -c '"$1" ls | grep -q "^B .*dead"' _ "$FLEET"
t "fleet stop of an unknown lane fails" bash -c '! "$1" stop NOPE 2>/dev/null' _ "$FLEET"
t "fleet stop A succeeds" "$FLEET" stop A
t "a stopped name can launch again" "$FLEET" launch --no-proof --dir "$T" A prompt "$T/prompt.txt"

# ---- (7) startup dialogs fail loud (proof path, dialog stub, no transcript ever appears)
chmod +x "$HERE/stubs/claude-dialog"
dlg() { FLEET_CLAUDE_BIN=$HERE/stubs/claude-dialog FLEET_PROOF_TIMEOUT=10 FLEET_RESUME_SETTLE=3 "$FLEET" launch --dir "$T" "$@" 2>&1; }
out=$(dlg DTRUST prompt "$T/prompt.txt"); rc=$?
t "trust dialog: launch exits 1" test $rc -eq 1
t "trust dialog: FAIL names the workspace-trust dialog" bash -c 'printf "%s" "$1" | grep -q "^FAIL .*workspace-trust dialog"' _ "$out" \
  || printf '    launcher said: %s\n' "$out"   # seen once in ~30 runs, not reproduced; keep the evidence
out=$(dlg DBYPASS prompt "$T/prompt.txt"); rc=$?
t "bypass dialog: launch exits 1" test $rc -eq 1
t "bypass dialog: FAIL names the Bypass Permissions dialog" bash -c 'printf "%s" "$1" | grep -q "^FAIL .*Bypass Permissions accept dialog"' _ "$out"
out=$(dlg DRBYPASS resume 00000000-0000-4000-8000-000000000001); rc=$?
t "bypass dialog on resume: launch exits 1" test $rc -eq 1
t "bypass dialog on resume: FAIL names the dialog" bash -c 'printf "%s" "$1" | grep -q "^FAIL .*Bypass Permissions accept dialog"' _ "$out"
out=$(dlg DNOENV prompt "$T/prompt.txt"); rc=$?
t "env check: a process really missing the skip var still fails after the retries" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "env check failed: CLAUDE_CODE_EVEN_MORE_DANGEROUSLY_SKIP_PERMISSIONS"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
out=$(dlg DPLAIN prompt "$T/prompt.txt"); rc=$?
t "control: no dialog still fails (no transcript) but names neither dialog" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "transcript never appeared" && ! printf "%s" "$1" | grep -q "dialog"' _ "$out" "$rc"
out=$(dlg DRPLAIN resume 00000000-0000-4000-8000-000000000002); rc=$?
t "control: resume with no dialog launches OK" bash -c 'test "$2" -eq 0 && printf "%s" "$1" | grep -q "^OK DRPLAIN"' _ "$out" "$rc"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILS FAILED"; exit 1; fi
