#!/usr/bin/env bash
# Tests for per-role launches. Private tmux server, temp state, stub CLIs. Exit 1 on any FAIL.
set -u
HERE=$(cd "$(dirname "$0")" && pwd -P)
ROOT=$(cd "$HERE/.." && pwd -P)
FLEET=$ROOT/bin/fleet
T=$(mktemp -d "${TMPDIR:-/tmp}/rolestest.XXXXXX")
export FLEET_TMUX_SOCKET=rolestest-$$
export FLEET_TMUX_SESSION=rolesess
export FLEET_STATE=$T/state
export FLEET_CLAUDE_BIN=$HERE/stubs/claude
export FLEET_CODEX_BIN=$HERE/stubs/codex
export FLEET_OPENCODE_BIN=$HERE/stubs/opencode
export FLEET_DIR=$T
unset FLEET_ROLE
mkdir -p "$FLEET_STATE"
cleanup() { tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT

FAILS=0
t() { local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi; }
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnot() { ! printf '%s' "$1" | grep -qF -- "$2"; }

printf '%s\n' 'Do the thing.' > "$T/prompt.txt"

# ---- control: the harness reports a failing check
ctl=$(FAILS=0; t "inner" false; echo "inner_fails=$FAILS")
t "control: a failing check prints FAIL and counts it" bash -c 'printf "%s" "$1" | grep -q "^FAIL inner" && printf "%s" "$1" | grep -q "inner_fails=1"' _ "$ctl"

ROLES="orchestrator desk listener worker"
dry() { "$FLEET" launch --dry-run "$@" 2>&1; }

# ---- role files
for r in $ROLES; do
  t "$r.md exists, 15-40 lines" bash -c 'n=$(wc -l < "$1"); [ "$n" -ge 15 ] && [ "$n" -le 40 ]' _ "$ROOT/roles/$r.md"
  t "$r.md has no em dash" bash -c '! grep -q "$(printf "\342\200\224")" "$1"' _ "$ROOT/roles/$r.md"
done

# ---- claude.json: exact deny lists
want_orch='["Edit","Write","NotebookEdit","Bash(git commit:*)","Bash(git push:*)","AskUserQuestion"]'
want_desk='["Edit","Write","NotebookEdit","Bash(fleet launch:*)","Bash(fleet park:*)","Bash(fleet stop:*)","Bash(fleet reopen:*)","Bash(git commit:*)","Bash(git push:*)","AskUserQuestion"]'
want_list=$want_desk
want_work='["Bash(fleet launch:*)","Bash(fleet park:*)","Bash(fleet stop:*)","Bash(fleet reopen:*)","AskUserQuestion"]'
for r in $ROLES; do
  case $r in orchestrator) w=$want_orch ;; desk) w=$want_desk ;; listener) w=$want_list ;; worker) w=$want_work ;; esac
  t "$r.claude.json parses with only permissions.deny" jq -e '(keys == ["permissions"]) and (.permissions|keys == ["deny"])' "$ROOT/roles/$r.claude.json"
  t "$r.claude.json deny list is exact" jq -e --argjson w "$w" '.permissions.deny == $w' "$ROOT/roles/$r.claude.json"
  t "control: $r deny list != a perturbed list" bash -c '! jq -e --argjson w "$1" ".permissions.deny == (\$w + [\"X\"])" "$2" >/dev/null' _ "$w" "$ROOT/roles/$r.claude.json"
done

# ---- codex.toml: developer_instructions == md, enabled_tools exact
for r in $ROLES; do
  case $r in
    orchestrator) tl='peer_list peer_send peer_inbox decision_open decision_list decision_answer' ;;
    desk) tl='peer_list peer_send peer_inbox decision_list decision_answer' ;;
    listener) tl='peer_list peer_send' ;;
    worker) tl='peer_list peer_send peer_inbox decision_open' ;;
  esac
  cat > "$T/chk_toml.py" <<'PY'
import re, sys
toml = open(sys.argv[1]).read()
md = open(sys.argv[2]).read()
tools = sys.argv[3].split()
m = re.match(r'developer_instructions = """\n(.*?)\n"""\n', toml, re.S)
assert m, "no developer_instructions"
assert m.group(1).rstrip("\n") == md.rstrip("\n"), "text differs from md"
m2 = re.search(r'\[mcp_servers\.fleet\]\nenabled_tools = \[(.*?)\]', toml)
assert m2, "no enabled_tools"
got = re.findall(r'"([^"]+)"', m2.group(1))
assert got == tools, got
PY
  t "$r.codex.toml developer_instructions matches $r.md and tools are exact" python3 -I "$T/chk_toml.py" "$ROOT/roles/$r.codex.toml" "$ROOT/roles/$r.md" "$tl"
done
printf 'developer_instructions = """\nstale\n"""\n\n[mcp_servers.fleet]\nenabled_tools = ["peer_list"]\n' > "$T/bad.toml"
t "control: a stale codex.toml is detected" bash -c '! python3 -I "$1" "$2" "$3" peer_list' _ "$T/chk_toml.py" "$T/bad.toml" "$ROOT/roles/listener.md"

# ---- opencode.json
for r in $ROLES; do
  case $r in worker) tw=true ;; *) tw=false ;; esac
  t "$r.opencode.json shape (mode primary, prompt file, tools.write/edit=$tw)" jq -e --arg p "{file:./roles/$r.md}" --argjson w "$tw" \
    '.mode=="primary" and .prompt==$p and .tools.write==$w and .tools.edit==$w and (.permission.bash|type=="object")' "$ROOT/roles/$r.opencode.json"
done
t "opencode orchestrator denies git commit, not fleet launch" jq -e '.permission.bash["git commit*"]=="deny" and (.permission.bash|has("fleet launch*")|not)' "$ROOT/roles/orchestrator.opencode.json"
t "opencode worker denies fleet launch" jq -e '.permission.bash["fleet launch*"]=="deny"' "$ROOT/roles/worker.opencode.json"

# ---- kiro agent json (installer fills name and prompt; the deny set lives here)
for r in $ROLES; do
  t "$r.kiro.json is valid JSON with V3 permissions.rules" jq -e '.tools==["*"] and .includeMcpJson==true and (.permissions.rules|type=="array") and (.description|type=="string")' "$ROOT/roles/$r.kiro.json"
  t "$r.kiro.json has no em dash" bash -c '! grep -q "$(printf "\342\200\224")" "$1"' _ "$ROOT/roles/$r.kiro.json"
done
for r in worker desk listener; do
  for v in launch park stop reopen; do
    t "kiro $r denies shell fleet $v*" jq -e --arg g "fleet $v*" '[.permissions.rules[]|select(.capability=="shell" and .effect=="deny" and (.match|index($g)))]|length==1' "$ROOT/roles/$r.kiro.json"
  done
done
for r in orchestrator desk listener; do
  t "kiro $r denies fs_write ** and git commit/push" jq -e '([.permissions.rules[]|select(.capability=="fs_write" and .effect=="deny" and .match==["**"])]|length==1) and ([.permissions.rules[]|select(.capability=="shell" and .effect=="deny")|.match[]] as $m | ($m|index("git commit*")!=null) and ($m|index("git push*")!=null))' "$ROOT/roles/$r.kiro.json"
done
t "kiro worker does not deny writes or git" jq -e '[.permissions.rules[]|select(.capability=="fs_write" or (.match|index("git commit*")))]|length==0' "$ROOT/roles/worker.kiro.json"
t "kiro orchestrator does not deny fleet launch" jq -e '[.permissions.rules[].match[]]|index("fleet launch*")==null' "$ROOT/roles/orchestrator.kiro.json"
t "control: a kiro file missing the fleet park rule fails the deny check" bash -c '! jq \
  '"'"'.permissions.rules|=map(select((.match//[])|index("fleet park*")|not))'"'"' "$1" | jq -e '"'"'[.permissions.rules[]|select(.match|index("fleet park*"))]|length==1'"'"' >/dev/null' _ "$ROOT/roles/worker.kiro.json"

# ---- claude dry-run wiring
for r in $ROLES; do
  out=$(dry --cli claude --role $r --dir "$T" N1 prompt "$T/prompt.txt")
  t "dry-run claude --role $r has --settings <abs>/roles/$r.claude.json" has "$out" "--settings $ROOT/roles/$r.claude.json"
  t "dry-run claude --role $r has --append-system-prompt-file <abs>/roles/$r.md" has "$out" "--append-system-prompt-file $ROOT/roles/$r.md"
  t "dry-run claude --role $r exports FLEET_ROLE=$r" has "$out" "FLEET_ROLE=$r"
done
out=$(dry --cli claude --dir "$T" N1 prompt "$T/prompt.txt")
t "default role is worker (settings)" has "$out" "--settings $ROOT/roles/worker.claude.json"
t "default role is worker (FLEET_ROLE)" has "$out" "FLEET_ROLE=worker"
out=$(dry --cli claude --dir "$T" N1 desk)
t "desk mode defaults to role desk (settings)" has "$out" "--settings $ROOT/roles/desk.claude.json"
t "desk mode defaults to role desk (FLEET_ROLE)" has "$out" "FLEET_ROLE=desk"
out=$(dry --cli claude --role orchestrator --dir "$T" N1 desk)
t "explicit --role overrides desk-mode default" has "$out" "FLEET_ROLE=orchestrator"
out=$("$FLEET" launch --dry-run --role bogus --dir "$T" N1 prompt hi 2>&1); rc=$?
t "unknown role rejected (exit 1, FAIL)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL unknown --role"' _ "$rc" "$out"
t "dry-run touches no state" test ! -e "$FLEET_STATE/roster.json" -a ! -d "$FLEET_STATE/launch"

# ---- codex profile flag only when the profile file exists
mkdir -p "$T/codex-empty" "$T/codex-full"
: > "$T/codex-full/fleet-desk.config.toml"
out=$(CODEX_HOME=$T/codex-empty dry --cli codex --role desk --dir "$T" N1 prompt hi)
t "codex without profile file: no -p fleet-desk" hasnot "$out" "-p fleet-desk"
t "codex without profile file: warns" has "$out" "warn: codex profile fleet-desk not installed"
out=$(CODEX_HOME=$T/codex-full dry --cli codex --role desk --dir "$T" N1 prompt hi)
t "codex with profile file: -p fleet-desk present" has "$out" " -p fleet-desk"
t "codex with profile file: no warning" hasnot "$out" "warn:"
out=$(CODEX_HOME=$T/codex-full dry --cli codex --role worker --dir "$T" N1 prompt hi)
t "codex: other role's profile file does not enable -p fleet-worker" hasnot "$out" "-p fleet-worker"

# ---- opencode agent flag only when opencode.json has the agent
mkdir -p "$T/xdg-empty/opencode" "$T/xdg-full/opencode"
printf '{}\n' > "$T/xdg-empty/opencode/opencode.json"
printf '{"agent":{"fleet-listener":{"mode":"primary"}}}\n' > "$T/xdg-full/opencode/opencode.json"
out=$(XDG_CONFIG_HOME=$T/xdg-empty dry --cli opencode --role listener --dir "$T" N1 prompt hi)
t "opencode without agent: no --agent" hasnot "$out" "--agent"
t "opencode without agent: warns" has "$out" "warn: opencode agent fleet-listener not installed"
out=$(XDG_CONFIG_HOME=$T/xdg-full dry --cli opencode --role listener --dir "$T" N1 prompt hi)
t "opencode with agent: --agent fleet-listener present" has "$out" " --agent fleet-listener"
out=$(XDG_CONFIG_HOME=$T/xdg-full dry --cli opencode --role worker --dir "$T" N1 prompt hi)
t "opencode: other role's agent does not enable --agent fleet-worker" hasnot "$out" "--agent fleet-worker"
out=$(XDG_CONFIG_HOME=$T/nonexistent dry --cli opencode --role worker --dir "$T" N1 prompt hi)
t "opencode with no config file: warns, no --agent" bash -c 'printf "%s" "$1" | grep -q "warn: opencode agent" && ! printf "%s" "$1" | grep -q -- "--agent"' _ "$out"

# ---- role guard
for r in worker desk listener; do
  out=$(FLEET_ROLE=$r "$FLEET" launch --no-proof --dir "$T" G1 prompt "$T/prompt.txt" 2>&1); rc=$?
  t "FLEET_ROLE=$r launch refused (exit 1)" test $rc -eq 1
  t "FLEET_ROLE=$r launch prints FAIL role $r can't launch" has "$out" "FAIL role $r can't launch"
done
t "refused launch wrote no roster" test ! -s "$FLEET_STATE/roster.json"
out=$(FLEET_ROLE=worker "$FLEET" stop G1 2>&1); rc=$?
t "FLEET_ROLE=worker stop refused" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role worker can.t stop"' _ "$rc" "$out"
for v in park reopen; do
  out=$(FLEET_ROLE=desk "$FLEET" $v G1 2>&1); rc=$?
  t "FLEET_ROLE=desk $v refused" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role desk can.t $3"' _ "$rc" "$out" "$v"
done
out=$(FLEET_ROLE=desk "$FLEET" cleanup --apply 2>&1); rc=$?
t "FLEET_ROLE=desk cleanup --apply refused" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role desk can.t cleanup"' _ "$rc" "$out"
out=$(FLEET_ROLE=desk "$FLEET" cleanup 2>&1)
t "control: FLEET_ROLE=desk cleanup (dry run) is not role-refused" hasnot "$out" "can't cleanup"
# lifecycle dispatch: execs fleet-lifecycle with the same args, or says it is missing
if [ -f "$ROOT/bin/fleet-lifecycle" ]; then
  out=$("$FLEET" tick 2>&1)
  t "tick dispatches to fleet-lifecycle (no 'missing')" hasnot "$out" "fleet-lifecycle missing"
else
  out=$("$FLEET" tick 2>&1)
  t "tick without fleet-lifecycle prints FAIL fleet-lifecycle missing" has "$out" "FAIL fleet-lifecycle missing"
fi

# ---- real launches of stubs
export CLAUDECODE=1 CLAUDE_CODE_CHILD_SESSION=1
rm -f "$FLEET_STATE/stub-claude.env" "$FLEET_STATE/stub-claude.argv"
out=$(FLEET_ROLE=orchestrator "$FLEET" launch --no-proof --role orchestrator --dir "$T" ORCH prompt "$T/prompt.txt" 2>&1); rc=$?
t "control: FLEET_ROLE=orchestrator launch succeeds" test $rc -eq 0
t "control: orchestrator launch prints OK line" bash -c 'printf "%s" "$1" | grep -q "^OK ORCH cli=claude mode=prompt pane=%"' _ "$out"
for i in $(seq 1 20); do [ -s "$FLEET_STATE/stub-claude.env" ] && break; sleep 0.5; done
t "window env has FLEET_ROLE=orchestrator" grep -q '^FLEET_ROLE=orchestrator$' "$FLEET_STATE/stub-claude.env"
argv=$(cat "$FLEET_STATE/stub-claude.argv" 2>/dev/null)
t "stub argv has --settings <abs>/roles/orchestrator.claude.json" has "$argv" "$ROOT/roles/orchestrator.claude.json"
t "stub argv has --append-system-prompt-file" has "$argv" "--append-system-prompt-file"
t "roster ORCH has role=orchestrator" bash -c 'test "$(jq -r .ORCH.role "$1")" = orchestrator' _ "$FLEET_STATE/roster.json"

rm -f "$FLEET_STATE/stub-claude.env"
t "unset FLEET_ROLE launch (default worker) succeeds" "$FLEET" launch --no-proof --dir "$T" WRK prompt "$T/prompt.txt"
for i in $(seq 1 20); do [ -s "$FLEET_STATE/stub-claude.env" ] && break; sleep 0.5; done
t "window env has FLEET_ROLE=worker" grep -q '^FLEET_ROLE=worker$' "$FLEET_STATE/stub-claude.env"
t "roster WRK has role=worker" bash -c 'test "$(jq -r .WRK.role "$1")" = worker' _ "$FLEET_STATE/roster.json"
t "log has a role=worker started line" grep -q ' WRK started cli=claude mode=prompt role=worker' "$FLEET_STATE/log/fleet.log"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILS FAILED"; exit 1; fi
