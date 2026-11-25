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
unset FLEET_PROBE_ALLOW_UNVERIFIED  # test-only override for test_probe_live.sh; never inherited here
export FLEET_CLAUDE_BIN=$HERE/stubs/claude
export FLEET_CODEX_BIN=$HERE/stubs/codex
export FLEET_OPENCODE_BIN=$HERE/stubs/opencode
export FLEET_DIR=$T
export FLEET_NO_DESKS=1  # issue #9: lanes here launch into a scratch fleet with no desks on purpose; desks tests opt back in
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

ROLES="route decide watch learn worker"
OLD_ROLES="orchestrator desk listener"  # retired with the pre-desks sessions: no files, refused at launch in every mode
dry() { "$FLEET" launch --dry-run "$@" 2>&1; }

# ---- role files
for r in $ROLES; do
  t "$r.md exists, 15-40 lines" bash -c 'n=$(wc -l < "$1"); [ "$n" -ge 15 ] && [ "$n" -le 40 ]' _ "$ROOT/roles/$r.md"
  t "$r.md has no em dash" bash -c '! grep -q "$(printf "\342\200\224")" "$1"' _ "$ROOT/roles/$r.md"
done

# ---- claude.json: exact deny lists
want_orch='["Edit","Write","NotebookEdit","Bash(git commit:*)","Bash(git push:*)","AskUserQuestion"]'
want_desk='["Edit","Write","NotebookEdit","Bash(fleet launch:*)","Bash(fleet park:*)","Bash(fleet stop:*)","Bash(fleet reopen:*)","Bash(git commit:*)","Bash(git push:*)","AskUserQuestion"]'
want_work='["Bash(fleet launch:*)","Bash(fleet park:*)","Bash(fleet stop:*)","Bash(fleet reopen:*)","AskUserQuestion"]'
for r in $ROLES; do
  case $r in route) w=$want_orch ;; decide|watch|learn) w=$want_desk ;; worker) w=$want_work ;; esac
  t "$r.claude.json parses with only permissions.deny and hooks" jq -e '(keys == ["hooks","permissions"]) and (.permissions|keys == ["deny"])' "$ROOT/roles/$r.claude.json"
  t "$r.claude.json mirrors SendMessage via \$FLEET_MESH mirror (PostToolUse, exit 0)" jq -e '(.hooks|keys == ["PostToolUse"]) and (.hooks.PostToolUse|length == 1) and (.hooks.PostToolUse[0].matcher == "SendMessage") and (.hooks.PostToolUse[0].hooks[0].command|test("python3 \"\\$FLEET_MESH\" mirror") and endswith("exit 0"))' "$ROOT/roles/$r.claude.json"
  t "$r.claude.json deny list is exact" jq -e --argjson w "$w" '.permissions.deny == $w' "$ROOT/roles/$r.claude.json"
  t "control: $r deny list != a perturbed list" bash -c '! jq -e --argjson w "$1" ".permissions.deny == (\$w + [\"X\"])" "$2" >/dev/null' _ "$w" "$ROOT/roles/$r.claude.json"
done

# ---- codex.toml: developer_instructions == md, enabled_tools exact
for r in $ROLES; do
  case $r in
    route) tl='peer_list peer_send peer_inbox decision_open decision_list decision_answer' ;;
    decide) tl='peer_list peer_send peer_inbox decision_list decision_answer' ;;
    watch) tl='peer_list peer_send decision_open' ;;
    learn) tl='peer_list peer_send peer_inbox' ;;
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
t "control: a stale codex.toml is detected" bash -c '! python3 -I "$1" "$2" "$3" peer_list' _ "$T/chk_toml.py" "$T/bad.toml" "$ROOT/roles/watch.md"

# ---- opencode.json
for r in $ROLES; do
  case $r in worker) tw=true ;; *) tw=false ;; esac
  t "$r.opencode.json shape (mode primary, prompt file, tools.write/edit=$tw)" jq -e --arg p "{file:./roles/$r.md}" --argjson w "$tw" \
    '.mode=="primary" and .prompt==$p and .tools.write==$w and .tools.edit==$w and (.permission.bash|type=="object")' "$ROOT/roles/$r.opencode.json"
done
t "opencode route denies git commit, not fleet launch" jq -e '.permission.bash["git commit*"]=="deny" and (.permission.bash|has("fleet launch*")|not)' "$ROOT/roles/route.opencode.json"
t "opencode worker denies fleet launch" jq -e '.permission.bash["fleet launch*"]=="deny"' "$ROOT/roles/worker.opencode.json"

# ---- kiro agent json (installer fills name and prompt; the deny set lives here)
for r in $ROLES; do
  t "$r.kiro.json is valid JSON with V3 permissions.rules" jq -e '.tools==["*"] and .includeMcpJson==true and (.permissions.rules|type=="array") and (.description|type=="string")' "$ROOT/roles/$r.kiro.json"
  t "$r.kiro.json has no em dash" bash -c '! grep -q "$(printf "\342\200\224")" "$1"' _ "$ROOT/roles/$r.kiro.json"
done
for r in worker decide watch learn; do
  for v in launch park stop reopen; do
    t "kiro $r denies shell fleet $v*" jq -e --arg g "fleet $v*" '[.permissions.rules[]|select(.capability=="shell" and .effect=="deny" and (.match|index($g)))]|length==1' "$ROOT/roles/$r.kiro.json"
  done
done
for r in route decide watch learn; do
  t "kiro $r denies fs_write ** and git commit/push" jq -e '([.permissions.rules[]|select(.capability=="fs_write" and .effect=="deny" and .match==["**"])]|length==1) and ([.permissions.rules[]|select(.capability=="shell" and .effect=="deny")|.match[]] as $m | ($m|index("git commit*")!=null) and ($m|index("git push*")!=null))' "$ROOT/roles/$r.kiro.json"
done
t "kiro worker does not deny writes or git" jq -e '[.permissions.rules[]|select(.capability=="fs_write" or (.match|index("git commit*")))]|length==0' "$ROOT/roles/worker.kiro.json"
t "kiro route does not deny fleet launch" jq -e '[.permissions.rules[].match[]]|index("fleet launch*")==null' "$ROOT/roles/route.kiro.json"
t "control: a kiro file missing the fleet park rule fails the deny check" bash -c '! jq \
  '"'"'.permissions.rules|=map(select((.match//[])|index("fleet park*")|not))'"'"' "$1" | jq -e '"'"'[.permissions.rules[]|select(.match|index("fleet park*"))]|length==1'"'"' >/dev/null' _ "$ROOT/roles/worker.kiro.json"

# ---- agy: settings fragment (deny rules, merged into the per-lane --settings) and agent frontmatter
for r in $ROLES; do
  t "$r.agy.json is valid JSON with only permissions.deny" jq -e '(keys==["permissions"]) and (.permissions|keys==["deny"]) and (.permissions.deny|type=="array")' "$ROOT/roles/$r.agy.json"
  t "$r.agy.md frontmatter: name fleet-$r, mainAgent, not a subagent, inherits MCP" python3 -c '
import sys, yaml
t = open(sys.argv[1]).read(); assert t.startswith("---\n") and t.count("---\n") == 2
fm = yaml.safe_load(t.split("---\n")[1])
assert fm["name"] == "fleet-" + sys.argv[2] and fm["mainAgent"] is True and fm["subagent"] is False and fm["inheritMcp"] is True
assert isinstance(fm["description"], str) and "run_command" in fm["tools"]' "$ROOT/roles/$r.agy.md" "$r"
  t "$r.agy.* have no em dash" bash -c '! grep -q "$(printf "\342\200\224")" "$1" "$2"' _ "$ROOT/roles/$r.agy.json" "$ROOT/roles/$r.agy.md"
done
for r in worker decide watch learn; do
  for v in launch park stop reopen; do
    t "agy $r denies command(fleet $v)" jq -e --arg g "command(fleet $v)" '.permissions.deny|index($g)!=null' "$ROOT/roles/$r.agy.json"
  done
done
for r in route decide watch learn; do
  t "agy $r denies write_file(*) and git commit/push" jq -e '.permissions.deny as $d | ($d|index("write_file(*)")!=null) and ($d|index("command(git commit)")!=null) and ($d|index("command(git push)")!=null)' "$ROOT/roles/$r.agy.json"
  t "agy $r agent has no write or edit tool" bash -c '! grep -Eq "write_file|edit_file" "$1"' _ "$ROOT/roles/$r.agy.md"
done
t "agy worker does not deny writes or git" jq -e '[.permissions.deny[]|select(startswith("write_file") or startswith("command(git"))]|length==0' "$ROOT/roles/worker.agy.json"
t "agy worker agent has write_file and edit_file" bash -c 'grep -q write_file "$1" && grep -q edit_file "$1"' _ "$ROOT/roles/worker.agy.md"
t "agy route does not deny fleet launch" jq -e '.permissions.deny|index("command(fleet launch)")==null' "$ROOT/roles/route.agy.json"
t "control: an agy file missing the fleet park rule fails the deny check" bash -c '! jq '"'"'.permissions.deny|=map(select(.!="command(fleet park)"))'"'"' "$1" | jq -e '"'"'.permissions.deny|index("command(fleet park)")!=null'"'"' >/dev/null' _ "$ROOT/roles/worker.agy.json"

for r in $OLD_ROLES; do t "agy: no retired $r.agy.* role files" bash -c '! ls "$1"/$2.agy.* >/dev/null 2>&1' _ "$ROOT/roles" "$r"; done

# ---- claude dry-run wiring
for r in $ROLES; do
  out=$(dry --cli claude --role $r --dir "$T" N1 prompt "$T/prompt.txt")
  t "dry-run claude --role $r has --settings <abs>/roles/$r.claude.json" has "$out" "--settings $ROOT/roles/$r.claude.json"
  t "dry-run claude --role $r has --append-system-prompt-file <abs>/roles/$r.md" has "$out" "--append-system-prompt-file $ROOT/roles/$r.md"
  t "dry-run claude --role $r exports FLEET_ROLE=$r" has "$out" "FLEET_ROLE=$r"
  t "dry-run claude --role $r exports FLEET_MESH=<abs>/lib/fleet_mesh.py" has "$out" "FLEET_MESH=$ROOT/lib/fleet_mesh.py"
  t "dry-run claude --role $r passes the fleet MCP per launch" has "$out" "--mcp-config"
  t "dry-run claude --role $r MCP config runs <abs>/lib/fleet_mesh.py serve" bash -c 'printf "%s" "$1" | grep -- "--mcp-config" | grep -qF "$2"' _ "$out" "$ROOT/lib/fleet_mesh.py"
done
out=$(dry --cli claude --dir "$T" N1 resume 11111111-2222-3333-4444-555555555555)
t "dry-run claude resume also passes the fleet MCP" bash -c 'printf "%s" "$1" | grep -- "--resume" | grep -- "--mcp-config" | grep -qF "$2"' _ "$out" "$ROOT/lib/fleet_mesh.py"
out=$(dry --cli claude --dir "$T" N1 prompt "$T/prompt.txt")
t "default role is worker (settings)" has "$out" "--settings $ROOT/roles/worker.claude.json"
t "default role is worker (FLEET_ROLE)" has "$out" "FLEET_ROLE=worker"
out=$(dry --cli claude --dir "$T" N1 desk)
t "desk mode defaults to role decide (settings)" has "$out" "--settings $ROOT/roles/decide.claude.json"
t "desk mode defaults to role decide (FLEET_ROLE)" has "$out" "FLEET_ROLE=decide"
out=$(dry --cli claude --role route --dir "$T" N1 desk)
t "explicit --role overrides desk-mode default" has "$out" "FLEET_ROLE=route"
out=$("$FLEET" launch --dry-run --role bogus --dir "$T" N1 prompt hi 2>&1); rc=$?
t "unknown role rejected (exit 1, FAIL)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL unknown --role"' _ "$rc" "$out"
t "dry-run touches no state" test ! -e "$FLEET_STATE/roster.json" -a ! -d "$FLEET_STATE/launch"

# ---- codex profile flag only when the profile file exists
mkdir -p "$T/codex-empty" "$T/codex-full"
: > "$T/codex-full/fleet-decide.config.toml"
out=$(CODEX_HOME=$T/codex-empty dry --cli codex --role decide --dir "$T" N1 prompt hi)
t "codex without profile file: no -p fleet-decide" hasnot "$out" "-p fleet-decide"
t "codex without profile file: warns" has "$out" "warn: codex profile fleet-decide not installed"
out=$(CODEX_HOME=$T/codex-full dry --cli codex --role decide --dir "$T" N1 prompt hi)
t "codex with profile file: -p fleet-decide present" has "$out" " -p fleet-decide"
t "codex with profile file: no warning" hasnot "$out" "warn:"
out=$(CODEX_HOME=$T/codex-full dry --cli codex --role worker --dir "$T" N1 prompt hi)
t "codex: other role's profile file does not enable -p fleet-worker" hasnot "$out" "-p fleet-worker"

# ---- opencode agent flag only when opencode.json has the agent
mkdir -p "$T/xdg-empty/opencode" "$T/xdg-full/opencode"
printf '{}\n' > "$T/xdg-empty/opencode/opencode.json"
printf '{"agent":{"fleet-watch":{"mode":"primary"}}}\n' > "$T/xdg-full/opencode/opencode.json"
out=$(XDG_CONFIG_HOME=$T/xdg-empty dry --cli opencode --role watch --dir "$T" N1 prompt hi)
t "opencode without agent: no --agent" hasnot "$out" "--agent"
t "opencode without agent: warns" has "$out" "warn: opencode agent fleet-watch not installed"
out=$(XDG_CONFIG_HOME=$T/xdg-full dry --cli opencode --role watch --dir "$T" N1 prompt hi)
t "opencode with agent: --agent fleet-watch present" has "$out" " --agent fleet-watch"
out=$(XDG_CONFIG_HOME=$T/xdg-full dry --cli opencode --role worker --dir "$T" N1 prompt hi)
t "opencode: other role's agent does not enable --agent fleet-worker" hasnot "$out" "--agent fleet-worker"
out=$(XDG_CONFIG_HOME=$T/nonexistent dry --cli opencode --role worker --dir "$T" N1 prompt hi)
t "opencode with no config file: warns, no --agent" bash -c 'printf "%s" "$1" | grep -q "warn: opencode agent" && ! printf "%s" "$1" | grep -q -- "--agent"' _ "$out"

# ---- role guard
for r in worker orchestrator desk listener decide watch learn; do
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
out=$(FLEET_ROLE=route "$FLEET" launch --no-proof --role route --dir "$T" ORCH prompt "$T/prompt.txt" 2>&1); rc=$?
t "control: FLEET_ROLE=route launch succeeds" test $rc -eq 0
t "control: route launch prints OK line" bash -c 'printf "%s" "$1" | grep -q "^OK ORCH cli=claude mode=prompt pane=%"' _ "$out"
for i in $(seq 1 20); do [ -s "$FLEET_STATE/stub-claude.env" ] && break; sleep 0.5; done
t "window env has FLEET_ROLE=route" grep -q '^FLEET_ROLE=route$' "$FLEET_STATE/stub-claude.env"
argv=$(cat "$FLEET_STATE/stub-claude.argv" 2>/dev/null)
t "stub argv has --settings <abs>/roles/route.claude.json" has "$argv" "$ROOT/roles/route.claude.json"
t "stub argv has --append-system-prompt-file" has "$argv" "--append-system-prompt-file"
t "stub argv: the arg after --mcp-config is JSON running <abs>/lib/fleet_mesh.py serve" bash -c 'printf "%s\n" "$1" | grep -A1 -x -- "--mcp-config" | tail -1 | jq -e --arg m "$2" ".mcpServers.fleet.command == \"python3\" and .mcpServers.fleet.args == [\$m, \"serve\"]"' _ "$argv" "$ROOT/lib/fleet_mesh.py"
t "control: the --mcp-config check rejects another path" bash -c '! printf "%s\n" "$1" | grep -A1 -x -- "--mcp-config" | tail -1 | jq -e --arg m "/nope/fleet_mesh.py" ".mcpServers.fleet.args == [\$m, \"serve\"]" >/dev/null' _ "$argv"
t "window env has FLEET_MESH=<abs>/lib/fleet_mesh.py" grep -qx "FLEET_MESH=$ROOT/lib/fleet_mesh.py" "$FLEET_STATE/stub-claude.env"
t "roster ORCH has role=route" bash -c 'test "$(jq -r .ORCH.role "$1")" = route' _ "$FLEET_STATE/roster.json"

rm -f "$FLEET_STATE/stub-claude.env"
t "unset FLEET_ROLE launch (default worker) succeeds" "$FLEET" launch --no-proof --dir "$T" WRK prompt "$T/prompt.txt"
for i in $(seq 1 20); do [ -s "$FLEET_STATE/stub-claude.env" ] && break; sleep 0.5; done
t "window env has FLEET_ROLE=worker" grep -q '^FLEET_ROLE=worker$' "$FLEET_STATE/stub-claude.env"
t "roster WRK has role=worker" bash -c 'test "$(jq -r .WRK.role "$1")" = worker' _ "$FLEET_STATE/roster.json"
t "log has a role=worker started line" grep -q ' WRK started cli=claude mode=prompt role=worker' "$FLEET_STATE/log/fleet.log"

out=$("$FLEET" launch --no-proof --role listener --dir "$T" LST prompt "$T/prompt.txt" 2>&1); rc=$?
t "--role listener launch refused (exit 1, renamed watch)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role listener is renamed watch"' _ "$rc" "$out"
t "--role listener launch: no roster entry" bash -c '! jq -e "has(\"LST\")" "$1" >/dev/null' _ "$FLEET_STATE/roster.json"

# ================================================================ phase 1: four desks (docs/design/four-desks.md 5, 7)
# ---- role files for all four desks on every CLI; old spellings keep their own files
for r in route decide watch learn; do
  for ext in md claude.json codex.toml kiro.json opencode.json; do
    t "role file roles/$r.$ext exists" test -f "$ROOT/roles/$r.$ext"
  done
done
for r in $OLD_ROLES; do t "retired: no roles/$r.* files" bash -c '! ls "$1"/roles/"$2".* >/dev/null 2>&1' _ "$ROOT" "$r"; done
t "control: the retired-file check sees an existing role" bash -c 'ls "$1"/roles/route.* >/dev/null 2>&1' _ "$ROOT"
t "route deny list is the old orchestrator list (claude, literal)" jq -e --argjson w "$want_orch" '.permissions.deny == $w' "$ROOT/roles/route.claude.json"
t "decide deny list is the old desk list (claude, literal)" jq -e --argjson w "$want_desk" '.permissions.deny == $w' "$ROOT/roles/decide.claude.json"
t "learn deny list present (claude): Edit, Write, fleet launch, git push" jq -e '.permissions.deny as $d | ["Edit","Write","Bash(fleet launch:*)","Bash(git push:*)"] | all(. as $x | $d | index($x))' "$ROOT/roles/learn.claude.json"
t "learn deny list present (kiro): fs_write ** and fleet launch*" jq -e '([.permissions.rules[]|select(.capability=="fs_write" and .effect=="deny")]|length==1) and ([.permissions.rules[].match[]]|index("fleet launch*")!=null)' "$ROOT/roles/learn.kiro.json"
t "learn deny list present (opencode): no write/edit, bash denies fleet launch and git push" jq -e '.tools.write==false and .tools.edit==false and .permission.bash["fleet launch*"]=="deny" and .permission.bash["git push*"]=="deny"' "$ROOT/roles/learn.opencode.json"
for r in route decide watch learn; do
  t "kiro $r.kiro.json name is fleet-$r" jq -e --arg n "fleet-$r" '.name==$n' "$ROOT/roles/$r.kiro.json"
done
t "watch role text sends to DECIDE, never ROUTE" bash -c 'grep -q DECIDE "$1" && grep -qi "never message ROUTE" "$1"' _ "$ROOT/roles/watch.md"

# ---- role names: the old names are refused at launch (Q110: no backwards compatibility); probe fails closed
for r in route decide watch learn; do
  out=$(dry --cli claude --role $r --dir "$T" N1 prompt "$T/prompt.txt"); rc=$?
  t "--role $r accepted (exit 0, no warn)" bash -c 'test "$1" = 0 && ! printf "%s" "$2" | grep -q "^warn: role"' _ "$rc" "$out"
done
for p in orchestrator:route desk:decide listener:watch; do
  o=${p%%:*} n=${p#*:}
  out=$(dry --cli claude --role $o --dir "$T" N1 prompt "$T/prompt.txt"); rc=$?
  t "--role $o refused: 'role $o is renamed $n' (exit 1)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role $3 is renamed $4"' _ "$rc" "$out" "$o" "$n"
done
for p in orchestrator:route desk:decide listener:watch; do
  o=${p%%:*} n=${p#*:}
  out=$(dry --cli claude --role $o --dir "$T" N1 resume 11111111-2222-3333-4444-555555555555); rc=$?
  t "resume mode refuses --role $o too: 'role $o is renamed $n' (exit 1)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role $3 is renamed $4"' _ "$rc" "$out" "$o" "$n"
done
out=$(dry --cli claude --role route --dir "$T" N1 resume 11111111-2222-3333-4444-555555555555); rc=$?
t "control: resume mode takes --role route" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "FLEET_ROLE=route "' _ "$rc" "$out"
out=$(dry --cli claude --role probe --dir "$T" N1 prompt hi); rc=$?
t "--role probe on claude: read tools only, strict MCP with fleet-probe, probe settings" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -qF -- "--tools Read,Grep,Glob,WebFetch --strict-mcp-config" && printf "%s" "$2" | grep -qF fleet_probe.py && printf "%s" "$2" | grep -qF roles/probe.claude.json && printf "%s" "$2" | grep -q "FLEET_ROLE=probe "' _ "$rc" "$out"
out=$(dry --cli claude --role worker --dir "$T" N1 prompt hi)
t "control: a claude worker keeps its tools (no --tools cut, no strict MCP, no fleet-probe)" bash -c '! printf "%s" "$1" | grep -qF -- "--strict-mcp-config" && ! printf "%s" "$1" | grep -qF fleet_probe.py' _ "$out"
for c in codex agy; do
  out=$(dry --cli $c --role probe --dir "$T" N1 prompt hi); rc=$?
  t "--role probe refused on --cli $c (exit 1, fails closed)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL role probe refused on --cli $3"' _ "$rc" "$out" "$c"
done
out=$(XDG_CONFIG_HOME=$T/xdg-empty dry --cli opencode --role probe --dir "$T" N1 prompt hi); rc=$?
t "--role probe on opencode with no fleet-probe agent fails closed (exit 1, no warn-and-launch)" bash -c 'test "$1" = 1 && printf "%s" "$2" | grep -q "^FAIL opencode agent fleet-probe not installed"' _ "$rc" "$out"
out=$(XDG_CONFIG_HOME=$T/xdg-empty dry --cli opencode --role watch --dir "$T" N1 prompt hi); rc=$?
t "control: --role watch on opencode with no agent still warns and launches" bash -c 'test "$1" = 0 && printf "%s" "$2" | grep -q "warn: opencode agent fleet-watch not installed"' _ "$rc" "$out"
t "roles/probe.* exist for every CLI" bash -c 'for f in md claude.json codex.toml kiro.json opencode.json agy.json agy.md; do test -f "$1/roles/probe.$f" || exit 1; done' _ "$ROOT"
out=$(FLEET_ROLE=route "$FLEET" stop NOPE 2>&1)
t "FLEET_ROLE=route passes the role guard (fails later on the missing lane)" bash -c 'printf "%s" "$1" | grep -q "no lane named NOPE" && ! printf "%s" "$1" | grep -q "can.t stop"' _ "$out"
out=$(FLEET_ROLE=route "$ROOT/bin/fleet-lifecycle" park NOPE 2>&1)
t "FLEET_ROLE=route passes the fleet-lifecycle role guard" hasnot "$out" "can't park"
out=$(FLEET_ROLE=orchestrator "$ROOT/bin/fleet-lifecycle" park NOPE 2>&1)
t "FLEET_ROLE=orchestrator (retired) is refused by the fleet-lifecycle park guard" has "$out" "FAIL role orchestrator can't park"
out=$(FLEET_ROLE=orchestrator "$FLEET" desks up --dry-run --no-proof 2>&1)
t "FLEET_ROLE=orchestrator (retired) is refused by fleet desks" has "$out" "FAIL role orchestrator can't desks"
out=$(FLEET_ROLE=decide "$FLEET" park NOPE 2>&1)
t "control: FLEET_ROLE=decide park refused" has "$out" "FAIL role decide can't park"
t "human_only accepts decide and the human, refuses desk (retired DECISIONS) and watch" python3 -c '
import os, sys; sys.path.insert(0, sys.argv[1]); import fleet_decisions as d
for r in ("decide", ""):
    os.environ["FLEET_ROLE"] = r; d.human_only("pick")
for r in ("desk", "watch"):
    os.environ["FLEET_ROLE"] = r
    try:
        d.human_only("pick")
    except PermissionError:
        continue
    sys.exit(1)
sys.exit(0)' "$ROOT/lib"

# ---- mesh names: DECIDE and ROUTE, no aliasing, old env names ignored
MS=$T/mesh; mkdir -p "$MS"
FLEET_STATE=$MS python3 "$ROOT/lib/fleet_mesh.py" send X ROUTE hi >/dev/null 2>&1
FLEET_STATE=$MS python3 "$ROOT/lib/fleet_mesh.py" send X ORCHESTRATOR hi >/dev/null 2>&1
t "send X ROUTE lands in inbox/ROUTE.jsonl" test "$(wc -l < "$MS/inbox/ROUTE.jsonl" 2>/dev/null)" -eq 1
t "send X ORCHESTRATOR still lands in inbox/ORCHESTRATOR.jsonl (no reroute)" test "$(wc -l < "$MS/inbox/ORCHESTRATOR.jsonl" 2>/dev/null)" -eq 1
dn() { env -u FLEET_DESK -u FLEET_ORCHESTRATOR -u FLEET_DECIDE -u FLEET_ROUTE FLEET_STATE=$1 "${@:2}" python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import fleet_mesh as m; print(m.desk_name(), m.orch_name())' "$ROOT/lib"; }
t "desk/orch names default to DECIDE ROUTE" test "$(dn "$MS")" = "DECIDE ROUTE"
t "FLEET_DECIDE/FLEET_ROUTE override them" test "$(dn "$MS" FLEET_DECIDE=D1 FLEET_ROUTE=R1)" = "D1 R1"
t "control: old FLEET_DESK/FLEET_ORCHESTRATOR are ignored" test "$(dn "$MS" FLEET_DESK=D0 FLEET_ORCHESTRATOR=O0)" = "DECIDE ROUTE"
t "fleet_mesh has no alias layer" bash -c '! grep -qE "aliases\.json|def resolve|FLEET_DESK|FLEET_ORCHESTRATOR" "$1/lib/fleet_mesh.py"' _ "$ROOT"

# ---- fleet tick never parks watch/learn (control: worker with the same goal state parks)
TK=$T/tick; mkdir -p "$TK"
jq -n '{w1:{cli:"claude",role:"watch",pane:"%nope",goalState:"achieved"},l1:{cli:"claude",role:"learn",pane:"%nope",goalState:"achieved"},k1:{cli:"claude",role:"worker",pane:"%nope",goalState:"achieved"}}' > "$TK/roster.json"
tk=$(FLEET_STATE=$TK FLEET_TMUX_SOCKET=tick-$$ "$FLEET" tick 2>/dev/null)
t "tick: watch lane never shows park" bash -c 'printf "%s\n" "$1" | grep "^w1 " | grep -vq "park$"' _ "$tk"
t "tick: learn lane never shows park" bash -c 'printf "%s\n" "$1" | grep "^l1 " | grep -vq "park$"' _ "$tk"
t "control: tick: worker with the same goal shows park" bash -c 'printf "%s\n" "$1" | grep "^k1 " | grep -q "park$"' _ "$tk"

# ---- fleet desks up: 2 split windows deskA (ROUTE | DECIDE) and deskB (WATCH | LEARN), idempotent, private socket
DS=$T/desks; mkdir -p "$DS"
dsk() { env FLEET_STATE=$DS FLEET_TMUX_SOCKET=desks-$$ FLEET_TMUX_SESSION=dsess "$FLEET" desks "$@"; }
# layout as "window:left-desk|right-desk" per window, from roster pane ids and #{pane_left}
dlayout() { # socket session state
  local s=$1 sess=$2 st=$3
  tmux -L "$s" list-panes -s -t "=$sess" -F '#{window_index} #{window_name} #{pane_left} #{pane_id}' 2>/dev/null | LC_ALL=C sort -k1,1n -k3,3n |
    while read -r _ w _ p; do [ "$(tmux -L "$s" display -p -t "$p" '#{@fleet-watch-status}')" = 1 ] && continue; n=$(jq -r --arg p "$p" 'to_entries[]|select(.value.pane==$p)|.key' "$st/roster.json" | head -1); printf '%s %s\n' "$w" "${n:-?}"; done |
    awk '{ if ($1!=last) { if (NR>1) printf " "; printf "%s:%s", $1, $2; last=$1 } else printf "|%s", $2 } END { print "" }'
}
dpanes() { tmux -L "$1" list-panes -s -t "=$2" -F '#{window_id}.#{pane_id}' 2>/dev/null | LC_ALL=C sort | tr '\n' ' '; }
o1=$(dsk up --no-proof --dir "$T" 2>&1); rc1=$?
lay1=$(dlayout desks-$$ dsess "$DS"); ids1=$(dpanes desks-$$ dsess)
o2=$(dsk up --no-proof --dir "$T" 2>&1); rc2=$?
lay2=$(dlayout desks-$$ dsess "$DS"); ids2=$(dpanes desks-$$ dsess)
t "desks up exits 0 twice" bash -c 'test "$1" = 0 && test "$2" = 0' _ "$rc1" "$rc2"
t "desks up: exactly windows deskA (ROUTE left | DECIDE right) then deskB (WATCH left | LEARN right)" test "$lay1" = "deskA:ROUTE|DECIDE deskB:WATCH|LEARN"
t "desks up: no separate WATCH, LEARN or desks window" bash -c '! tmux -L "$1" list-windows -t =dsess -F "#{window_name}" | grep -qxE "WATCH|LEARN|desks|DECIDE|ROUTE|home"' _ "desks-$$"
t "desks up second run: same layout" test "$lay2" = "$lay1"
t "desks up second run: same window and pane ids" test "$ids2" = "$ids1"
t "desks up second run: all four already live" bash -c 'for d in ROUTE DECIDE WATCH LEARN; do printf "%s" "$1" | grep -q "^$d already live" || exit 1; done' _ "$o2"
t "desks up second run launches nothing" hasnot "$o2" "OK launched"
widx() { tmux -L "$1" list-windows -t "=$2" -F '#{window_index}:#{window_name}' 2>/dev/null | tr '\n' ' '; }
t "desks up: fresh fleet puts deskA at index 0 and deskB at index 1 (got: $(widx desks-$$ dsess))" test "$(widx desks-$$ dsess)" = "0:deskA 1:deskB "
t "desks up second run: deskA and deskB already at their index" bash -c 'printf "%s" "$1" | grep -q "^window deskA already at index 0" && printf "%s" "$1" | grep -q "^window deskB already at index 1"' _ "$o2"
t "desks up: roster DECIDE role decide, ROUTE role route" bash -c 'test "$(jq -r .DECIDE.role "$1")" = decide && test "$(jq -r .ROUTE.role "$1")" = route' _ "$DS/roster.json"
t "desks up writes no alias map" test ! -e "$DS/aliases.json"
t "desks up: roster WATCH role watch, LEARN role learn, all four mode desk" jq -e '.WATCH.role=="watch" and .LEARN.role=="learn" and ([.DECIDE,.ROUTE,.WATCH,.LEARN]|all(.mode=="desk"))' "$DS/roster.json"
t "desks up: WATCH and LEARN got a read-only look-for-work brief" bash -c 'grep -q "Look for work, read-only" "$1/launch/WATCH.txt" && grep -q "Look for work, read-only" "$1/launch/LEARN.txt"' _ "$DS"
t "desks up: all four desk panes tagged @fleet-desk with their role" bash -c 'test "$(tmux -L "$1" list-panes -s -t =dsess -F "#{@fleet-watch-status}#{pane_title}=#{@fleet-desk}" | grep -v "^1" | LC_ALL=C sort | tr "\n" " ")" = "DECIDE=decide LEARN=learn ROUTE=route WATCH=watch "' _ "desks-$$"
wsp() { tmux -L "$1" list-panes -s -t "=$2" -F '#{window_name} #{pane_dead} #{@fleet-watch-status} #{pane_start_command}' 2>/dev/null | awk '$3==1'; }
ws1=$(wsp desks-$$ dsess)
t "desks up: exactly one WATCH status pane, live, in window deskB" bash -c 'test "$(printf "%s\n" "$1" | grep -c .)" = 1 && printf "%s" "$1" | grep -q "^deskB 0 1 "' _ "$ws1"
t "desks up: the WATCH status pane runs fleet watch status" bash -c 'printf "%s" "$1" | grep -q "watch status"' _ "$ws1"
t "control: window deskA has no WATCH status pane" bash -c '! printf "%s" "$1" | grep -q "^deskA "' _ "$ws1"
t "desks up second run: WATCH status pane left alone, still exactly one" bash -c 'printf "%s" "$1" | grep -q "no WATCH status pane" && test "$(printf "%s\n" "$2" | grep -c .)" = 1' _ "$o2" "$(wsp desks-$$ dsess)"
cl=$(FLEET_STATE=$DS FLEET_TMUX_SOCKET=desks-$$ FLEET_TMUX_SESSION=dsess "$FLEET" cleanup 2>&1)
t "cleanup does not call deskA or deskB an orphan" bash -c '! printf "%s" "$1" | grep -q ORPHAN-WINDOW' _ "$cl"
# half-dead pair: left desk gone, right desk live -> relaunch the left desk as the left pane of the same window
kp=$(jq -r .WATCH.pane "$DS/roster.json"); lp=$(jq -r .LEARN.pane "$DS/roster.json")
tmux -L desks-$$ kill-pane -t "$kp"
o3=$(dsk up --no-proof --dir "$T" 2>&1); rc3=$?
t "half-dead deskB: desks up exits 0" test "$rc3" = 0
t "half-dead deskB: LEARN already live, WATCH relaunched" bash -c 'printf "%s" "$1" | grep -q "^LEARN already live" && ! printf "%s" "$1" | grep -q "^WATCH already live"' _ "$o3"
t "half-dead deskB: back to deskA ROUTE|DECIDE, deskB WATCH|LEARN" test "$(dlayout desks-$$ dsess "$DS")" = "deskA:ROUTE|DECIDE deskB:WATCH|LEARN"
t "half-dead deskB: LEARN kept its pane id" test "$(jq -r .LEARN.pane "$DS/roster.json")" = "$lp"
t "half-dead deskB: WATCH relaunched, its status pane reused (still exactly one)" bash -c 'printf "%s" "$1" | grep -q "^WATCH status pane already live" && test "$(printf "%s\n" "$2" | grep -c .)" = 1' _ "$o3" "$(wsp desks-$$ dsess)"
t "half-dead deskB: WATCH has a new live pane" bash -c 'p=$(jq -r .WATCH.pane "$1"); test "$p" != "$2" && tmux -L "$3" list-panes -a -F "#{pane_id} #{pane_dead}" | grep -qx "$p 0"' _ "$DS/roster.json" "$kp" "desks-$$"
out=$(FLEET_ROLE=decide env FLEET_STATE=$DS "$FLEET" desks up --no-proof 2>&1)
t "desks up is role-guarded (FLEET_ROLE=decide refused)" has "$out" "FAIL role decide can't desks"
tmux -L desks-$$ kill-server >/dev/null 2>&1

# ---- desks up on a fleet whose deskA/deskB were built by hand (the live fleet): says already live, changes nothing
HB=$T/byhand; mkdir -p "$HB"
hb() { env FLEET_STATE=$HB FLEET_TMUX_SOCKET=byhand-$$ FLEET_TMUX_SESSION=hsess "$@"; }
tmux -L byhand-$$ new-session -d -s hsess -n deskA "sleep 300"
hR=$(tmux -L byhand-$$ display -p -t =hsess:deskA '#{pane_id}')
hD=$(tmux -L byhand-$$ split-window -d -h -P -F '#{pane_id}' -t "$hR" "sleep 300")
hW=$(tmux -L byhand-$$ new-window -d -P -F '#{pane_id}' -t =hsess: -n deskB "sleep 300")
hL=$(tmux -L byhand-$$ split-window -d -h -P -F '#{pane_id}' -t "$hW" "sleep 300")
jq -n --arg r "$hR" --arg d "$hD" --arg w "$hW" --arg l "$hL" '{ROUTE:{name:"ROUTE",cli:"claude",mode:"desk",role:"route",pane:$r},DECIDE:{name:"DECIDE",cli:"claude",mode:"desk",role:"decide",pane:$d},WATCH:{name:"WATCH",cli:"claude",mode:"desk",role:"watch",pane:$w},LEARN:{name:"LEARN",cli:"claude",mode:"desk",role:"learn",pane:$l}}' > "$HB/roster.json"
hids=$(dpanes byhand-$$ hsess); hros=$(jq -cS . "$HB/roster.json")
hopts=$(tmux -L byhand-$$ list-panes -s -t =hsess -F '#{pane_id}=#{@fleet-desk}' | tr '\n' ' ')
ho=$(hb "$FLEET" desks up --no-proof --dir "$T" 2>&1); hrc=$?
t "by hand: desks up exits 0" test "$hrc" = 0
t "by hand: all four already live" bash -c 'for d in ROUTE DECIDE WATCH LEARN; do printf "%s" "$1" | grep -q "^$d already live" || exit 1; done' _ "$ho"
t "by hand: window and pane ids unchanged" test "$(dpanes byhand-$$ hsess)" = "$hids"
t "by hand: roster unchanged" test "$(jq -cS . "$HB/roster.json")" = "$hros"
t "by hand: pane tags unchanged" test "$(tmux -L byhand-$$ list-panes -s -t =hsess -F '#{pane_id}=#{@fleet-desk}' | tr '\n' ' ')" = "$hopts"
t "control: by hand (WATCH already live, not launched): no WATCH status pane" bash -c '! tmux -L "$1" list-panes -s -t =hsess -F "#{@fleet-watch-status}" | grep -qx 1 && printf "%s" "$2" | grep -q "no WATCH status pane"' _ "byhand-$$" "$ho"
t "by hand: window names still deskA deskB" test "$(tmux -L byhand-$$ list-windows -t =hsess -F '#{window_name}' | tr '\n' ' ')" = "deskA deskB "
tmux -L byhand-$$ swap-pane -d -s "$hR" -t "$hD"
sw=$(dlayout byhand-$$ hsess "$HB")
t "control: dlayout reports a swapped deskA as DECIDE|ROUTE" test "$sw" = "deskA:DECIDE|ROUTE deskB:WATCH|LEARN"
tmux -L byhand-$$ kill-server >/dev/null 2>&1

# ---- desk slots (issue #9): deskA at index 0, deskB at 1, lanes at 2+, no pane or window id changes, focus kept
SL=$T/slots; mkdir -p "$SL"
sl() { env FLEET_STATE=$SL FLEET_TMUX_SOCKET=slots-$$ FLEET_TMUX_SESSION=ssess "$@"; }
stm() { tmux -L slots-$$ "$@"; }
# a fleet whose lanes took 0 and 1 and whose desks (live, built by hand) sit at 3 and 5
stm new-session -d -s ssess -n L0 "sleep 300"
sL0=$(stm display -p -t =ssess:L0 '#{window_id}')
sL1=$(stm new-window -d -P -F '#{window_id}' -t =ssess:1 -n L1 "sleep 300")
sR=$(stm new-window -d -P -F '#{pane_id}' -t =ssess:3 -n deskA "sleep 300")
sD=$(stm split-window -d -h -P -F '#{pane_id}' -t "$sR" "sleep 300")
sW=$(stm new-window -d -P -F '#{pane_id}' -t =ssess:5 -n deskB "sleep 300")
sLn=$(stm split-window -d -h -P -F '#{pane_id}' -t "$sW" "sleep 300")
jq -n --arg r "$sR" --arg d "$sD" --arg w "$sW" --arg l "$sLn" '{ROUTE:{name:"ROUTE",cli:"claude",mode:"desk",role:"route",pane:$r},DECIDE:{name:"DECIDE",cli:"claude",mode:"desk",role:"decide",pane:$d},WATCH:{name:"WATCH",cli:"claude",mode:"desk",role:"watch",pane:$w},LEARN:{name:"LEARN",cli:"claude",mode:"desk",role:"learn",pane:$l}}' > "$SL/roster.json"
stm select-window -t "$sL1"
sids=$(dpanes slots-$$ ssess); sros=$(jq -cS . "$SL/roster.json")
t "control: before desks up, lanes sit at 0 and 1 and the desks at 3 and 5" test "$(widx slots-$$ ssess)" = "0:L0 1:L1 3:deskA 5:deskB "
so=$(sl "$FLEET" desks up --no-proof --dir "$T" 2>&1); src=$?
t "slots: desks up exits 0" test "$src" = 0
t "slots: deskA moved to index 0, deskB to index 1 (got: $(widx slots-$$ ssess))" bash -c 'printf "%s" "$1" | grep -q "^0:deskA 1:deskB "' _ "$(widx slots-$$ ssess)"
t "slots: the lanes moved to index 2 or more (got: $(widx slots-$$ ssess))" bash -c 'for w in L0 L1; do i=$(printf "%s" "$1" | tr " " "\n" | grep ":$w$" | cut -d: -f1); [ -n "$i" ] && [ "$i" -ge 2 ] || exit 1; done' _ "$(widx slots-$$ ssess)"
t "slots: every window and pane id unchanged" test "$(dpanes slots-$$ ssess)" = "$sids"
t "slots: roster unchanged" test "$(jq -cS . "$SL/roster.json")" = "$sros"
t "slots: the current window is still L1" test "$(stm display -p -t =ssess: '#{window_id}')" = "$sL1"
t "slots: launched nothing, said it moved both" bash -c '! printf "%s" "$1" | grep -q "OK launched" && printf "%s" "$1" | grep -q "^OK window deskA moved to index 0" && printf "%s" "$1" | grep -q "^OK window deskB moved to index 1"' _ "$so"
sids2=$(dpanes slots-$$ ssess); swin2=$(widx slots-$$ ssess)
so2=$(sl "$FLEET" desks up --no-proof --dir "$T" 2>&1)
t "slots rerun: no-op (same indexes, same ids, nothing moved)" bash -c 'test "$1" = "$2" && test "$3" = "$4" && ! printf "%s" "$5" | grep -q "moved to index"' _ "$(widx slots-$$ ssess)" "$swin2" "$(dpanes slots-$$ ssess)" "$sids2" "$so2"
# deskB at 0, deskA at 1: both swap into place
stm swap-window -d -s "$(stm display -p -t "$sR" '#{window_id}')" -t "$(stm display -p -t "$sW" '#{window_id}')"
t "control: deskA and deskB swapped" bash -c 'printf "%s" "$1" | grep -q "^0:deskB 1:deskA "' _ "$(widx slots-$$ ssess)"
sl "$FLEET" desks up --no-proof --dir "$T" >/dev/null 2>&1
t "slots: swapped desks put back to 0:deskA 1:deskB" bash -c 'printf "%s" "$1" | grep -q "^0:deskA 1:deskB "' _ "$(widx slots-$$ ssess)"
# a lane launched on this fleet opens at the first free index from 2, even with 2 free below a busy 3
stm kill-window -t "$sL0"; stm kill-window -t "$sL1"; stm new-window -d -t =ssess:3 -n L3 "sleep 300"
sl "$FLEET" launch --no-proof --dir "$T" LN1 prompt "$T/prompt.txt" >/dev/null 2>&1
t "slots: a lane launch lands at index 2 (got: $(widx slots-$$ ssess))" bash -c 'printf "%s" "$1" | grep -q " 2:LN1 "' _ "$(widx slots-$$ ssess)"
sl "$FLEET" launch --no-proof --dir "$T" LN2 prompt "$T/prompt.txt" >/dev/null 2>&1
t "slots: the next lane skips the busy 3 and lands at 4 (got: $(widx slots-$$ ssess))" bash -c 'printf "%s" "$1" | grep -q " 4:LN2 "' _ "$(widx slots-$$ ssess)"
stm kill-server >/dev/null 2>&1
# no desks at all: a lane launch on a new session still leaves 0 and 1 free
NL=$T/nodesks; mkdir -p "$NL"
env FLEET_STATE=$NL FLEET_TMUX_SOCKET=nodesks-$$ FLEET_TMUX_SESSION=nsess "$FLEET" launch --no-proof --dir "$T" LZ prompt "$T/prompt.txt" >/dev/null 2>&1
t "no desks: the first lane opens at index 2, beside home at 0 (got: $(widx nodesks-$$ nsess))" test "$(widx nodesks-$$ nsess)" = "0:home 2:LZ "
tmux -L nodesks-$$ kill-server >/dev/null 2>&1

# ---- desks up --dry-run shows the two split windows and touches nothing
DR=$T/dry; mkdir -p "$DR"
dro=$(env FLEET_STATE=$DR FLEET_TMUX_SOCKET=dry-$$ FLEET_TMUX_SESSION=drsess "$FLEET" desks up --dry-run --no-proof --dir "$T" 2>&1); drc=$?
t "dry run: exits 0" test "$drc" = 0
t "dry run: names window deskA ROUTE left | DECIDE right" has "$dro" "window deskA: ROUTE (left) | DECIDE (right)"
t "dry run: names window deskB WATCH left | LEARN right" has "$dro" "window deskB: WATCH (left) | LEARN (right)"
t "dry run: DECIDE splits right of ROUTE, LEARN right of WATCH" bash -c 'printf "%s" "$1" | grep -q "would run:.*--split-right-of ROUTE DECIDE desk" && printf "%s" "$1" | grep -q "would run:.*--split-right-of WATCH LEARN desk"' _ "$dro"
t "dry run: shows the WATCH status pane under WATCH running fleet watch status" bash -c 'printf "%s" "$1" | grep -q "WATCH status pane under WATCH" && printf "%s" "$1" | grep -q "would run: tmux split-window.*watch\\\\ status"' _ "$dro"
t "dry run: shows deskA placed at index 0 and deskB at 1" bash -c 'printf "%s" "$1" | grep -q "^window deskA: placed at index 0" && printf "%s" "$1" | grep -q "^window deskB: placed at index 1"' _ "$dro"
t "dry run: no tmux server started, no roster" bash -c '! tmux -L "$1" has-session 2>/dev/null && test ! -e "$2/roster.json"' _ "dry-$$" "$DR"

# ---- desks up beside old-named ORCHESTRATOR and DECISIONS lanes (Q110 c): they keep their windows, panes, roster and mail
AD=$T/beside; mkdir -p "$AD"
ad() { env FLEET_STATE=$AD FLEET_TMUX_SOCKET=beside-$$ FLEET_TMUX_SESSION=asess "$@"; }
ad tmux -L beside-$$ new-session -d -s asess -n home "sleep 300"
pO=$(ad tmux -L beside-$$ new-window -d -P -F '#{pane_id}' -t =asess: -n ORCHESTRATOR "printf \"> \"; exec cat > $AD/o.keys")
pD=$(ad tmux -L beside-$$ new-window -d -P -F '#{pane_id}' -t =asess: -n DECISIONS "printf \"> \"; exec cat > $AD/d.keys")
jq -n --arg o "$pO" --arg d "$pD" '{ORCHESTRATOR:{name:"ORCHESTRATOR",cli:"claude",mode:"prompt",role:"orchestrator",pane:$o},DECISIONS:{name:"DECISIONS",cli:"claude",mode:"desk",role:"desk",pane:$d}}' > "$AD/roster.json"
old=$(jq -cS '{ORCHESTRATOR,DECISIONS}' "$AD/roster.json")
ad "$FLEET" desks up --no-proof --dir "$T" > "$AD/up.out" 2>&1; rc=$?  # as the human runs it (FLEET_ROLE unset)
t "beside: desks up exits 0" test "$rc" = 0
t "beside: roster entries ORCHESTRATOR and DECISIONS unchanged" test "$(jq -cS '{ORCHESTRATOR,DECISIONS}' "$AD/roster.json")" = "$old"
t "beside: roster now also has DECIDE ROUTE WATCH LEARN" jq -e 'keys==["DECIDE","DECISIONS","LEARN","ORCHESTRATOR","ROUTE","WATCH"]' "$AD/roster.json"
# deskB has 3 panes: WATCH | LEARN plus the `fleet watch status` pane desks up splits under a WATCH it launched
bw=$(tmux -L "beside-$$" list-windows -t =asess -F "#{window_name}:#{window_panes}" | LC_ALL=C sort | tr "\n" " ")
t "beside: windows ORCHESTRATOR DECISIONS kept, deskA deskB added (got: $bw)" test "$bw" = "DECISIONS:1 ORCHESTRATOR:1 deskA:2 deskB:3 home:1 "
t "beside: deskB's third pane is the WATCH status pane" bash -c 'test "$(tmux -L "$1" list-panes -t =asess:deskB -F "#{@fleet-watch-status}" | grep -cx 1)" = 1' _ "beside-$$"
t "beside: control: the ORCHESTRATOR and DECISIONS windows hold only their own panes" bash -c 'test "$(tmux -L "$1" list-panes -t =asess:ORCHESTRATOR -F "#{pane_id}")" = "$2" && test "$(tmux -L "$1" list-panes -t =asess:DECISIONS -F "#{pane_id}")" = "$3"' _ "beside-$$" "$pO" "$pD"
t "beside: ORCHESTRATOR and DECISIONS panes still alive on the same ids" bash -c 'tmux -L "$1" list-panes -a -F "#{pane_id} #{pane_dead}" | grep -qx "$2 0" && tmux -L "$1" list-panes -a -F "#{pane_id} #{pane_dead}" | grep -qx "$3 0"' _ "beside-$$" "$pO" "$pD"
sleep 1
t "beside: nothing typed into the ORCHESTRATOR or DECISIONS pane" bash -c 'test ! -s "$1/o.keys" && test ! -s "$1/d.keys"' _ "$AD"
ad python3 "$ROOT/lib/fleet_mesh.py" send X ORCHESTRATOR hi >/dev/null 2>&1
ad python3 "$ROOT/lib/fleet_mesh.py" send X DECISIONS hi2 >/dev/null 2>&1
ad python3 "$ROOT/lib/fleet_mesh.py" send X ROUTE hi3 >/dev/null 2>&1
t "beside: send to ORCHESTRATOR lands in inbox/ORCHESTRATOR.jsonl" test "$(wc -l < "$AD/inbox/ORCHESTRATOR.jsonl")" -eq 1
t "beside: send to DECISIONS lands in inbox/DECISIONS.jsonl" test "$(wc -l < "$AD/inbox/DECISIONS.jsonl")" -eq 1
t "beside: send to ROUTE lands in inbox/ROUTE.jsonl" test "$(wc -l < "$AD/inbox/ROUTE.jsonl")" -eq 1
sleep 1
t "beside: the live ORCHESTRATOR pane was still woken (typed pointer)" grep -q "message from X" "$AD/o.keys"
t "beside: no alias map" test ! -e "$AD/aliases.json"
ad FLEET_NAME=ORCHESTRATOR "$FLEET" send ROUTE "ping route" >/dev/null 2>&1
ad FLEET_NAME=ORCHESTRATOR "$FLEET" send DECIDE "ping decide" >/dev/null 2>&1
t "beside: fleet send ROUTE lands (second line in inbox/ROUTE.jsonl)" test "$(wc -l < "$AD/inbox/ROUTE.jsonl")" -eq 2
t "beside: fleet send DECIDE lands in inbox/DECIDE.jsonl" test "$(wc -l < "$AD/inbox/DECIDE.jsonl")" -eq 1
sleep 1
t "beside: the ROUTE pane was woken by fleet send" bash -c 'tmux -L "$1" capture-pane -p -t "$2" | grep -q "message from ORCHESTRATOR"' _ "beside-$$" "$(jq -r '.ROUTE.pane' "$AD/roster.json")"
# issue #34: the decision desk pane never gets typed keys; a tmux notice (@fleet_unread) replaces the pointer
pDd=$(jq -r '.DECIDE.pane' "$AD/roster.json")
t "beside: nothing typed into the DECIDE pane by fleet send" bash -c '! tmux -L "$1" capture-pane -p -t "$2" | grep -q "message from ORCHESTRATOR"' _ "beside-$$" "$pDd"
t "beside: the DECIDE pane got the @fleet_unread notice instead" bash -c 'test -n "$(tmux -L "$1" show-options -p -v -t "$2" @fleet_unread 2>/dev/null)"' _ "beside-$$" "$pDd"
tk=$(ad FLEET_ROLE=route "$FLEET" tick 2>/dev/null)
t "beside: tick shows no park for the old-role ORCHESTRATOR/DECISIONS or any desk" bash -c '! printf "%s\n" "$1" | grep -E "^(ORCHESTRATOR|DECISIONS|DECIDE|ROUTE|WATCH|LEARN) " | grep -q "park$"' _ "$tk"
t "control: tick lists the old-role entries" bash -c 'printf "%s\n" "$1" | grep -q "^ORCHESTRATOR .*orchestrator" && printf "%s\n" "$1" | grep -q "^DECISIONS .*desk"' _ "$tk"
cl=$(ad "$FLEET" cleanup 2>&1)
t "beside: cleanup calls no window an orphan" bash -c '! printf "%s" "$1" | grep -q ORPHAN' _ "$cl"
tmux -L beside-$$ kill-server >/dev/null 2>&1

# ---- installers write fleet-<role> for the new roles and no profile for the retired ones (throwaway HOME, no network)
IH=$T/insthome; mkdir -p "$IH/.local/share/opencode-goal-pro-max-complete-plugin"
printf '{"name":"fixture"}\n' > "$IH/.local/share/opencode-goal-pro-max-complete-plugin/package.json"
mkdir -p "$T/inststubs"  # test/stubs/codex is a lane stub that never exits; the installer only needs `codex mcp list|add`
printf '#!/bin/sh\nexit 0\n' > "$T/inststubs/codex"; chmod +x "$T/inststubs/codex"
for s in codex kiro opencode; do
  ( unset CODEX_HOME KIRO_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME FLEET_PREFIX
    export HOME=$IH PATH="$T/inststubs:$HERE/stubs:$PATH" FLEET_STATE=$IH/state FLEET_CODEX_BIN=codex
    bash "$ROOT/install/$s.sh" --prefix "$IH/.local" ) > "$T/inst-$s.out" 2>&1
done
for r in $OLD_ROLES; do
  t "installers wrote no fleet-$r profile (codex, kiro, opencode)" bash -c 'test ! -e "$1/.codex/fleet-$2.config.toml" && test ! -e "$1/.kiro/agents/fleet-$2.json" && ! jq -e --arg r "fleet-$2" ".agent|has(\$r)" "$1/.config/opencode/opencode.json" >/dev/null' _ "$IH" "$r"
done
for r in watch learn route decide worker; do
  t "install/codex.sh wrote fleet-$r.config.toml" cmp -s "$ROOT/roles/$r.codex.toml" "$IH/.codex/fleet-$r.config.toml"
  t "install/kiro.sh wrote agents/fleet-$r.json (name fleet-$r)" jq -e --arg n "fleet-$r" '.name==$n' "$IH/.kiro/agents/fleet-$r.json"
  t "install/opencode.sh merged agent fleet-$r" jq -e --arg r "fleet-$r" '.agent[$r].prompt | endswith("/roles/" + ($r|ltrimstr("fleet-")) + ".md}")' "$IH/.config/opencode/opencode.json"
done

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILS FAILED"; exit 1; fi
