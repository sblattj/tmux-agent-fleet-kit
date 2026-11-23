#!/usr/bin/env bash
# Tests for bin/fleet. Private tmux server, temp state, stub CLIs. Exit 1 on any FAIL.
set -u
# Run inside a fleet lane too: drop the lane's own FLEET_* env (FLEET_ROLE=worker trips the role guard,
# FLEET_STATE/FLEET_NAME point at the live fleet) before setting the test's own.
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
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
        t "dry-run claude/$mode has -n N1" has "$out" '-n N1'
        # resume is what fleet reopen runs: a reopened lane (even one first started outside fleet) gets peer_inbox
        t "dry-run claude/$mode passes the fleet MCP (--mcp-config with fleet_mesh.py)" bash -c 'printf "%s" "$1" | grep -q -- "--mcp-config .*fleet_mesh.py"' _ "$out" ;;
      codex)
        t "dry-run codex/$mode has --dangerously-bypass-approvals-and-sandbox" has "$out" '--dangerously-bypass-approvals-and-sandbox'
        t "control: dry-run codex/$mode passes no --mcp-config (codex gets the mesh from its own config)" bash -c '! printf "%s" "$1" | grep -q -- "--mcp-config"' _ "$out" ;;
      opencode)
        t "dry-run opencode/$mode has --port" has "$out" '--port'
        if [ $mode = resume ]; then t "dry-run opencode/resume has -s" has "$out" ' -s ses_abc123'
        else t "dry-run opencode/$mode has --prompt" has "$out" '--prompt'; fi ;;
    esac
  done
done
t "dry-run desk default text names the desk skill" has "$(dry claude --dir "$T" D1 desk)" 'standing decisions desk'
# ---- (1b) worker tool block (context-diet trim 1): opt-in keep via --keep-tools or the goal's keepTools
printf '{"goal":"x"}' > "$T/g0.json"
printf '{"goal":"x","keepTools":["Workflow"]}' > "$T/gw.json"
printf '/goal Work the living goal file %s; the file wins, or stop after 5 turns.\n' "$T/g0.json" > "$T/c0.txt"
printf '/goal Work the living goal file %s; the file wins, or stop after 5 turns.\n' "$T/gw.json" > "$T/cw.txt"
t "worker goal lane blocks Workflow, ScheduleWakeup, ReportFindings" has "$(dry claude --dir "$T" W1 goal "$T/c0.txt")" '--disallowed-tools=AskUserQuestion,Workflow,ScheduleWakeup,ReportFindings '
t "goal keepTools [Workflow] keeps Workflow" has "$(dry claude --dir "$T" W2 goal "$T/cw.txt")" '--disallowed-tools=AskUserQuestion,ScheduleWakeup,ReportFindings '
t "--keep-tools adds to the goal's keepTools" has "$(dry claude --keep-tools ReportFindings --dir "$T" W3 goal "$T/cw.txt")" '--disallowed-tools=AskUserQuestion,ScheduleWakeup '
t "--keep-tools all restores the old deny list" has "$(dry claude --keep-tools all --dir "$T" W4 prompt "$T/prompt.txt")" '--disallowed-tools=AskUserQuestion '
t "route role keeps every tool" has "$(dry claude --role route --dir "$T" W5 goal "$T/c0.txt")" '--disallowed-tools=AskUserQuestion '
t "desk role keeps every tool" has "$(dry claude --dir "$T" W6 desk)" '--disallowed-tools=AskUserQuestion '
out=$(dry claude --keep-tools Bogus --dir "$T" W7 prompt "$T/prompt.txt"); rc=$?
t "--keep-tools with an unknown tool fails loud" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "^FAIL"' _ "$rc" "$out"
t "codex lane is untouched by the worker block" bash -c '! printf "%s" "$1" | grep -q Workflow' _ "$(dry codex --dir "$T" W8 goal "$T/c0.txt")"
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

# ---- (8) kiro: scratch KIRO_HOME, python stub that writes leases/sessions like kiro-cli 3.5 (test/stubs/kiro)
export KIRO_HOME=$T/kh FLEET_KIRO_BIN=$HERE/stubs/kiro
mkdir -p "$KIRO_HOME"
chmod +x "$HERE/stubs/kiro"
RT=$(cd "$T" && pwd -P)
KSID=sess_11111111-2222-4333-8444-555555555555
out=$(dry kiro --dir "$T" K0 prompt "$T/prompt.txt"); rc=$?
t "kiro dry-run exits 0" test $rc -eq 0
t "kiro dry-run has chat --agent-engine v3 --trust-all-tools --trust-workspace" has "$out" 'chat --agent-engine v3 --trust-all-tools --trust-workspace'
t "kiro dry-run reads the prompt file" has "$out" '"$(cat '
t "kiro dry-run without the agent file warns" has "$out" 'warn: kiro agent fleet-worker not installed'
t "kiro dry-run without the agent file has no --agent" bash -c '! printf "%s" "$1" | grep -q -- "--agent fleet-"' _ "$out"
t "kiro window script exports KIRO_HOME" has "$out" "KIRO_HOME=$KIRO_HOME"
t "kiro window script passes FLEET_KIRO_BIN" has "$out" "FLEET_KIRO_BIN=$FLEET_KIRO_BIN"
mkdir -p "$KIRO_HOME/agents"; printf '{"name":"fleet-worker"}\n' > "$KIRO_HOME/agents/fleet-worker.json"
out=$(dry kiro --dir "$T" --model m1 K0 prompt "$T/prompt.txt")
t "kiro dry-run with the agent file has --agent fleet-worker" has "$out" '--trust-workspace --agent fleet-worker --model m1 "$(cat '
t "kiro dry-run with the agent file does not warn" bash -c '! printf "%s" "$1" | grep -q "^warn:"' _ "$out"
out=$(dry kiro --dir "$T" K0 goal "$T/good.txt")
t "kiro goal prompt is rewritten to /goal --max 20" bash -c 'printf "%s\n" "$1" | grep -qx "/goal --max 20 Work from goal.json; the file wins over memory, then stop after 5 turns."' _ "$out"
out=$(FLEET_KIRO_GOAL_MAX=7 "$FLEET" launch --dry-run --cli kiro --dir "$T" K0 goal "$T/good.txt" 2>&1)
t "FLEET_KIRO_GOAL_MAX=7 gives /goal --max 7" has "$out" '/goal --max 7 Work from goal.json'
out=$(FLEET_KIRO_GOAL_MAX=0 "$FLEET" launch --dry-run --cli kiro --dir "$T" K0 goal "$T/good.txt" 2>&1); rc=$?
t "FLEET_KIRO_GOAL_MAX=0 is refused" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "^FAIL FLEET_KIRO_GOAL_MAX must be a positive integer"' _ "$out" "$rc"
out=$(dry claude --dir "$T" K0 goal "$T/good.txt")
t "control: claude goal prompt is not rewritten (no --max)" bash -c 'printf "%s\n" "$1" | grep -qx "/goal Work from goal.json; the file wins over memory, then stop after 5 turns." && ! printf "%s" "$1" | grep -q -- "--max"' _ "$out"
{ printf '%s' '/goal goal.json file wins stop after 5 '; head -c 3955 /dev/zero | tr '\0' x; printf '\n'; } > "$T/c6.txt"
t "control: a 3994-char condition passes the 4000 lint for claude" "$FLEET" launch --dry-run L9 goal "$T/c6.txt"
rej "kiro: the 4000 lint counts the --max rewrite" "max 4000" --cli kiro L9 goal "$T/c6.txt"
out=$(dry kiro --dir "$T" K0 resume "$KSID")
t "kiro dry-run resume has --resume-id SID" has "$out" "--trust-workspace --agent fleet-worker --resume-id $KSID"
t "kiro dry-run resume sends no prompt file" bash -c '! printf "%s" "$1" | grep -q "## prompt"' _ "$out"
rej "kiro resume rejects a non-sess_ id" "sess_<uuid>" --cli kiro K0 resume ses_abc123

# an unrelated live process's lease on the same workspace: no kiro proof may pick it
USID=sess_99999999-0000-4000-8000-000000000000
mkdir -p "$KIRO_HOME/session-leases" "$KIRO_HOME/sessions/unrelated/$USID"
jq -n --argjson p $$ '{v:1,pid:$p,parentPid:1,clientName:"kiro-cli"}' > "$KIRO_HOME/session-leases/$USID.lease"
jq -n --arg s "$USID" --arg d "$RT" '{id:$s,agentMode:"main",workspacePaths:[$d]}' > "$KIRO_HOME/sessions/unrelated/$USID/session.json"
printf '%s\n' '{"payload":{"type":"user","content":"unrelated"}}' > "$KIRO_HOME/sessions/unrelated/$USID/messages.jsonl"
R=$FLEET_STATE/roster.json

rm -f "$FLEET_STATE/stub-kiro.argv" "$FLEET_STATE/stub-kiro.env"
out=$("$FLEET" launch --cli kiro --dir "$T" K1 prompt "$T/prompt.txt" 2>&1); rc=$?
t "kiro launch K1 (proof on) exits 0" test $rc -eq 0 || printf '    launcher said: %s\n' "$out"
t "kiro K1 prints one OK line with session=sess_<uuid>" bash -c 'printf "%s\n" "$1" | grep -Eqx "OK K1 cli=kiro mode=prompt pane=%[0-9]+ session=sess_[0-9a-f-]{36} goal=n/a"' _ "$out"
k1sid=$(jq -r '.K1.session_id // empty' "$R")
t "roster K1 session_id is not the unrelated lease" test -n "$k1sid" -a "$k1sid" != "$USID"
t "roster K1 session_id names a live lease" test -f "$KIRO_HOME/session-leases/$k1sid.lease"
k1lp=$(jq -r .pid "$KIRO_HOME/session-leases/$k1sid.lease" 2>/dev/null)
k1pp=$(jq -r .K1.pid "$R")
t "K1 lease pid is a grandchild of the pane pid (not its child)" bash -c 'p=$(ps -o ppid= -p "$1" | tr -d " "); test "$p" != "$2" && test "$(ps -o ppid= -p "$p" | tr -d " ")" = "$2"' _ "$k1lp" "$k1pp"
argv=$(cat "$FLEET_STATE/stub-kiro.argv" 2>/dev/null)
t "kiro stub argv starts with chat" bash -c 'test "$(printf "%s\n" "$1" | head -1)" = chat' _ "$argv"
t "kiro stub argv has --trust-all-tools" bash -c 'printf "%s\n" "$1" | grep -qx -e --trust-all-tools' _ "$argv"
t "kiro stub argv has --trust-workspace" bash -c 'printf "%s\n" "$1" | grep -qx -e --trust-workspace' _ "$argv"
t "kiro stub argv has --agent then fleet-worker" bash -c 'printf "%s\n" "$1" | grep -A1 -x -e --agent | grep -qx fleet-worker' _ "$argv"
t "kiro stub argv has the prompt text" has "$argv" 'Do the thing.'
t "kiro window env has KIRO_HOME" grep -qx "KIRO_HOME=$KIRO_HOME" "$FLEET_STATE/stub-kiro.env"
before=$(tm list-panes -a -F '#{pane_id}' | wc -l | tr -d ' ')
out=$("$FLEET" launch --cli kiro --dir "$T" K1 prompt "$T/prompt.txt" 2>&1); rc=$?
t "second live kiro K1 is refused" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "^FAIL .*already live"' _ "$out" "$rc"
t "kiro refusal opened no pane" test "$(tm list-panes -a -F '#{pane_id}' | wc -l | tr -d ' ')" = "$before"

out=$("$FLEET" launch --cli kiro --dir "$T" K2 goal "$T/good.txt" 2>&1); rc=$?
t "kiro goal K2 exits 0 with goal=active" bash -c 'test "$2" -eq 0 && printf "%s\n" "$1" | grep -Eqx "OK K2 cli=kiro mode=goal pane=%[0-9]+ session=sess_[0-9a-f-]{36} goal=active"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
k2sid=$(jq -r '.K2.session_id // empty' "$R")
t "kiro stub got /goal --max 20" has "$(cat "$FLEET_STATE/stub-kiro.argv" 2>/dev/null)" '/goal --max 20 Work from goal.json'
t "K2 has a live goal child lease that sorts first (rootConversationId = K2)" bash -c '
  for f in "$1"/session-leases/sess_00000000-*.lease; do
    s=$(basename "$f" .lease); jq -e --arg r "$2" ".rootConversationId == \$r" "$1"/sessions/*/"$s"/session.json >/dev/null && exit 0
  done; exit 1' _ "$KIRO_HOME" "$k2sid"
t "roster K2 session_id is the parent session (no rootConversationId)" bash -c 'jq -e ".rootConversationId == null" "$1"/sessions/*/"$2"/session.json' _ "$KIRO_HOME" "$k2sid"

out=$("$FLEET" launch --cli kiro --dir "$T" K3 resume "$KSID" 2>&1); rc=$?
t "kiro resume K3 prints OK session=SID" bash -c 'test "$2" -eq 0 && printf "%s\n" "$1" | grep -Eqx "OK K3 cli=kiro mode=resume pane=%[0-9]+ session=$3 goal=n/a"' _ "$out" "$rc" "$KSID" \
  || printf '    launcher said: %s\n' "$out"
t "kiro stub argv has --resume-id then SID" bash -c 'grep -A1 -x -e --resume-id "$1" | grep -qx "$2"' _ "$FLEET_STATE/stub-kiro.argv" "$KSID"

nlease=$(ls "$KIRO_HOME/session-leases" | wc -l | tr -d ' ')
out=$(FLEET_PROOF_TIMEOUT=10 "$FLEET" launch --cli kiro --dir "$T" KTRUSTALL prompt "$T/prompt.txt" 2>&1); rc=$?
t "kiro trust-all warning: launch exits 1 with FAIL naming the confirmation" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "^FAIL Kiro trust-all confirmation in pane %[0-9]*; run install/kiro.sh (sets chat.disableTrustAllConfirmation) then fleet stop KTRUSTALL and relaunch"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
t "kiro trust-all stub created no lease" test "$(ls "$KIRO_HOME/session-leases" | wc -l | tr -d ' ')" = "$nlease"

# CONTROL: only an unrelated pid's lease and this pane's goal-child lease exist; the proof must pick neither
out=$(FLEET_PROOF_TIMEOUT=5 "$FLEET" launch --cli kiro --dir "$T" KCHILDONLY prompt "$T/prompt.txt" 2>&1); rc=$?
t "control: child-only + unrelated lease: launch fails 'no kiro session'" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "^FAIL no kiro session for KCHILDONLY"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
t "control: roster KCHILDONLY has no session_id" bash -c 'test "$(jq -r ".KCHILDONLY.session_id" "$1")" = null' _ "$R"
t "control setup was real: the unrelated lease is still there" test -f "$KIRO_HOME/session-leases/$USID.lease"
t "control setup was real: a child lease held by a KCHILDONLY pane descendant exists" bash -c '
  pp=$(jq -r .KCHILDONLY.pid "$2")
  for f in "$1"/session-leases/sess_00000000-*.lease; do
    p=$(jq -r .pid "$f"); while [ -n "$p" ] && [ "$p" -gt 1 ]; do [ "$p" = "$pp" ] && exit 0; p=$(ps -o ppid= -p "$p" | tr -d " "); done
  done; exit 1' _ "$KIRO_HOME" "$R"

t "fleet stop K1 succeeds" "$FLEET" stop K1
for i in 1 2 3 4 5 6 7 8 9 10; do [ -e "$KIRO_HOME/session-leases/$k1sid.lease" ] || break; sleep 0.5; done
t "stopping K1 removes its lease (stub cleanup like real Kiro)" test ! -e "$KIRO_HOME/session-leases/$k1sid.lease"

# ---- (9) agy: scratch FLEET_AGY_HOME, python stub that writes the log and brain transcript like agy 1.5.13 (test/stubs/agy)
export FLEET_AGY_HOME=$T/ah FLEET_AGY_BIN=$HERE/stubs/agy
mkdir -p "$FLEET_AGY_HOME"
chmod +x "$HERE/stubs/agy"
ASID=00000000-0000-4000-8000-0000000000a1
out=$(dry agy --dir "$T" A0 prompt "$T/prompt.txt"); rc=$?
t "agy dry-run exits 0" test $rc -eq 0
t "agy dry-run has --dangerously-skip-permissions --settings <lane json> --log-file" bash -c 'printf "%s" "$1" | grep -Eq -- "--dangerously-skip-permissions --settings [^ ]*/launch/A0\.agy-settings\.json --log-file [^ ]*/log/A0\.agy\.log"' _ "$out"
t "agy dry-run prompt mode passes -i with the prompt file" has "$out" '-i "$(cat '
t "agy dry-run without the agent file warns" has "$out" 'warn: agy agent fleet-worker not installed'
t "agy dry-run without the agent file has no --agent" bash -c '! printf "%s" "$1" | grep -q -- "--agent fleet-"' _ "$out"
t "agy dry-run prints the lane settings: always-proceed, the dir trusted, worker deny rules" bash -c '
  printf "%s\n" "$1" | sed -n "/^## settings /,/^}/p" | sed 1d | jq -e --arg d "$2" ".toolPermission==\"always-proceed\" and .artifactReviewPolicy==\"always-proceed\" and (.trustedWorkspaces|index(\$d)!=null) and (.permissions.deny|index(\"command(fleet launch)\")!=null)"' _ "$out" "$(cd "$T" && pwd -P)"
t "agy window script exports FLEET_AGY_HOME and FLEET_AGY_BIN" bash -c 'printf "%s" "$1" | grep -qF "FLEET_AGY_HOME=$2" && printf "%s" "$1" | grep -qF "FLEET_AGY_BIN=$3"' _ "$out" "$FLEET_AGY_HOME" "$FLEET_AGY_BIN"
t "agy window script truncates the lane log before agy starts" bash -c 'printf "%s" "$1" | grep -Eq "^: > [^ ]*/log/A0\.agy\.log$"' _ "$out"
mkdir -p "$FLEET_AGY_HOME/config/agents"; printf -- '---\nname: fleet-worker\n---\nx\n' > "$FLEET_AGY_HOME/config/agents/fleet-worker.md"
out=$(dry agy --dir "$T" --model m1 A0 prompt "$T/prompt.txt")
t "agy dry-run with the agent file has --agent fleet-worker --model m1" has "$out" '--agent fleet-worker --model m1 -i "$(cat '
t "agy dry-run with the agent file does not warn" bash -c '! printf "%s" "$1" | grep -q "^warn:"' _ "$out"
out=$(dry agy --dir "$T" A0 goal "$T/good.txt")
t "agy goal mode passes no -i (agy would send /goal as plain text)" bash -c '! printf "%s" "$1" | grep -q -- " -i "' _ "$out"
t "agy goal mode types the /goal line after the idle prompt, pause, then Enter" bash -c 'printf "%s" "$1" | grep -qF "? for shortcuts" && printf "%s" "$1" | grep -q "send-keys -t \"\$TMUX_PANE\" -l" && printf "%s" "$1" | grep -q "^agy_type &$"' _ "$out"
out=$(dry agy --dir "$T" A0 prompt "$T/prompt.txt")
t "control: agy prompt mode has no /goal typer" bash -c '! printf "%s" "$1" | grep -q agy_type' _ "$out"
out=$(dry agy --dir "$T" A0 resume "$ASID")
t "agy dry-run resume has --conversation SID and no -i" bash -c 'printf "%s" "$1" | grep -q -- "--conversation $2" && ! printf "%s" "$1" | grep -q -- " -i "' _ "$out" "$ASID"
rej "agy resume rejects a non-uuid id" "conversation uuid" --cli agy A0 resume ses_abc123

rm -f "$FLEET_STATE/stub-agy.argv" "$FLEET_STATE/stub-agy.env"
out=$("$FLEET" launch --cli agy --dir "$T" A1 prompt "$T/prompt.txt" 2>&1); rc=$?
t "agy launch A1 (proof on) prints one OK line with a conversation uuid" bash -c 'test "$2" -eq 0 && printf "%s\n" "$1" | grep -Eqx "OK A1 cli=agy mode=prompt pane=%[0-9]+ session=[0-9a-f-]{36} goal=n/a"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
a1sid=$(jq -r '.A1.session_id // empty' "$R")
t "roster A1 session_id has a brain transcript under FLEET_AGY_HOME" test -s "$FLEET_AGY_HOME/antigravity-cli/brain/$a1sid/.system_generated/logs/transcript_full.jsonl"
argv=$(cat "$FLEET_STATE/stub-agy.argv" 2>/dev/null)
t "agy stub argv starts with --dangerously-skip-permissions" bash -c 'test "$(printf "%s\n" "$1" | head -1)" = --dangerously-skip-permissions' _ "$argv"
t "agy stub argv has --agent then fleet-worker" bash -c 'printf "%s\n" "$1" | grep -A1 -x -e --agent | grep -qx fleet-worker' _ "$argv"
t "agy stub argv has -i then the prompt text" bash -c 'printf "%s\n" "$1" | grep -A1 -x -e -i | grep -qx "Do the thing."' _ "$argv"
t "agy --settings file on disk is the lane JSON" bash -c 'f=$(printf "%s\n" "$1" | grep -A1 -x -e --settings | tail -1); jq -e ".toolPermission==\"always-proceed\"" "$f"' _ "$argv"
t "agy window env has FLEET_AGY_HOME and FLEET_NAME=A1" bash -c 'grep -qx "FLEET_AGY_HOME=$2" "$1" && grep -qx FLEET_NAME=A1 "$1"' _ "$FLEET_STATE/stub-agy.env" "$FLEET_AGY_HOME"

out=$("$FLEET" launch --cli agy --dir "$T" A2 goal "$T/good.txt" 2>&1); rc=$?
t "agy goal A2 exits 0 with goal=active" bash -c 'test "$2" -eq 0 && printf "%s\n" "$1" | grep -Eqx "OK A2 cli=agy mode=goal pane=%[0-9]+ session=[0-9a-f-]{36} goal=active"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
a2sid=$(jq -r '.A2.session_id // empty' "$R")
t "agy A2 transcript has the typed /goal with slash-command metadata" bash -c 'grep -F "/goal is a [Slash Command]" "$1" | grep -qF "Work from goal.json"' _ "$FLEET_AGY_HOME/antigravity-cli/brain/$a2sid/.system_generated/logs/transcript_full.jsonl"
t "agy A2 stub argv has no -i" bash -c '! grep -qx -e -i "$1"' _ "$FLEET_STATE/stub-agy.argv"

out=$("$FLEET" launch --cli agy --dir "$T" A3 resume "$a1sid" 2>&1); rc=$?
t "agy resume A3 prints OK session=<A1 conversation>" bash -c 'test "$2" -eq 0 && printf "%s\n" "$1" | grep -Eqx "OK A3 cli=agy mode=resume pane=%[0-9]+ session=$3 goal=n/a"' _ "$out" "$rc" "$a1sid" \
  || printf '    launcher said: %s\n' "$out"
t "agy stub argv has --conversation then the A1 id" bash -c 'grep -A1 -x -e --conversation "$1" | grep -qx "$2"' _ "$FLEET_STATE/stub-agy.argv" "$a1sid"

out=$(FLEET_PROOF_TIMEOUT=5 "$FLEET" launch --cli agy --dir "$T" ATRUST prompt "$T/prompt.txt" 2>&1); rc=$?
t "agy trust dialog: launch exits 1 with FAIL naming the dialog" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "^FAIL agy workspace trust dialog in pane"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
out=$(FLEET_PROOF_TIMEOUT=5 "$FLEET" launch --cli agy --dir "$T" ANOAUTH prompt "$T/prompt.txt" 2>&1); rc=$?
t "agy sign-in wait: launch exits 1 with FAIL naming sign-in" bash -c 'test "$2" -eq 1 && printf "%s" "$1" | grep -q "^FAIL agy is waiting for sign-in"' _ "$out" "$rc" \
  || printf '    launcher said: %s\n' "$out"
t "control: the failed agy launches recorded no session_id" bash -c 'test "$(jq -r ".ATRUST.session_id, .ANOAUTH.session_id" "$1" | sort -u)" = null' _ "$R"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILS FAILED"; exit 1; fi
