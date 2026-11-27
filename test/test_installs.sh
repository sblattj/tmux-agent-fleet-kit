#!/usr/bin/env bash
# test_installs.sh: run each installer against a throwaway HOME with stubbed claude/codex/git on PATH.
# One PASS/FAIL line per assertion; exits 1 on any FAIL. Never touches the real HOME.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-inst.XXXXXX" | sed 's#//#/#g')
trap 'tmux -L insttest-$$ kill-server >/dev/null 2>&1; rm -rf "$tmp"' EXIT
fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
check() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (want '$3' got '$2')"; fi; }
count() { local n; n=$(grep -F -c -- "$1" "$2" 2>/dev/null || true); printf '%s' "${n:-0}"; }
nfiles() { find "$1" -mindepth 1 2>/dev/null | wc -l | tr -d ' '; }

command -v jq >/dev/null 2>&1 || { echo "FAIL jq is required for this test"; exit 1; }

# ---- stubs: log argv, fake just enough state
stubs=$tmp/stubs; mkdir -p "$stubs"
export STUB_LOG=$tmp/argv.log STUB_STATE=$tmp/stubstate
mkdir -p "$STUB_STATE"; : > "$STUB_LOG"
cat > "$stubs/claude" <<'EOF'
#!/usr/bin/env bash
echo "claude $*" >> "$STUB_LOG"
if [ "${1:-}" = mcp ] && [ "${2:-}" = list ]; then cat "$STUB_STATE/claude_mcp" 2>/dev/null; exit 0; fi
if [ "${1:-}" = mcp ] && [ "${2:-}" = add ]; then echo "fleet: python3 serve - connected" >> "$STUB_STATE/claude_mcp"; exit 0; fi
exit 0
EOF
cat > "$stubs/codex" <<'EOF'
#!/usr/bin/env bash
echo "codex $*" >> "$STUB_LOG"
if [ "${1:-}" = mcp ] && [ "${2:-}" = list ]; then cat "$STUB_STATE/codex_mcp" 2>/dev/null; exit 0; fi
if [ "${1:-}" = mcp ] && [ "${2:-}" = add ]; then
  echo "fleet  python3  enabled" >> "$STUB_STATE/codex_mcp"
  ch=${CODEX_HOME:-$HOME/.codex}; mkdir -p "$ch"
  printf '[mcp_servers.fleet]\ncommand = "python3"\nargs = ["x", "serve"]\n' >> "$ch/config.toml"
  exit 0
fi
exit 0
EOF
cat > "$stubs/git" <<'EOF'
#!/usr/bin/env bash
echo "git $*" >> "$STUB_LOG"
if [ "${1:-}" = clone ]; then
  last=""; for a in "$@"; do last=$a; done
  mkdir -p "$last/.git"; exit 0
fi
if [ "${1:-}" = -C ] && [ "${3:-}" = describe ]; then echo v1.5.3; exit 0; fi
exit 0
EOF
chmod +x "$stubs"/*

# run an installer with a given HOME; XDG vars unset so nothing can leak to the real HOME
inst() {
  local h=$1 script=$2; shift 2
  ( unset CODEX_HOME KIRO_HOME FLEET_AGY_HOME FLEET_AGY_BIN XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME FLEET_STATE FLEET_PREFIX
    if [ -n "${INST_CODEX_HOME:-}" ]; then export CODEX_HOME=$INST_CODEX_HOME; fi
    if [ -n "${INST_KIRO_HOME:-}" ]; then export KIRO_HOME=$INST_KIRO_HOME; fi
    if [ -n "${INST_AGY_HOME:-}" ]; then export FLEET_AGY_HOME=$INST_AGY_HOME; fi
    export HOME="$h" PATH="$stubs:$PATH" FLEET_CLAUDE_BIN=claude FLEET_CODEX_BIN=codex
    # issue #9: installers end with `fleet desks up`; never against the live fleet: scratch socket and state, and
    # skipped unless a test opts in (test_desks_default.sh covers the step)
    export FLEET_TMUX_SOCKET=insttest-$$ FLEET_TMUX_SESSION=instsess FLEET_NO_DESKS=1
    bash "$ROOT/install/$script" "$@" )
}
newhome() { local h=$tmp/home.$1; mkdir -p "$h"; printf '%s' "$h"; }

proj=$tmp/proj; mkdir -p "$proj"
projreal=$(cd "$proj" && pwd -P)

# =========================================================== claude.sh
h=$(newhome claude)
mkdir -p "$h/.claude"
printf '# my notes\nkeep this line\n' > "$h/.claude/CLAUDE.md"
export FLEET_HUMAN=Pat FLEET_DECIDE=DESKX FLEET_ROUTE=ORCHX
out=$(inst "$h" claude.sh --mesh --trust "$proj" --prefix "$h/.local" 2>&1); rc=$?
eq "claude.sh first run exits 0" "$rc" 0
md=$h/.claude/CLAUDE.md
eq "claude: begin marker once" "$(count 'fleet-rules:begin' "$md")" 1
eq "claude: end marker once" "$(count 'fleet-rules:end' "$md")" 1
check "claude: existing user text preserved" grep -q 'keep this line' "$md"
check "claude: placeholder HUMAN substituted" grep -q 'Pat' "$md"
check "claude: placeholder DESK substituted" grep -q 'DESKX' "$md"
check "claude: placeholder ORCHESTRATOR substituted" grep -q 'ORCHX' "$md"
eq "claude: no unreplaced {{ placeholders" "$(count '{{' "$md")" 0
for s in running-the-decisions-desk launching-a-goal-lane checking-a-peer-before-relaying retiring-the-fleet-into-handoffs; do
  check "claude: skill $s/SKILL.md copied" test -f "$h/.claude/skills/$s/SKILL.md"
done
check "claude: desk.sh copied and executable" test -x "$h/.claude/skills/running-the-decisions-desk/desk.sh"
check "claude: roster.sh copied and executable" test -x "$h/.claude/skills/retiring-the-fleet-into-handoffs/roster.sh"
eq "claude: bin/fleet symlink target" "$(readlink "$h/.local/bin/fleet")" "$ROOT/bin/fleet"
eq "claude: bin/tafk symlink (fleet under a second name)" "$(readlink "$h/.local/bin/tafk")" "$ROOT/bin/tafk"
eq "claude: bin/fleet-goal symlink target" "$(readlink "$h/.local/bin/fleet-goal")" "$ROOT/bin/fleet-goal"
check "claude: --mesh ran mcp add with the exact argv" grep -qF "claude mcp add --scope user fleet -- python3 $ROOT/lib/fleet_mesh.py serve" "$STUB_LOG"
check "claude: --trust wrote hasTrustDialogAccepted" jq -e --arg d "$projreal" '.projects[$d].hasTrustDialogAccepted == true' "$h/.claude.json"
bk=$(find "$h/.claude" -name 'CLAUDE.md.fleet-bak.*' | wc -l | tr -d ' ')
eq "claude: one backup of the pre-existing CLAUDE.md" "$bk" 1
before=$(cksum < "$md")
out=$(inst "$h" claude.sh --mesh --trust "$proj" --prefix "$h/.local" 2>&1); rc=$?
eq "claude.sh second run exits 0" "$rc" 0
eq "claude: second run leaves CLAUDE.md byte-identical" "$(cksum < "$md")" "$before"
eq "claude: markers still once after second run" "$(count 'fleet-rules:begin' "$md")" 1
eq "claude: mcp add ran exactly once across two runs" "$(count 'claude mcp add' "$STUB_LOG")" 1
eq "claude: no extra backup on second run" "$(find "$h/.claude" -name 'CLAUDE.md.fleet-bak.*' | wc -l | tr -d ' ')" 1
check "claude: second run reports unchanged" bash -c "printf '%s' \"\$1\" | grep -q unchanged" _ "$out"
FLEET_DECIDE=DESKY inst "$h" claude.sh --prefix "$h/.local" >/dev/null 2>&1
eq "claude: changed placeholder replaces the block (markers still once)" "$(count 'fleet-rules:begin' "$md")" 1
check "claude: replaced block carries the new value" grep -q 'DESKY' "$md"
eq "claude: replaced block dropped the old value" "$(count 'DESKX' "$md")" 0
check "claude: user text still preserved after replace" grep -q 'keep this line' "$md"

h=$(newhome claude-dry); : > "$STUB_STATE/claude_mcp"
out=$(inst "$h" claude.sh --dry-run --mesh --trust "$proj" --prefix "$h/.local" 2>&1); rc=$?
eq "claude --dry-run exits 0" "$rc" 0
eq "claude --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
check "claude --dry-run prints would-write lines" bash -c "printf '%s' \"\$1\" | grep -q 'would write'" _ "$out"
check "claude --dry-run prints the mcp command" bash -c "printf '%s' \"\$1\" | grep -q 'would run: claude mcp add'" _ "$out"
eq "claude --dry-run did not run mcp add again" "$(count 'claude mcp add' "$STUB_LOG")" 1

# =========================================================== codex.sh
h=$(newhome codex)
out=$(inst "$h" codex.sh --trust "$proj" --prefix "$h/.local" 2>&1); rc=$?
eq "codex.sh first run exits 0" "$rc" 0
md=$h/.codex/AGENTS.md; cfg=$h/.codex/config.toml
eq "codex: begin marker once" "$(count 'fleet-rules:begin' "$md")" 1
eq "codex: end marker once" "$(count 'fleet-rules:end' "$md")" 1
check "codex: placeholder substituted" grep -q 'DESKX' "$md"
check "codex: mcp add argv has --env FLEET_STATE and the server command" grep -qE "codex mcp add fleet --env FLEET_STATE=.*/fleet -- python3 $ROOT/lib/fleet_mesh.py serve" "$STUB_LOG"
eq "codex: env_vars forwarded once" "$(count 'env_vars = ["FLEET_NAME"]' "$cfg")" 1
check "codex: env_vars sits under [mcp_servers.fleet]" bash -c "awk '/^\\[/{s=(\$0==\"[mcp_servers.fleet]\")} s&&/env_vars/{f=1} END{exit !f}' \"\$1\"" _ "$cfg"
eq "codex: trust header once" "$(count "[projects.\"$projreal\"]" "$cfg")" 1
check "codex: trust_level trusted written" grep -q 'trust_level = "trusted"' "$cfg"
eq "codex: bin/fleet symlink" "$(readlink "$h/.local/bin/fleet")" "$ROOT/bin/fleet"
eq "codex: bin/tafk symlink (fleet under a second name)" "$(readlink "$h/.local/bin/tafk")" "$ROOT/bin/tafk"
before=$(cksum < "$cfg")
inst "$h" codex.sh --trust "$proj" --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "codex.sh second run exits 0" "$rc" 0
eq "codex: second run leaves config.toml byte-identical" "$(cksum < "$cfg")" "$before"
eq "codex: markers once after second run" "$(count 'fleet-rules:begin' "$md")" 1
eq "codex: mcp add ran exactly once across two runs" "$(count 'codex mcp add' "$STUB_LOG")" 1
eq "codex: env_vars still once after second run" "$(count 'env_vars = ["FLEET_NAME"]' "$cfg")" 1
eq "codex: trust header still once after second run" "$(count "[projects.\"$projreal\"]" "$cfg")" 1

h=$(newhome codex-dry)
: > "$STUB_STATE/codex_mcp"
out=$(inst "$h" codex.sh --dry-run --trust "$proj" --prefix "$h/.local" 2>&1); rc=$?
eq "codex --dry-run exits 0" "$rc" 0
eq "codex --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
check "codex --dry-run prints the mcp add command" bash -c "printf '%s' \"\$1\" | grep -q 'would run: codex mcp add fleet'" _ "$out"
eq "codex --dry-run did not run mcp add" "$(count 'codex mcp add' "$STUB_LOG")" 1

# =========================================================== opencode.sh
h=$(newhome opencode)
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode.sh first run exits 0" "$rc" 0
cdir=$h/.config/opencode; pdir=$h/.local/share/opencode-goal-pro-max-complete-plugin; cpdir=$h/.local/share/opencode-compact-at-token-limit
check "opencode: git clone pinned to v1.5.3 from the plugin repo" grep -qF "git clone --depth 1 --branch v1.5.3 https://github.com/sblattj/opencode-goal-pro-max-complete-plugin $pdir" "$STUB_LOG"
# One compactor: the compact plugin owns forced compaction, the goal plugin's compactAtTokens is 0.
# goal-plugin.js v1.5.3 forcedCompactionAllowed needs compactAtTokens > 0; plugin options seed every goal.
ONE_COMPACTOR='[.plugin[] | if type == "array" then . else [., {}] end
  | if (.[0] | test("opencode-goal-pro-max-complete-plugin")) then (.[1].compactAtTokens // 200000) != 0
    elif (.[0] | test("opencode-compact-at-token-limit")) then ((.[1].limit // 200000) | tostring) != "0"
    else false end | select(.)] | length == 1'
check "opencode: opencode.json lists the plugin once" jq -e --arg p "file://$pdir" '[.plugin[] | select(. == $p or (type == "array" and .[0] == $p))] | length == 1' "$cdir/opencode.json"
check "opencode: goal plugin entry turns its own forced compaction off" jq -e --arg p "file://$pdir" '.plugin[0] == [$p, {"compactAtTokens":0}]' "$cdir/opencode.json"
check "opencode: exactly one compactor is active in opencode.json" jq -e "$ONE_COMPACTOR" "$cdir/opencode.json"
check "opencode: compact plugin cloned at v0.1.2 from its repo" grep -qF "git clone --depth 1 --branch v0.1.2 https://github.com/sblattj/opencode-compact-at-token-limit $cpdir" "$STUB_LOG"
check "opencode: opencode.json lists the compact plugin once, after the goal plugin, with the default limit as an option" jq -e --arg g "file://$pdir" --arg c "file://$cpdir" '.plugin == [[$g, {"compactAtTokens":0}], [$c, {"limit":200000}]]' "$cdir/opencode.json"
check "opencode: tui.json does not list the compact plugin (server plugin only)" jq -e --arg c "file://$cpdir" '(.plugin | index($c)) == null' "$cdir/tui.json"
check "opencode: tui.json lists the plugin once, as a plain entry" jq -e --arg p "file://$pdir" '.plugin == [$p]' "$cdir/tui.json"
check "opencode: mcp.fleet entry is local, enabled, runs fleet_mesh.py serve" jq -e --arg m "$ROOT/lib/fleet_mesh.py" '.mcp.fleet == {"type":"local","command":["python3",$m,"serve"],"enabled":true}' "$cdir/opencode.json"
check "opencode: --bypass sets permission allow" jq -e '.permission == "allow"' "$cdir/opencode.json"
check "opencode: mcp.fleet-probe entry runs fleet_probe.py serve" jq -e --arg p "$ROOT/lib/fleet_probe.py" '.mcp["fleet-probe"] == {"type":"local","command":["python3",$p,"serve"],"enabled":true}' "$cdir/opencode.json"
check "opencode: fleet-probe tools off by default, on only in agent fleet-probe" jq -e '.tools["fleet-probe_*"] == false and .agent["fleet-probe"].tools["fleet-probe_*"] == true and (.agent["fleet-worker"].tools // {} | has("fleet-probe_*") | not)' "$cdir/opencode.json"
check "opencode: agent fleet-probe denies bash and edit, all other tools off" jq -e '.agent["fleet-probe"] | .tools["*"] == false and .tools.bash == false and .tools.write == false and .tools.edit == false and .permission.bash == {"*":"deny"} and .permission.edit == "deny"' "$cdir/opencode.json"
check "opencode: control: agent fleet-worker does not deny bash" jq -e '.agent["fleet-worker"].tools.bash != false' "$cdir/opencode.json"
# the global tools deny is overridden by permission "allow" (last match wins), so each agent but fleet-probe denies
# fleet-probe_* in its own permission; a live opencode 1.19 run showed the worker offered all 8 verbs without it
check "opencode: every fleet agent but fleet-probe denies fleet-probe_* itself" jq -e '[.agent | to_entries[] | select(.key | startswith("fleet-")) | select(.key != "fleet-probe") | .value.permission["fleet-probe_*"]] | length == 5 and all(. == "deny")' "$cdir/opencode.json"
check "opencode: built-in build, plan and general deny fleet-probe_*" jq -e '[.agent.build, .agent.plan, .agent.general | .permission["fleet-probe_*"]] == ["deny","deny","deny"]' "$cdir/opencode.json"
check "opencode: control: agent fleet-probe does not deny its own verbs" jq -e '.agent["fleet-probe"].permission | has("fleet-probe_*") | not' "$cdir/opencode.json"
check "opencode: the worker keeps its bash deny map next to the probe deny" jq -e '.agent["fleet-worker"].permission.bash["fleet park*"] == "deny"' "$cdir/opencode.json"
eq "opencode: AGENTS.md begin marker once" "$(count 'fleet-rules:begin' "$cdir/AGENTS.md")" 1
eq "opencode: AGENTS.md end marker once" "$(count 'fleet-rules:end' "$cdir/AGENTS.md")" 1
eq "opencode: bin/fleet-goal symlink" "$(readlink "$h/.local/bin/fleet-goal")" "$ROOT/bin/fleet-goal"
eq "opencode: bin/tafk symlink (fleet under a second name)" "$(readlink "$h/.local/bin/tafk")" "$ROOT/bin/tafk"
before=$(cksum < "$cdir/opencode.json")
inst "$h" opencode.sh --bypass --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "opencode.sh second run exits 0" "$rc" 0
eq "opencode: second run leaves opencode.json byte-identical" "$(cksum < "$cdir/opencode.json")" "$before"
check "opencode: plugin array still has two entries" jq -e '.plugin | length == 2' "$cdir/opencode.json"
eq "opencode: git clone ran exactly twice (goal + compact) across two runs" "$(count 'git clone' "$STUB_LOG")" 2
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1)
check "opencode: a rerun says the compact plugin is unchanged" bash -c "printf '%s' \"\$1\" | grep -q 'unchanged: compact plugin already cloned'" _ "$out"
eq "opencode: AGENTS.md markers once after second run" "$(count 'fleet-rules:begin' "$cdir/AGENTS.md")" 1

h=$(newhome opencode-nobypass)
printf '{"theme":"x","plugin":["other"]}\n' > /dev/null
mkdir -p "$h/.config/opencode"; printf '{"theme":"x","plugin":["other"],"agent":{"mine":{"description":"u","permission":"ask"}}}\n' > "$h/.config/opencode/opencode.json"
FLEET_OC_GOAL_PLUGIN_REF=v9.9.9 FLEET_OC_GOAL_PLUGIN_URL=https://example.invalid/p.git FLEET_OC_COMPACT_PLUGIN_REF=v8.8.8 FLEET_OC_COMPACT_PLUGIN_URL=https://example.invalid/c.git inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "opencode (no --bypass, env overrides) exits 0" "$rc" 0
check "opencode: no permission key without --bypass" jq -e 'has("permission") | not' "$h/.config/opencode/opencode.json"
check "opencode: a user agent's string permission becomes {*: it} plus the probe deny" jq -e '.agent.mine == {"description":"u","permission":{"*":"ask","fleet-probe_*":"deny"}}' "$h/.config/opencode/opencode.json"
check "opencode: existing keys preserved, plugin appended" jq -e '.theme == "x" and .plugin[0] == "other" and (.plugin | length) == 3' "$h/.config/opencode/opencode.json"
check "opencode: ref and url overrides honored" grep -qF "git clone --depth 1 --branch v9.9.9 https://example.invalid/p.git" "$STUB_LOG"
check "opencode: compact ref and url overrides honored" grep -qF "git clone --depth 1 --branch v8.8.8 https://example.invalid/c.git" "$STUB_LOG"

# FLEET_OC_COMPACT_LIMIT writes the tuple form [ref, {limit}] in place of the plain entry
h=$(newhome opencode-limit); oj=$h/.config/opencode/opencode.json; cref=file://$h/.local/share/opencode-compact-at-token-limit
inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
FLEET_OC_COMPACT_LIMIT=5k inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "opencode (FLEET_OC_COMPACT_LIMIT=5k) exits 0" "$rc" 0
check "opencode: limit replaces the default in the same slot" jq -e --arg c "$cref" '(.plugin | length) == 2 and .plugin[1] == [$c, {"limit":"5k"}]' "$oj"
check "opencode (limit 5k): exactly one compactor is active" jq -e "$ONE_COMPACTOR" "$oj"
before=$(cksum < "$oj")
FLEET_OC_COMPACT_LIMIT=5k inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
eq "opencode: same limit again leaves opencode.json byte-identical" "$(cksum < "$oj")" "$before"
FLEET_OC_COMPACT_LIMIT=150000 inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
check "opencode: a new numeric limit replaces the tuple in place, as a number" jq -e --arg c "$cref" '(.plugin | length) == 2 and .plugin[1] == [$c, {"limit":150000}]' "$oj"
inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
check "opencode: no FLEET_OC_COMPACT_LIMIT keeps an existing tuple" jq -e --arg c "$cref" '.plugin[1] == [$c, {"limit":150000}]' "$oj"
before=$(cksum < "$oj")
out=$(FLEET_OC_COMPACT_LIMIT=lots inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode: invalid FLEET_OC_COMPACT_LIMIT exits 1" "$rc" 1
check "opencode: invalid limit names the variable" bash -c "printf '%s' \"\$1\" | grep -q 'FLEET_OC_COMPACT_LIMIT'" _ "$out"
eq "opencode: invalid limit left opencode.json alone" "$(cksum < "$oj")" "$before"
FLEET_OC_COMPACT_LIMIT=0 inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
check "opencode: FLEET_OC_COMPACT_LIMIT=0 turns forced compaction off in both plugins" jq -e --arg c "$cref" '.plugin[1] == [$c, {"limit":0}] and .plugin[0][1].compactAtTokens == 0' "$oj"

# an older install (plain entries) or a hand-made goal tuple: options kept, goal compaction forced off,
# compact limit written, so OPENCODE_COMPACT_AT_TOKENS in the tmux server env never decides the limit
h=$(newhome opencode-upgrade); oj=$h/.config/opencode/opencode.json
g=file://$h/.local/share/opencode-goal-pro-max-complete-plugin; c=file://$h/.local/share/opencode-compact-at-token-limit
mkdir -p "$h/.config/opencode"
jq -n --arg g "$g" --arg c "$c" '{plugin: [[$g, {"maxTurns":5, "compactAtTokens":150000}], $c]}' > "$oj"
inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "opencode (upgrade) exits 0" "$rc" 0
check "opencode (upgrade): goal tuple keeps its options and gets compactAtTokens 0" jq -e --arg g "$g" '.plugin[0] == [$g, {"maxTurns":5, "compactAtTokens":0}]' "$oj"
check "opencode (upgrade): plain compact entry gets the default limit option" jq -e --arg c "$c" '.plugin[1] == [$c, {"limit":200000}] and (.plugin | length) == 2' "$oj"
check "opencode (upgrade): exactly one compactor is active" jq -e "$ONE_COMPACTOR" "$oj"
jq -n --arg g "$g" --arg c "$c" '{plugin: [$g, $c]}' > "$oj"
check "control: the old plain-entry config has two compactors (the check can fail)" bash -c '! jq -e "$1" "$2"' _ "$ONE_COMPACTOR" "$oj"

# plugin dir filled by the plugin's own installer (package.json, no .git): reuse it, never clone over it
h=$(newhome opencode-preinst)
mkdir -p "$h/.local/share/opencode-goal-pro-max-complete-plugin"; echo '{}' > "$h/.local/share/opencode-goal-pro-max-complete-plugin/package.json"
mkdir -p "$h/.local/share/opencode-compact-at-token-limit"; echo '{}' > "$h/.local/share/opencode-compact-at-token-limit/package.json"
n=$(count 'git clone' "$STUB_LOG")
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode (pre-installed plugin dir) exits 0" "$rc" 0
eq "opencode (pre-installed plugin dir) ran no git clone" "$(count 'git clone' "$STUB_LOG")" "$n"
check "opencode (pre-installed plugin dir) says already installed" bash -c "printf '%s' \"\$1\" | grep -q 'already installed'" _ "$out"
check "opencode (pre-installed plugin dir) says the compact plugin is already installed" bash -c "printf '%s' \"\$1\" | grep -q 'unchanged: compact plugin already installed'" _ "$out"
check "opencode (pre-installed plugin dir) still lists both plugins" jq -e '.plugin | length == 2' "$h/.config/opencode/opencode.json"
# non-empty dir with no plugin: fail loud, no clone
h=$(newhome opencode-junk)
mkdir -p "$h/.local/share/opencode-goal-pro-max-complete-plugin"; echo x > "$h/.local/share/opencode-goal-pro-max-complete-plugin/junk"
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode (junk plugin dir) exits 1" "$rc" 1
check "opencode (junk plugin dir) names the dir" bash -c "printf '%s' \"\$1\" | grep -q 'holds no plugin'" _ "$out"
eq "opencode (junk plugin dir) ran no git clone" "$(count 'git clone' "$STUB_LOG")" "$n"
h=$(newhome opencode-junk-compact)
mkdir -p "$h/.local/share/opencode-compact-at-token-limit"; echo x > "$h/.local/share/opencode-compact-at-token-limit/junk"
n=$(count 'git clone' "$STUB_LOG")
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode (junk compact plugin dir) exits 1" "$rc" 1
check "opencode (junk compact plugin dir) names the dir" bash -c "printf '%s' \"\$1\" | grep -q 'opencode-compact-at-token-limit exists, is not empty and holds no plugin'" _ "$out"
eq "opencode (junk compact plugin dir) cloned only the goal plugin" "$(count 'git clone' "$STUB_LOG")" "$((n + 1))"

h=$(newhome opencode-dry)
n=$(count 'git clone' "$STUB_LOG")
out=$(inst "$h" opencode.sh --dry-run --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode --dry-run exits 0" "$rc" 0
eq "opencode --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
eq "opencode --dry-run ran no git clone" "$(count 'git clone' "$STUB_LOG")" "$n"
check "opencode --dry-run prints would-write lines" bash -c "printf '%s' \"\$1\" | grep -q 'would write'" _ "$out"
check "opencode --dry-run prints the compact plugin clone" bash -c "printf '%s' \"\$1\" | grep -q 'would run: git clone --depth 1 --branch v0.1.2 https://github.com/sblattj/opencode-compact-at-token-limit'" _ "$out"
out=$(inst "$h" opencode.sh --help 2>&1)
check "opencode --help names FLEET_OC_COMPACT_LIMIT" bash -c "printf '%s' \"\$1\" | grep -q 'FLEET_OC_COMPACT_LIMIT'" _ "$out"

# =========================================================== role installs (fixture roles dir)
roles=$tmp/roles; mkdir -p "$roles"
printf 'developer_instructions = "alpha role"\n' > "$roles/alpha.codex.toml"
printf 'developer_instructions = "beta role"\n[mcp_servers.fleet]\nenabled_tools = ["peer_send"]\n' > "$roles/beta.codex.toml"
printf '{"description":"alpha","prompt":"{file:./roles/alpha.md}","tools":{"write":false}}\n' > "$roles/alpha.opencode.json"
printf '{"description":"beta","prompt":"{file:./roles/beta.md}","permission":{"bash":{"fleet launch*":"deny"}}}\n' > "$roles/beta.opencode.json"
printf 'alpha prompt\n' > "$roles/alpha.md"; printf 'beta prompt\n' > "$roles/beta.md"

h=$(newhome roles-codex); empty=$tmp/emptyroles; mkdir -p "$empty"
out=$(FLEET_ROLES_DIR="$empty" inst "$h" codex.sh --prefix "$h/.local" 2>&1); rc=$?
eq "codex roles: no role files exits 0" "$rc" 0
check "codex roles: prints 'no role profiles found'" bash -c "printf '%s' \"\$1\" | grep -q 'no role profiles found'" _ "$out"
check "codex roles: no fleet-*.config.toml written when none exist" bash -c "! ls \"\$1\"/.codex/fleet-*.config.toml >/dev/null 2>&1" _ "$h"
out=$(FLEET_ROLES_DIR="$roles" inst "$h" codex.sh --prefix "$h/.local" 2>&1); rc=$?
eq "codex roles: with fixtures exits 0" "$rc" 0
check "codex roles: fleet-alpha.config.toml is a copy of the role file" cmp -s "$roles/alpha.codex.toml" "$h/.codex/fleet-alpha.config.toml"
check "codex roles: fleet-beta.config.toml is a copy of the role file" cmp -s "$roles/beta.codex.toml" "$h/.codex/fleet-beta.config.toml"
before=$(cat "$h/.codex/fleet-alpha.config.toml" "$h/.codex/fleet-beta.config.toml" | cksum)
out=$(FLEET_ROLES_DIR="$roles" inst "$h" codex.sh --prefix "$h/.local" 2>&1)
eq "codex roles: second run byte-identical" "$(cat "$h/.codex/fleet-alpha.config.toml" "$h/.codex/fleet-beta.config.toml" | cksum)" "$before"
eq "codex roles: no backup of an unchanged profile" "$(find "$h/.codex" -name 'fleet-*.fleet-bak.*' | wc -l | tr -d ' ')" 0
h=$(newhome roles-codex-home); mkdir -p "$tmp/chome"
INST_CODEX_HOME=$tmp/chome FLEET_ROLES_DIR="$roles" inst "$h" codex.sh --prefix "$h/.local" >/dev/null 2>&1
check "codex roles: CODEX_HOME is honored for profiles" test -f "$tmp/chome/fleet-alpha.config.toml"
check "codex: CODEX_HOME is honored for the rules block" grep -q 'fleet-rules:begin' "$tmp/chome/AGENTS.md"
check "codex: CODEX_HOME set, nothing written to HOME/.codex" test ! -e "$h/.codex"
INST_CODEX_HOME=$tmp/chome FLEET_ROLES_DIR="$roles" inst "$h" codex.sh --prefix "$h/.local" --trust "$proj" >/dev/null 2>&1
check "codex: CODEX_HOME is honored for --trust" grep -qF "[projects.\"$projreal\"]" "$tmp/chome/config.toml"
check "codex: CODEX_HOME set, --trust wrote nothing to HOME/.codex" test ! -e "$h/.codex"
h=$(newhome roles-codex-dry)
out=$(FLEET_ROLES_DIR="$roles" inst "$h" codex.sh --dry-run --prefix "$h/.local" 2>&1)
eq "codex roles: --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
check "codex roles: --dry-run says would write the profile" bash -c "printf '%s' \"\$1\" | grep -q 'would write: codex role profile fleet-alpha'" _ "$out"

h=$(newhome roles-oc)
out=$(FLEET_ROLES_DIR="$roles" inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode roles: first run exits 0" "$rc" 0
oj=$h/.config/opencode/opencode.json
check "opencode roles: agent fleet-alpha merged with absolute prompt path" jq -e --arg p "{file:$roles/alpha.md}" '.agent["fleet-alpha"].prompt == $p and .agent["fleet-alpha"].tools.write == false' "$oj"
check "opencode roles: agent fleet-beta merged" jq -e --arg p "{file:$roles/beta.md}" '.agent["fleet-beta"].prompt == $p and .agent["fleet-beta"].permission.bash["fleet launch*"] == "deny"' "$oj"
check "opencode roles: relative ./roles path is gone" bash -c "! grep -q '{file:./roles' \"\$1\"" _ "$oj"
check "opencode roles: mcp and plugin entries survive the merge" jq -e '.mcp.fleet.enabled == true and (.plugin | length) == 2' "$oj"
before=$(cksum < "$oj")
FLEET_ROLES_DIR="$roles" inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
eq "opencode roles: second run byte-identical" "$(cksum < "$oj")" "$before"
h=$(newhome roles-oc-dry)
FLEET_ROLES_DIR="$roles" inst "$h" opencode.sh --dry-run --prefix "$h/.local" >/dev/null 2>&1
eq "opencode roles: --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
h=$(newhome roles-oc-empty)
out=$(FLEET_ROLES_DIR="$empty" inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode roles: empty roles dir exits 0" "$rc" 0
check "opencode roles: empty roles dir adds no fleet-* agent" jq -e '(.agent // {}) | keys | map(select(startswith("fleet-"))) == []' "$h/.config/opencode/opencode.json"
check "opencode roles: empty roles dir still denies fleet-probe_* on build, plan and general" jq -e '.agent | keys == ["build","general","plan"] and all(.[]; .permission["fleet-probe_*"] == "deny")' "$h/.config/opencode/opencode.json"

# upgrade from v0.1.x: agents of renamed roles (desk/orchestrator/listener) point at deleted roles/*.md files,
# which makes OpenCode reject the whole config. The installer drops them and checks every {file:} it leaves.
h=$(newhome roles-oc-upgrade); oj=$h/.config/opencode/opencode.json; mkdir -p "$(dirname "$oj")"
gone=$tmp/oldcheckout/roles
printf '{"agent":{"fleet-desk":{"prompt":"{file:%s/desk.md}"},"fleet-orchestrator":{"prompt":"{file:%s/orchestrator.md}"},"fleet-listener":{"prompt":"{file:%s/listener.md}"},"mine":{"prompt":"{file:/nowhere/mine.md}"}}}\n' "$gone" "$gone" "$gone" > "$oj"
out=$(inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode upgrade: real roles dir over a v0.1.x config exits 0" "$rc" 0
check "opencode upgrade: retired agents fleet-desk/orchestrator/listener are gone" jq -e '.agent | has("fleet-desk") or has("fleet-orchestrator") or has("fleet-listener") | not' "$oj"
check "opencode upgrade: no prompt still points at desk.md" bash -c "! grep -q 'desk.md' \"\$1\"" _ "$oj"
check "opencode upgrade: the install says which agents it retired" bash -c "printf '%s' \"\$1\" | grep -q 'retired opencode agents fleet-desk, fleet-listener, fleet-orchestrator'" _ "$out"
check "opencode upgrade: new role agents decide/route/watch point at existing files" jq -e --arg d "$ROOT/roles" '[.agent["fleet-decide","fleet-route","fleet-watch"].prompt] == ["{file:\($d)/decide.md}","{file:\($d)/route.md}","{file:\($d)/watch.md}"]' "$oj"
check "opencode upgrade: control: a non-fleet agent is left alone, even with a bad path" jq -e '.agent.mine.prompt == "{file:/nowhere/mine.md}"' "$oj"
check "opencode upgrade: the pre-upgrade config was backed up" bash -c "grep -l 'fleet-desk' \"\$1\".fleet-bak.* >/dev/null" _ "$oj"
before=$(cksum < "$oj")
out=$(inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode upgrade: a rerun exits 0" "$rc" 0
eq "opencode upgrade: a rerun is byte-identical" "$(cksum < "$oj")" "$before"
check "opencode upgrade: a rerun retires nothing" bash -c "! printf '%s' \"\$1\" | grep -q retired" _ "$out"
h=$(newhome roles-oc-upgrade-dry); oj=$h/.config/opencode/opencode.json; mkdir -p "$(dirname "$oj")"
printf '{"agent":{"fleet-desk":{"prompt":"{file:%s/desk.md}"}}}\n' "$gone" > "$oj"; before=$(cksum < "$oj")
out=$(inst "$h" opencode.sh --dry-run --prefix "$h/.local" 2>&1); rc=$?
eq "opencode upgrade --dry-run exits 0" "$rc" 0
eq "opencode upgrade --dry-run leaves the old config untouched" "$(cksum < "$oj")" "$before"
check "opencode upgrade --dry-run says it would retire fleet-desk" bash -c "printf '%s' \"\$1\" | grep -q 'would write: retired opencode agents fleet-desk'" _ "$out"
# fail loudly: a {file:} the installer would write that does not exist
badroles=$tmp/badroles; mkdir -p "$badroles"
printf '{"description":"gamma","prompt":"{file:./roles/gamma.md}"}\n' > "$badroles/gamma.opencode.json"
h=$(newhome roles-oc-badref)
out=$(FLEET_ROLES_DIR="$badroles" inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
check "opencode roles: a role prompt file that does not exist fails the install" test "$rc" -ne 0
check "opencode roles: the failure names the missing prompt file" bash -c "printf '%s' \"\$1\" | grep -q 'FAIL.*gamma.md does not exist'" _ "$out"
check "opencode roles: the bad agent was not written" bash -c "! jq -e '.agent[\"fleet-gamma\"]' \"\$1\" >/dev/null 2>&1" _ "$h/.config/opencode/opencode.json"
printf 'gamma prompt\n' > "$badroles/gamma.md"
out=$(FLEET_ROLES_DIR="$badroles" inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode roles: control: the same role with its prompt file present installs" "$rc" 0
# fail loudly: a fleet agent outside the retired list whose prompt file is gone
h=$(newhome roles-oc-leftover); oj=$h/.config/opencode/opencode.json; mkdir -p "$(dirname "$oj")"
printf '{"agent":{"fleet-old":{"prompt":"{file:%s/old.md}"}}}\n' "$gone" > "$oj"
out=$(FLEET_ROLES_DIR="$roles" inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
check "opencode roles: a fleet agent left with a missing {file:} fails the install" test "$rc" -ne 0
check "opencode roles: the failure names the agent and the path" bash -c "printf '%s' \"\$1\" | grep -qF 'agent fleet-old: {file:$gone/old.md}'" _ "$out"

# kiro, agy and codex keep one file per role: an upgrade retires the old names, other roles stay
h=$(newhome roles-retire); kd=$h/.kiro/agents; ad=$h/.gemini/config/agents; cd_=$h/.codex
mkdir -p "$kd" "$ad" "$cd_"
for r in desk orchestrator listener; do
  echo '{}' > "$kd/fleet-$r.json"; printf -- '---\nname: fleet-%s\n---\n' "$r" > "$ad/fleet-$r.md"; echo 'x = 1' > "$cd_/fleet-$r.config.toml"
done
echo '{"keep":1}' > "$kd/mine.json"
out=$(inst "$h" kiro.sh --dry-run --prefix "$h/.local" 2>&1)
check "kiro upgrade --dry-run says it would retire fleet-desk" bash -c "printf '%s' \"\$1\" | grep -q 'would retire: stale kiro agent fleet-desk'" _ "$out"
check "kiro upgrade --dry-run leaves fleet-desk.json in place" test -f "$kd/fleet-desk.json"
inst "$h" kiro.sh --prefix "$h/.local" >/dev/null 2>&1
inst "$h" agy.sh --prefix "$h/.local" >/dev/null 2>&1
inst "$h" codex.sh --prefix "$h/.local" >/dev/null 2>&1
for r in desk orchestrator listener; do
  check "kiro upgrade: fleet-$r.json retired" test ! -e "$kd/fleet-$r.json"
  check "agy upgrade: fleet-$r.md retired" test ! -e "$ad/fleet-$r.md"
  check "codex upgrade: fleet-$r.config.toml retired" test ! -e "$cd_/fleet-$r.config.toml"
done
eq "upgrade: every retired file kept as a .fleet-bak copy" "$(find "$kd" "$ad" "$cd_" -name 'fleet-desk*.fleet-bak.*' -o -name 'fleet-orchestrator*.fleet-bak.*' -o -name 'fleet-listener*.fleet-bak.*' | wc -l | tr -d ' ')" 9
check "kiro upgrade: control: fleet-decide.json installed" test -f "$kd/fleet-decide.json"
check "agy upgrade: control: fleet-route.md installed" test -f "$ad/fleet-route.md"
check "codex upgrade: control: fleet-watch.config.toml installed" test -f "$cd_/fleet-watch.config.toml"
check "kiro upgrade: control: a non-fleet agent file is left alone" test -f "$kd/mine.json"

h=$(newhome roles-claude)
out=$(inst "$h" claude.sh --help 2>&1)
check "claude roles: --help says no role install is needed" bash -c "printf '%s' \"\$1\" | grep -q 'installs no role files'" _ "$out"
check "claude roles: the installer writes no fleet-* role files" bash -c "! find \"\$1\" -name 'fleet-*' | grep -q ." _ "$h"

# =========================================================== kiro.sh
h=$(newhome kiro); kh=$h/.kiro
mkdir -p "$kh/settings"
printf '{"chat.defaultModel":"keep-me"}\n' > "$kh/settings/cli.json"
printf '{"mcpServers":{"other":{"url":"http://127.0.0.1:1/mcp"}}}\n' > "$kh/settings/mcp.json"
out=$(inst "$h" kiro.sh --prefix "$h/.local" 2>&1); rc=$?
eq "kiro.sh first run exits 0" "$rc" 0
check "kiro: cli.json disables the trust-all confirmation, keeps other keys" jq -e '."chat.disableTrustAllConfirmation" == true and ."chat.defaultModel" == "keep-me"' "$kh/settings/cli.json"
check "kiro: the install prints the trust-all note" bash -c "printf '%s' \"\$1\" | grep -q 'chat.disableTrustAllConfirmation=true'" _ "$out"
check "kiro: mcp.json fleet server is python3 <repo>/lib/fleet_mesh.py serve" jq -e --arg m "$ROOT/lib/fleet_mesh.py" '.mcpServers.fleet == {"command":"python3","args":[$m,"serve"]}' "$kh/settings/mcp.json"
check "kiro: existing mcp server preserved" jq -e '.mcpServers.other.url == "http://127.0.0.1:1/mcp"' "$kh/settings/mcp.json"
eq "kiro: steering begin marker once" "$(count 'fleet-rules:begin' "$kh/steering/fleet-rules.md")" 1
eq "kiro: steering has no unreplaced placeholders" "$(count '{{' "$kh/steering/fleet-rules.md")" 0
check "kiro: steering placeholder substituted" grep -q 'DESKY\|DESKX' "$kh/steering/fleet-rules.md"
for r in route decide watch worker; do
  af=$kh/agents/fleet-$r.json
  check "kiro: agent fleet-$r valid JSON named fleet-$r" jq -e --arg n "fleet-$r" '.name == $n and .tools == ["*"] and .includeMcpJson == true' "$af"
  check "kiro: agent fleet-$r prompt is the inline text of roles/$r.md" jq -e --rawfile p "$ROOT/roles/$r.md" '.prompt == $p and (.prompt | startswith("file:") | not)' "$af"
done
for r in worker decide watch; do
  for v in launch park stop reopen; do
    check "kiro: fleet-$r denies shell 'fleet $v*'" jq -e --arg g "fleet $v*" '[.permissions.rules[] | select(.capability=="shell" and .effect=="deny" and (.match | index($g)))] | length == 1' "$kh/agents/fleet-$r.json"
  done
done
for r in route decide watch; do
  check "kiro: fleet-$r denies fs_write **" jq -e '[.permissions.rules[] | select(.capability=="fs_write" and .effect=="deny" and .match == ["**"])] | length == 1' "$kh/agents/fleet-$r.json"
  check "kiro: fleet-$r denies git commit and git push" jq -e '([.permissions.rules[] | select(.capability=="shell" and .effect=="deny") | .match[]] | index("git commit*") != null and index("git push*") != null)' "$kh/agents/fleet-$r.json"
done
check "kiro: worker has no fs_write deny" jq -e '[.permissions.rules[] | select(.capability=="fs_write")] | length == 0' "$kh/agents/fleet-worker.json"
check "kiro: route does not deny fleet launch" jq -e '[.permissions.rules[].match[]] | index("fleet launch*") == null' "$kh/agents/fleet-route.json"
pf=$kh/agents/fleet-probe.json
check "kiro: agent fleet-probe: no shell or write tool, mcp.json not inherited" jq -e '.name == "fleet-probe" and .includeMcpJson == false and (.tools | index("*") == null and index("shell") == null and index("fs_write") == null)' "$pf"
check "kiro: agent fleet-probe carries only the fleet + fleet-probe servers" jq -e --arg m "$ROOT/lib/fleet_mesh.py" --arg p "$ROOT/lib/fleet_probe.py" '.mcpServers == {"fleet":{"command":"python3","args":[$m,"serve"]},"fleet-probe":{"command":"python3","args":[$p,"serve"]}}' "$pf"
check "kiro: agent fleet-probe prompt is the inline text of roles/probe.md" jq -e --rawfile t "$ROOT/roles/probe.md" '.prompt == $t' "$pf"
check "kiro: control: fleet-worker gets no fleet-probe server" jq -e '(.mcpServers // {}) | has("fleet-probe") | not' "$kh/agents/fleet-worker.json"
eq "kiro: bin/fleet symlink" "$(readlink "$h/.local/bin/fleet")" "$ROOT/bin/fleet"
eq "kiro: bin/tafk symlink (fleet under a second name)" "$(readlink "$h/.local/bin/tafk")" "$ROOT/bin/tafk"
# CONTROL: the deny assertion must fail on an agent whose deny rule was stripped
jq '.permissions.rules |= map(select((.match // []) | index("fleet park*") | not))' "$kh/agents/fleet-worker.json" > "$tmp/stripped-agent.json"
if jq -e '[.permissions.rules[] | select(.capability=="shell" and .effect=="deny" and (.match | index("fleet park*")))] | length == 1' "$tmp/stripped-agent.json" >/dev/null 2>&1; then fail "control: kiro deny assertion catches an agent missing the fleet park rule"; else pass "control: kiro deny assertion catches an agent missing the fleet park rule"; fi
sumbefore=$(cat "$kh/settings/cli.json" "$kh/settings/mcp.json" "$kh"/agents/fleet-*.json "$kh/steering/fleet-rules.md" | cksum)
nbak=$(find "$kh" -name '*.fleet-bak.*' | wc -l | tr -d ' ')
eq "kiro: backups made for the two pre-existing settings files" "$nbak" 2
check "kiro: cli.json backup holds the original content" bash -c "grep -q 'keep-me' \"\$1\"/settings/cli.json.fleet-bak.* && ! grep -q disableTrust \"\$1\"/settings/cli.json.fleet-bak.*" _ "$kh"
out=$(inst "$h" kiro.sh --prefix "$h/.local" 2>&1); rc=$?
eq "kiro.sh second run exits 0" "$rc" 0
eq "kiro: second run leaves every file byte-identical" "$(cat "$kh/settings/cli.json" "$kh/settings/mcp.json" "$kh"/agents/fleet-*.json "$kh/steering/fleet-rules.md" | cksum)" "$sumbefore"
eq "kiro: second run makes no extra backup" "$(find "$kh" -name '*.fleet-bak.*' | wc -l | tr -d ' ')" "$nbak"
check "kiro: second run reports no 'wrote:' lines" bash -c "! printf '%s' \"\$1\" | grep -q '^wrote:'" _ "$out"
eq "kiro: fleet server entry still once after second run" "$(jq '.mcpServers | keys | map(select(. == "fleet")) | length' "$kh/settings/mcp.json")" 1

h=$(newhome kiro-dry)
out=$(inst "$h" kiro.sh --dry-run --prefix "$h/.local" 2>&1); rc=$?
eq "kiro --dry-run exits 0" "$rc" 0
eq "kiro --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
check "kiro --dry-run prints would-write lines for agents and mcp" bash -c "printf '%s' \"\$1\" | grep -q 'would write: kiro agent fleet-worker' && printf '%s' \"\$1\" | grep -q 'would write: fleet MCP in kiro mcp.json'" _ "$out"

h=$(newhome kiro-home); mkdir -p "$tmp/khome"
INST_KIRO_HOME=$tmp/khome inst "$h" kiro.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "kiro (KIRO_HOME set) exits 0" "$rc" 0
check "kiro: KIRO_HOME is honored for agents, mcp, cli.json and steering" bash -c "test -f \"\$1\"/agents/fleet-worker.json && test -f \"\$1\"/settings/mcp.json && test -f \"\$1\"/settings/cli.json && test -f \"\$1\"/steering/fleet-rules.md" _ "$tmp/khome"
check "kiro: KIRO_HOME set, nothing written to HOME/.kiro" test ! -e "$h/.kiro"

h=$(newhome kiro-jsonc); mkdir -p "$h/.kiro/settings"
printf '{\n  // comment\n  "mcpServers": {}\n}\n' > "$h/.kiro/settings/mcp.json"
before=$(cksum < "$h/.kiro/settings/mcp.json")
out=$(inst "$h" kiro.sh --prefix "$h/.local" 2>&1); rc=$?
eq "kiro (JSONC mcp.json) exits 1" "$rc" 1
check "kiro (JSONC mcp.json) says it is not plain JSON" bash -c "printf '%s' \"\$1\" | grep -q 'not plain JSON'" _ "$out"
eq "kiro (JSONC mcp.json) left untouched" "$(cksum < "$h/.kiro/settings/mcp.json")" "$before"

h=$(newhome kiro-roles-fixture)
fx=$tmp/kiroroles; mkdir -p "$fx"
printf '{"name":"x","description":"gamma","tools":["*"],"permissions":{"rules":[]}}\n' > "$fx/gamma.kiro.json"
printf 'Gamma "role" text\nline two\n' > "$fx/gamma.md"
FLEET_ROLES_DIR="$fx" inst "$h" kiro.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "kiro roles (fixture dir) exits 0" "$rc" 0
check "kiro roles: fixture prompt is the exact inline md text, name overwritten" jq -e --rawfile p "$fx/gamma.md" '.name == "fleet-gamma" and .prompt == $p' "$h/.kiro/agents/fleet-gamma.json"
rm -f "$fx/gamma.md"; h=$(newhome kiro-roles-nomd)
out=$(FLEET_ROLES_DIR="$fx" inst "$h" kiro.sh --prefix "$h/.local" 2>&1); rc=$?
eq "kiro roles: a role json without its .md fails" "$rc" 1

# =========================================================== agy.sh
# agy on PATH is a stub that only handles `mcp add` (writes $HOME/.gemini/config/mcp_config.json like agy 1.5.13)
cat > "$stubs/agy" <<'EOF'
#!/usr/bin/env bash
echo "agy $*" >> "$STUB_LOG"
if [ "${1:-}" = mcp ] && [ "${2:-}" = add ]; then
  shift 2; n=$1; c=$2; shift 2; f=$HOME/.gemini/config/mcp_config.json; mkdir -p "$(dirname "$f")"
  [ -s "$f" ] || echo '{}' > "$f"
  jq --arg n "$n" --arg c "$c" --args '.mcpServers[$n] = {command:$c,args:$ARGS.positional,disabled:false}' "$@" < "$f" > "$f.t" && mv "$f.t" "$f"
  exit 0
fi
exit 0
EOF
chmod +x "$stubs/agy"
h=$(newhome agy); ah=$h/.gemini; mkdir -p "$ah/config" "$h/proj"
printf '{"mcpServers":{"other":{"serverUrl":"http://127.0.0.1:1/mcp"}}}\n' > "$ah/config/mcp_config.json"
out=$(inst "$h" agy.sh --prefix "$h/.local" --trust "$h/proj" 2>&1); rc=$?
eq "agy.sh first run exits 0" "$rc" 0
check "agy: fleet MCP added via agy mcp add" grep -q "^agy mcp add fleet python3 $ROOT/lib/fleet_mesh.py serve" "$STUB_LOG"
check "agy: mcp_config.json fleet server is python3 <repo>/lib/fleet_mesh.py serve" jq -e --arg m "$ROOT/lib/fleet_mesh.py" '.mcpServers.fleet.command == "python3" and .mcpServers.fleet.args == [$m,"serve"]' "$ah/config/mcp_config.json"
check "agy: existing mcp server preserved" jq -e '.mcpServers.other.serverUrl == "http://127.0.0.1:1/mcp"' "$ah/config/mcp_config.json"
eq "agy: rules file starts with always_on frontmatter" "$(head -3 "$ah/config/rules/fleet-rules.md" | tr '\n' '|')" "---|trigger: always_on|---|"
eq "agy: rules have no unreplaced placeholders" "$(count '{{' "$ah/config/rules/fleet-rules.md")" 0
for r in route decide watch learn worker; do
  af=$ah/config/agents/fleet-$r.md
  check "agy: agent fleet-$r frontmatter parses with name fleet-$r, mainAgent, inheritMcp" python3 -c '
import sys, yaml
t = open(sys.argv[1]).read(); assert t.startswith("---\n"); fm = yaml.safe_load(t.split("---\n")[1])
assert fm["name"] == "fleet-" + sys.argv[2] and fm["mainAgent"] is True and fm["inheritMcp"] is True and fm["subagent"] is False' "$af" "$r"
  check "agy: agent fleet-$r body is roles/$r.md verbatim" python3 -c '
import sys; t = open(sys.argv[1]).read(); b = open(sys.argv[2]).read(); assert t.endswith(b) and t.count("---\n") >= 2' "$af" "$ROOT/roles/$r.md"
done
check "agy: worker agent has write tools, decide does not" bash -c 'grep -q write_file "$1"/fleet-worker.md && ! grep -q write_file "$1"/fleet-decide.md' _ "$ah/config/agents"
check "agy: skills copied with SKILL.md" test -f "$ah/config/skills/running-the-decisions-desk/SKILL.md"
check "agy: --trust adds the dir to antigravity-cli/settings.json trustedWorkspaces" jq -e --arg d "$(cd "$h/proj" && pwd -P)" '.trustedWorkspaces | index($d) != null' "$ah/antigravity-cli/settings.json"
eq "agy: bin/fleet symlink" "$(readlink "$h/.local/bin/fleet")" "$ROOT/bin/fleet"
eq "agy: bin/tafk symlink (fleet under a second name)" "$(readlink "$h/.local/bin/tafk")" "$ROOT/bin/tafk"
sumbefore=$(cat "$ah/config/mcp_config.json" "$ah"/config/agents/fleet-*.md "$ah/config/rules/fleet-rules.md" "$ah/antigravity-cli/settings.json" | cksum)
nadd=$(grep -c '^agy mcp add' "$STUB_LOG")
out=$(inst "$h" agy.sh --prefix "$h/.local" --trust "$h/proj" 2>&1); rc=$?
eq "agy.sh second run exits 0" "$rc" 0
eq "agy: second run leaves every file byte-identical" "$(cat "$ah/config/mcp_config.json" "$ah"/config/agents/fleet-*.md "$ah/config/rules/fleet-rules.md" "$ah/antigravity-cli/settings.json" | cksum)" "$sumbefore"
eq "agy: second run does not call agy mcp add again" "$(grep -c '^agy mcp add' "$STUB_LOG")" "$nadd"
eq "agy: trustedWorkspaces stays unique" "$(jq '.trustedWorkspaces | length' "$ah/antigravity-cli/settings.json")" 1

h=$(newhome agy-dry)
out=$(inst "$h" agy.sh --dry-run --prefix "$h/.local" 2>&1); rc=$?
eq "agy --dry-run exits 0" "$rc" 0
eq "agy --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
check "agy --dry-run says it would add the fleet MCP" bash -c "printf '%s' \"\$1\" | grep -q 'would add: fleet MCP'" _ "$out"

h=$(newhome agy-home); mkdir -p "$tmp/ahome"; nadd=$(grep -c '^agy mcp add' "$STUB_LOG")
INST_AGY_HOME=$tmp/ahome inst "$h" agy.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "agy (FLEET_AGY_HOME set) exits 0" "$rc" 0
check "agy: FLEET_AGY_HOME honored, mcp written by jq (agy mcp add only knows HOME/.gemini)" jq -e --arg m "$ROOT/lib/fleet_mesh.py" '.mcpServers.fleet.args == [$m,"serve"]' "$tmp/ahome/config/mcp_config.json"
eq "agy: FLEET_AGY_HOME set, agy mcp add not called" "$(grep -c '^agy mcp add' "$STUB_LOG")" "$nadd"
check "agy: FLEET_AGY_HOME set, nothing written to HOME/.gemini" test ! -e "$h/.gemini"

# ---- control: a half-marked file must make the installer fail, not corrupt the file
h=$(newhome control); mkdir -p "$h/.codex"
printf 'x\n<!-- fleet-rules:begin -->\nno end marker\n' > "$h/.codex/AGENTS.md"
before=$(cksum < "$h/.codex/AGENTS.md")
if inst "$h" codex.sh --prefix "$h/.local" >/dev/null 2>&1; then fail "control: unbalanced markers make the installer fail"; else pass "control: unbalanced markers make the installer fail"; fi
eq "control: unbalanced AGENTS.md left untouched" "$(cksum < "$h/.codex/AGENTS.md")" "$before"

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_installs.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_installs.sh: all assertions"
