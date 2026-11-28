#!/usr/bin/env bash
# desk.sh: record decisions-desk changes in the desk's living goal file, then validate with `fleet-goal check`.
# Usage:
#   desk.sh open                                       list open items (id, from, question)
#   desk.sh add  <id> <from> <question> [relayTo...]   new open call (openCalls[], or the file's legacy openForHuman[])
#   desk.sh answer <id> <answer> [relayTo...]          close item + append decision D<n> + a line in $FLEET_STATE/decisions-log.jsonl
#   desk.sh elsewhere <id> <answer-with-source>        answered in another session: close, no decision
#   desk.sh amend <id> <addendum>                      the human added to an answer already given (refuses an open call)
#   desk.sh revise <id> <new question text> [why]      facts changed the call: a new rev, old text kept (issue #60);
#                                                      runs `fleet decisions revise`, so decisions.jsonl and this file
#                                                      get the same revisions[] entry. A different topic is refused.
#   desk.sh relayto <id> add|set <session...>          second asker (add), or asker stopped (set ROUTE)
# Env: FLEET_DESK_GOAL  the desk goal file (default: $FLEET_STATE/desk.goal.json)
#      FLEET_BIN        the fleet CLI for revise when not in a kit checkout (../../bin/fleet wins), else fleet on PATH
#      FLEET_DECISION_EXTRA  JSON object merged into the answer's decisions-log line (default none)
#      FLEET_GOAL_BIN   the validator (default: fleet-goal on PATH, else ../../bin/fleet-goal next to this repo)
#      FLEET_STATE      default ${XDG_STATE_HOME:-$HOME/.local/state}/fleet
set -euo pipefail
state=${FLEET_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/fleet}
f=${FLEET_DESK_GOAL:-$state/desk.goal.json}
here=$(cd "$(dirname "$0")" && pwd)
if [ -n "${FLEET_GOAL_BIN:-}" ]; then v=$FLEET_GOAL_BIN
elif command -v fleet-goal >/dev/null 2>&1; then v=fleet-goal
elif [ -x "$here/../../bin/fleet-goal" ]; then v=$here/../../bin/fleet-goal
else echo "fleet-goal not found: install the kit (install/claude.sh) or set FLEET_GOAL_BIN" >&2; exit 2; fi
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cmd=${1:-open}; shift || true

if [ ! -f "$f" ]; then
  case $cmd in
    open|-h|--help|help) ;;
    *) echo "no desk goal file at $f: create it with  fleet-goal new $f --goal \"Run the decisions desk\"" >&2; exit 2 ;;
  esac
fi

# The open-calls key: openCalls, else the legacy openForHuman this file already uses. Never migrated in place, so
# readers that know only the legacy name keep seeing the items.
k=openCalls
[ ! -f "$f" ] || k=$(jq -r 'if has("openCalls") then "openCalls" elif has("openForHuman") then "openForHuman" else "openCalls" end' "$f")

to_json() { if [ $# -eq 0 ]; then echo '[]'; else printf '%s\n' "$@" | jq -R . | jq -s .; fi; }

apply() {
  local tmp out
  tmp=$(mktemp "$f.XXXXXX")
  if ! jq --arg now "$now" --arg k "$k" "$@" "$f" > "$tmp"; then rm -f "$tmp"; echo "jq edit failed" >&2; return 1; fi
  mv "$tmp" "$f"
  # capture then take the first line: `check | head -1` under pipefail dies with SIGPIPE (141)
  out=$("$v" check "$f") || { echo "$out"; return 1; }
  echo "${out%%$'\n'*}"
}

log='| .changelog += [{at:$now,change:$chg,why:$why}] | .lastReconciled=$now'

case $cmd in
  open) jq -r --arg k "$k" '(.[$k] // [])[]|select(.status=="open")|"\(.id)\t\(.from // "?")\t\(.question)"' "$f" ;;
  add)  id=$1 from=$2 q=$3; shift 3; rt=$(to_json "$@")
        apply --arg id "$id" --arg from "$from" --arg q "$q" --argjson rt "$rt" --arg chg "Added $id" --arg why "call from $from" \
          ".[\$k] += [{id:\$id,at:\$now,from:\$from,status:\"open\",question:\$q,relayTo:\$rt}] $log" ;;
  answer) id=$1 a=$2; shift 2; rt=$(to_json "$@")
        asker=$(jq -r --arg id "$id" --arg k "$k" '(.[$k] // [])[]|select(.id==$id)|.from // empty' "$f" | head -1)
        apply --arg id "$id" --arg a "$a" --argjson rt "$rt" --arg chg "Recorded and relayed $id" --arg why "human typed answer" \
          "(.[\$k][]|select(.id==\$id)) |= (.status=\"answered\"|.answer=\$a|.answeredAt=\$now) | .decisions += [{id:\"D\(.decisions|length+1)\",at:\$now,item:\$id,decision:\$a,answer:\$a,relayedTo:\$rt}] $log" || exit $?
        # shared append-only log ROUTE reads (same line shape as fleet_mesh decision_answer)
        mkdir -p "$state"
        # FLEET_DECISION_EXTRA: optional JSON object merged into the log line (the DECISIONS UI adds via, kind, delivery)
        extra=${FLEET_DECISION_EXTRA:-}; [ -n "$extra" ] || extra='{}'
        jq -cn --arg id "$id" --arg at "$now" --arg asker "$asker" --arg a "$a" --argjson rt "$rt" --argjson x "$extra" \
          '{id:$id,at:$at,asker:$asker,verbatim:$a,relayedTo:$rt} + $x' >> "$state/decisions-log.jsonl" ;;
  elsewhere) id=$1 a=$2
        apply --arg id "$id" --arg a "$a" --arg chg "$id closed: answered in another session" --arg why "not re-asked" \
          "(.[\$k][]|select(.id==\$id)) |= (.status=\"answered-elsewhere\"|.answer=\$a|.answeredAt=\$now) $log" ;;
  amend) id=$1 a=$2
        st=$(jq -r --arg id "$id" --arg k "$k" '(.[$k] // [])[]|select(.id==$id or ((.aliases // [])|index($id)))|.status // "open"' "$f" | head -1)
        [ -n "$st" ] || { echo "no desk item $id" >&2; exit 2; }
        # issue #60: amend adds to an answer already given; on an open call it would write an answer nobody gave
        [ "$st" != open ] || { echo "$id is open: amend adds to an answer already given; use revise or answer" >&2; exit 2; }
        apply --arg id "$id" --arg a "$a" --arg chg "Amended $id" --arg why "human added to an earlier answer" \
          "(.[\$k][]|select(.id==\$id)) |= (.answer=((.answer // \"\")+\" | amended: \"+\$a)) | (.decisions[]|select(.item==\$id)) |= (.decision=(.decision+\" | amended: \"+\$a)) $log" ;;
  revise) id=$1 q=$2 w=${3:-desk revise: new facts or the call moved the pick}
        if [ -x "$here/../../bin/fleet" ]; then fb=$here/../../bin/fleet  # a kit checkout: its own CLI
        elif [ -n "${FLEET_BIN:-}" ]; then fb=$FLEET_BIN
        elif command -v fleet >/dev/null 2>&1; then fb=fleet
        else echo "fleet not found: install the kit or set FLEET_BIN" >&2; exit 2; fi
        out=$(FLEET_DESK_GOAL=$f FLEET_GOAL_BIN=$v "$fb" decisions revise "$id" --question "$q" --why "$w" 2>&1) || { echo "$out" >&2; exit 1; }
        echo OK ;;
  _revised) id=$1 fields=$2  # internal: `fleet decisions revise` mirrors its result here; never call it by hand
        apply --arg id "$id" --argjson x "$fields" --arg chg "Revised $id" --arg why "rev $(jq -r '.rev' <<<"$fields"): $(jq -r '.revisions[-1].why // "?"' <<<"$fields")" \
          "(.[\$k][]|select(.id==\$id)) |= (. + \$x) $log" ;;
  relayto) id=$1 mode=$2; shift 2; rt=$(to_json "$@")
        case $mode in add) expr='.relayTo=((.relayTo//[])+$rt|unique)';; set) expr='.relayTo=$rt';; *) echo "mode must be add|set"; exit 2;; esac
        apply --arg id "$id" --argjson rt "$rt" --arg chg "Relay targets for $id: $mode $*" --arg why "duplicate asker or stopped session" \
          "(.[\$k][]|select(.id==\$id)) |= ($expr) $log" ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
