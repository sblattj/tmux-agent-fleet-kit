#!/usr/bin/env bash
# test_installs.sh: run each installer against a throwaway HOME with stubbed claude/codex/git on PATH.
# One PASS/FAIL line per assertion; exits 1 on any FAIL. Never touches the real HOME.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-inst.XXXXXX" | sed 's#//#/#g')
trap 'rm -rf "$tmp"' EXIT
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
  ( unset CODEX_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME FLEET_STATE FLEET_PREFIX
    if [ -n "${INST_CODEX_HOME:-}" ]; then export CODEX_HOME=$INST_CODEX_HOME; fi
    export HOME="$h" PATH="$stubs:$PATH" FLEET_CLAUDE_BIN=claude FLEET_CODEX_BIN=codex
    bash "$ROOT/install/$script" "$@" )
}
newhome() { local h=$tmp/home.$1; mkdir -p "$h"; printf '%s' "$h"; }

proj=$tmp/proj; mkdir -p "$proj"
projreal=$(cd "$proj" && pwd -P)

# =========================================================== claude.sh
h=$(newhome claude)
mkdir -p "$h/.claude"
printf '# my notes\nkeep this line\n' > "$h/.claude/CLAUDE.md"
export FLEET_HUMAN=Pat FLEET_DESK=DESKX FLEET_ORCHESTRATOR=ORCHX
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
FLEET_DESK=DESKY inst "$h" claude.sh --prefix "$h/.local" >/dev/null 2>&1
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
cdir=$h/.config/opencode; pdir=$h/.local/share/opencode-goal-pro-max-complete-plugin
check "opencode: git clone pinned to v1.5.3 from the plugin repo" grep -qF "git clone --depth 1 --branch v1.5.3 https://github.com/sblattj/opencode-goal-pro-max-complete-plugin $pdir" "$STUB_LOG"
check "opencode: opencode.json lists the plugin once" jq -e --arg p "file://$pdir" '[.plugin[] | select(. == $p)] | length == 1' "$cdir/opencode.json"
check "opencode: tui.json lists the plugin once" jq -e --arg p "file://$pdir" '[.plugin[] | select(. == $p)] | length == 1' "$cdir/tui.json"
check "opencode: mcp.fleet entry is local, enabled, runs fleet_mesh.py serve" jq -e --arg m "$ROOT/lib/fleet_mesh.py" '.mcp.fleet == {"type":"local","command":["python3",$m,"serve"],"enabled":true}' "$cdir/opencode.json"
check "opencode: --bypass sets permission allow" jq -e '.permission == "allow"' "$cdir/opencode.json"
eq "opencode: AGENTS.md begin marker once" "$(count 'fleet-rules:begin' "$cdir/AGENTS.md")" 1
eq "opencode: AGENTS.md end marker once" "$(count 'fleet-rules:end' "$cdir/AGENTS.md")" 1
eq "opencode: bin/fleet-goal symlink" "$(readlink "$h/.local/bin/fleet-goal")" "$ROOT/bin/fleet-goal"
before=$(cksum < "$cdir/opencode.json")
inst "$h" opencode.sh --bypass --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "opencode.sh second run exits 0" "$rc" 0
eq "opencode: second run leaves opencode.json byte-identical" "$(cksum < "$cdir/opencode.json")" "$before"
check "opencode: plugin array still has one entry" jq -e '.plugin | length == 1' "$cdir/opencode.json"
eq "opencode: git clone ran exactly once across two runs" "$(count 'git clone' "$STUB_LOG")" 1
eq "opencode: AGENTS.md markers once after second run" "$(count 'fleet-rules:begin' "$cdir/AGENTS.md")" 1

h=$(newhome opencode-nobypass)
printf '{"theme":"x","plugin":["other"]}\n' > /dev/null
mkdir -p "$h/.config/opencode"; printf '{"theme":"x","plugin":["other"]}\n' > "$h/.config/opencode/opencode.json"
FLEET_OC_GOAL_PLUGIN_REF=v9.9.9 FLEET_OC_GOAL_PLUGIN_URL=https://example.invalid/p.git inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1; rc=$?
eq "opencode (no --bypass, env overrides) exits 0" "$rc" 0
check "opencode: no permission key without --bypass" jq -e 'has("permission") | not' "$h/.config/opencode/opencode.json"
check "opencode: existing keys preserved, plugin appended" jq -e '.theme == "x" and .plugin[0] == "other" and (.plugin | length) == 2' "$h/.config/opencode/opencode.json"
check "opencode: ref and url overrides honored" grep -qF "git clone --depth 1 --branch v9.9.9 https://example.invalid/p.git" "$STUB_LOG"

# plugin dir filled by the plugin's own installer (package.json, no .git): reuse it, never clone over it
h=$(newhome opencode-preinst)
mkdir -p "$h/.local/share/opencode-goal-pro-max-complete-plugin"; echo '{}' > "$h/.local/share/opencode-goal-pro-max-complete-plugin/package.json"
n=$(count 'git clone' "$STUB_LOG")
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode (pre-installed plugin dir) exits 0" "$rc" 0
eq "opencode (pre-installed plugin dir) ran no git clone" "$(count 'git clone' "$STUB_LOG")" "$n"
check "opencode (pre-installed plugin dir) says already installed" bash -c "printf '%s' \"\$1\" | grep -q 'already installed'" _ "$out"
check "opencode (pre-installed plugin dir) still lists the plugin" jq -e '.plugin | length == 1' "$h/.config/opencode/opencode.json"
# non-empty dir with no plugin: fail loud, no clone
h=$(newhome opencode-junk)
mkdir -p "$h/.local/share/opencode-goal-pro-max-complete-plugin"; echo x > "$h/.local/share/opencode-goal-pro-max-complete-plugin/junk"
out=$(inst "$h" opencode.sh --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode (junk plugin dir) exits 1" "$rc" 1
check "opencode (junk plugin dir) names the dir" bash -c "printf '%s' \"\$1\" | grep -q 'holds no plugin'" _ "$out"
eq "opencode (junk plugin dir) ran no git clone" "$(count 'git clone' "$STUB_LOG")" "$n"

h=$(newhome opencode-dry)
n=$(count 'git clone' "$STUB_LOG")
out=$(inst "$h" opencode.sh --dry-run --bypass --prefix "$h/.local" 2>&1); rc=$?
eq "opencode --dry-run exits 0" "$rc" 0
eq "opencode --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
eq "opencode --dry-run ran no git clone" "$(count 'git clone' "$STUB_LOG")" "$n"
check "opencode --dry-run prints would-write lines" bash -c "printf '%s' \"\$1\" | grep -q 'would write'" _ "$out"

# =========================================================== role installs (fixture roles dir)
roles=$tmp/roles; mkdir -p "$roles"
printf 'developer_instructions = "alpha role"\n' > "$roles/alpha.codex.toml"
printf 'developer_instructions = "beta role"\n[mcp_servers.fleet]\nenabled_tools = ["peer_send"]\n' > "$roles/beta.codex.toml"
printf '{"description":"alpha","prompt":"{file:./roles/alpha.md}","tools":{"write":false}}\n' > "$roles/alpha.opencode.json"
printf '{"description":"beta","prompt":"{file:./roles/beta.md}","permission":{"bash":{"fleet launch*":"deny"}}}\n' > "$roles/beta.opencode.json"

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
check "opencode roles: mcp and plugin entries survive the merge" jq -e '.mcp.fleet.enabled == true and (.plugin | length) == 1' "$oj"
before=$(cksum < "$oj")
FLEET_ROLES_DIR="$roles" inst "$h" opencode.sh --prefix "$h/.local" >/dev/null 2>&1
eq "opencode roles: second run byte-identical" "$(cksum < "$oj")" "$before"
h=$(newhome roles-oc-dry)
FLEET_ROLES_DIR="$roles" inst "$h" opencode.sh --dry-run --prefix "$h/.local" >/dev/null 2>&1
eq "opencode roles: --dry-run wrote nothing under HOME" "$(nfiles "$h")" 0
h=$(newhome roles-oc-empty)
out=$(FLEET_ROLES_DIR="$empty" inst "$h" opencode.sh --prefix "$h/.local" 2>&1); rc=$?
eq "opencode roles: empty roles dir exits 0" "$rc" 0
check "opencode roles: empty roles dir adds no agent key" jq -e 'has("agent") | not' "$h/.config/opencode/opencode.json"

h=$(newhome roles-claude)
out=$(inst "$h" claude.sh --help 2>&1)
check "claude roles: --help says no role install is needed" bash -c "printf '%s' \"\$1\" | grep -q 'installs no role files'" _ "$out"
check "claude roles: the installer writes no fleet-* role files" bash -c "! find \"\$1\" -name 'fleet-*' | grep -q ." _ "$h"

# ---- control: a half-marked file must make the installer fail, not corrupt the file
h=$(newhome control); mkdir -p "$h/.codex"
printf 'x\n<!-- fleet-rules:begin -->\nno end marker\n' > "$h/.codex/AGENTS.md"
before=$(cksum < "$h/.codex/AGENTS.md")
if inst "$h" codex.sh --prefix "$h/.local" >/dev/null 2>&1; then fail "control: unbalanced markers make the installer fail"; else pass "control: unbalanced markers make the installer fail"; fi
eq "control: unbalanced AGENTS.md left untouched" "$(cksum < "$h/.codex/AGENTS.md")" "$before"

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_installs.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_installs.sh: all assertions"
