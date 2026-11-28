#!/usr/bin/env bash
# test/test_rules_live.sh: opt-in live probe of the rules layer (#55). It asks each installed CLI, headless and from
# an empty dir (so no project instructions load), to quote words that appear only in AGENTS.fleet.md. PASS means
# that CLI's own loading chain (Claude @ import, OpenCode instructions, Codex AGENTS.md, Kiro steering) reached the
# rules. It reads your real HOME and spends a few model calls, so it runs only with FLEET_LIVE_RULES=1.
# A CLI that isn't on PATH is a SKIP. agy has no headless one-shot mode the kit relies on, so it's a SKIP too.
# Env: FLEET_LIVE_CLIS (comma list, default claude,codex,opencode,kiro)  FLEET_LIVE_TIMEOUT (seconds, default 180)
set -u
if [ "${FLEET_LIVE_RULES:-}" != 1 ]; then echo "SKIP test_rules_live.sh: set FLEET_LIVE_RULES=1 to run (live model calls)"; exit 0; fi
TO=${FLEET_LIVE_TIMEOUT:-180}
CLIS=${FLEET_LIVE_CLIS:-claude,codex,opencode,kiro}
dir=$(mktemp -d); trap 'rm -rf "$dir"' EXIT
Q='Your instructions include fleet operating rules that say "A rule here overrides a home rule only where it says" followed by two bold words. Reply with only those two words, without the asterisks. If no such sentence is in your instructions, reply with only NOT-FOUND. Do not run tools or read files.'
fails=0
probe() {
  local name=$1 out; shift
  if ! command -v "$1" >/dev/null 2>&1; then echo "SKIP $name: $1 not on PATH"; return; fi
  out=$(cd "$dir" && timeout "$TO" "$@" </dev/null 2>&1); local rc=$?
  if printf '%s' "$out" | grep -qi 'overrides home'; then echo "PASS $name: the rules layer is loaded (quoted 'Overrides home')"
  else echo "FAIL $name: rc=$rc, got: $(printf '%s' "$out" | tail -3 | tr '\n' ' ' | cut -c1-200)"; fails=$((fails + 1)); fi
}
case ",$CLIS," in *,claude,*) probe claude claude -p --model claude-haiku-5-5 --tools "" "$Q" ;; esac
case ",$CLIS," in *,codex,*) probe codex codex exec --skip-git-repo-check "$Q" ;; esac
case ",$CLIS," in *,opencode,*) probe opencode opencode run "$Q" ;; esac
case ",$CLIS," in *,kiro,*) probe kiro kiro-cli chat --no-interactive "$Q" ;; esac
echo "SKIP agy: no headless probe (its global rules/*.md loading is covered by the install tests)"
echo "---"
[ $fails -eq 0 ] && echo "ALL PASS (or skipped)" || { echo "FAIL test_rules_live.sh: $fails CLI(s) did not see the rules"; exit 1; }
