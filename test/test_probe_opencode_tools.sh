#!/usr/bin/env bash
# test_probe_opencode_tools.sh: the fleet-probe MCP verbs are offered to OpenCode agent fleet-probe only.
# OpenCode 1.19 turns the global tools["fleet-probe_*"]=false into a deny rule that a later global permission
# ("allow" from --bypass) overrides, last match wins; install/common.sh therefore denies fleet-probe_* in each other
# agent's own permission. Isolated XDG dirs; the global OpenCode config is only read (provider block, live mode).
#   bash test/test_probe_opencode_tools.sh            resolver check: `opencode debug agent <a>`, no model call
#   FLEET_PROBE_OC_LIVE=1 bash test/...               also asks fleet-worker, fleet-probe and a pre-fix worker
#                                                     (control) to list their fleet-probe_* tools: 3 model calls
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
export FLEET_NO_DESKS=1  # issue #9: lanes here launch into a scratch fleet with no desks on purpose; desks tests opt back in
command -v opencode >/dev/null 2>&1 || { echo "SKIP opencode not on PATH"; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "FAIL jq is required"; exit 1; }
T=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-octools.XXXXXX")
trap 'rm -rf "$T"' EXIT   # the live config may hold a copied provider key: never keep it
fails=0
t() { local l=$1; shift; if "$@" >/dev/null 2>&1; then echo "PASS $l"; else echo "FAIL $l"; fails=$((fails + 1)); fi; }
OCENV=(env -u OPENCODE_CONFIG -u OPENCODE_CONFIG_DIR -u OPENCODE_CONFIG_CONTENT XDG_DATA_HOME="$T/data")

mkdir -p "$T/xdg/opencode" "$T/data"; echo '{}' > "$T/xdg/opencode/opencode.json"
t "isolated install --bypass exits 0" "${OCENV[@]}" XDG_CONFIG_HOME="$T/xdg" FLEET_OC_CONFIG_DIR="$T/xdg/opencode" \
  bash "$ROOT/install/opencode.sh" --bypass --prefix "$T/prefix"
C=$T/xdg/opencode/opencode.json
# pre-fix copy for the controls: the same config with every per-agent fleet-probe_* deny removed
mkdir -p "$T/old/opencode"
jq '.agent |= with_entries(.value.permission |= (if type == "object" then del(.["fleet-probe_*"]) else . end))' "$C" > "$T/old/opencode/opencode.json"

verdict() {  # verdict <xdg> <agent>: the action the last matching rule gives tool fleet-probe_probe_gh_get
  (cd "$T" && "${OCENV[@]}" XDG_CONFIG_HOME="$1" timeout 90 opencode debug agent "$2" 2>/dev/null) | python3 -I -c '
import fnmatch, json, sys
s = sys.stdin.read(); d = json.loads(s[s.find("{"):]); act = "none"
for r in d.get("permission", []):
    if r.get("pattern") == "*" and fnmatch.fnmatchcase("fleet-probe_probe_gh_get", r.get("permission", "")):
        act = r.get("action")
print(act)'
}
for a in fleet-worker fleet-route fleet-decide fleet-watch fleet-learn build plan general; do
  v=$(verdict "$T/xdg" "$a"); t "resolver: $a denies fleet-probe_* (got $v)" test "$v" = deny
done
v=$(verdict "$T/xdg" fleet-probe); t "resolver: control: fleet-probe allows its verbs (got $v)" test "$v" = allow
v=$(verdict "$T/old" fleet-worker); t "resolver: control: the pre-fix worker allows them (got $v)" test "$v" = allow

if [ "${FLEET_PROBE_OC_LIVE:-}" = 1 ]; then
  g=$HOME/.config/opencode/opencode.json
  for x in xdg old; do
    jq -s '.[0] * ((.[1] // {}) | {provider, model, small_model} | with_entries(select(.value != null)))' \
      "$T/$x/opencode/opencode.json" "$g" > "$T/$x.tmp" && mv "$T/$x.tmp" "$T/$x/opencode/opencode.json"
    chmod 600 "$T/$x/opencode/opencode.json"
  done
  P='Do not call any tool. Reply with the exact names of every tool available to you whose name starts with "fleet-probe", one per line, or the single word NONE if there are none. Then a last line END.'
  live() { (cd "$T" && "${OCENV[@]}" XDG_CONFIG_HOME="$1" timeout 240 opencode run --agent "$2" "$P" 2>/dev/null) | grep -c '^fleet-probe_'; }
  n=$(live "$T/xdg" fleet-worker); t "live: fleet-worker is offered no fleet-probe_* tool (listed $n)" test "$n" = 0
  n=$(live "$T/xdg" fleet-probe); t "live: control: fleet-probe is offered its verbs (listed $n)" test "$n" -ge 7
  n=$(live "$T/old" fleet-worker); t "live: control: the pre-fix worker is offered them (listed $n)" test "$n" -ge 1
else
  echo "SKIP live checks (set FLEET_PROBE_OC_LIVE=1; 3 model calls)"
fi
echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_probe_opencode_tools.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_probe_opencode_tools.sh: all assertions"
