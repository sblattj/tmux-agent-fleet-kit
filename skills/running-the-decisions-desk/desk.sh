#!/usr/bin/env bash
# desk.sh: record decisions-desk changes in the desk's living goal file, then validate with `fleet-goal check`.
# Usage:
#   desk.sh open                                       list open items (id, from, question)
#   desk.sh add  <id> <from> <question> [relayTo...]   new openForHuman item
#   desk.sh answer <id> <answer> [relayTo...]          close item + append decision D<n> + a line in $FLEET_STATE/decisions-log.jsonl
#   desk.sh elsewhere <id> <answer-with-source>        answered in another session: close, no decision
#   desk.sh amend <id> <addendum>                      the human added to an answer already given
#   desk.sh revise <id> <new question text>            recommendation changed mid-call
#   desk.sh relayto <id> add|set <session...>          second asker (add), or asker stopped (set ORCHESTRATOR)
# Env: FLEET_DESK_GOAL  the desk goal file (default: $FLEET_STATE/desk.goal.json)
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

to_json() { if [ $# -eq 0 ]; then echo '[]'; else printf '%s\n' "$@" | jq -R . | jq -s .; fi; }

apply() {
  local tmp out
  tmp=$(mktemp "$f.XXXXXX")
  if ! jq --arg now "$now" "$@" "$f" > "$tmp"; then rm -f "$tmp"; echo "jq edit failed" >&2; return 1; fi
  mv "$tmp" "$f"
  # capture then take the first line: `check | head -1` under pipefail dies with SIGPIPE (141)
  out=$("$v" check "$f") || { echo "$out"; return 1; }
  echo "${out%%$'\n'*}"
}

log='| .changelog += [{at:$now,change:$chg,why:$why}] | .lastReconciled=$now'

case $cmd in
  open) jq -r '(.openForHuman // [])[]|select(.status=="open")|"\(.id)\t\(.from // "?")\t\(.question)"' "$f" ;;
  add)  id=$1 from=$2 q=$3; shift 3; rt=$(to_json "$@")
        apply --arg id "$id" --arg from "$from" --arg q "$q" --argjson rt "$rt" --arg chg "Added $id" --arg why "call from $from" \
          ".openForHuman += [{id:\$id,at:\$now,from:\$from,status:\"open\",question:\$q,relayTo:\$rt}] $log" ;;
  answer) id=$1 a=$2; shift 2; rt=$(to_json "$@")
        asker=$(jq -r --arg id "$id" '(.openForHuman // [])[]|select(.id==$id)|.from // empty' "$f" | head -1)
        apply --arg id "$id" --arg a "$a" --argjson rt "$rt" --arg chg "Recorded and relayed $id" --arg why "human typed answer" \
          "(.openForHuman[]|select(.id==\$id)) |= (.status=\"answered\"|.answer=\$a|.answeredAt=\$now) | .decisions += [{id:\"D\(.decisions|length+1)\",at:\$now,item:\$id,decision:\$a,answer:\$a,relayedTo:\$rt}] $log" || exit $?
        # shared append-only log the orchestrator reads (same line shape as fleet_mesh decision_answer)
        mkdir -p "$state"
        jq -cn --arg id "$id" --arg at "$now" --arg asker "$asker" --arg a "$a" --argjson rt "$rt" \
          '{id:$id,at:$at,asker:$asker,verbatim:$a,relayedTo:$rt}' >> "$state/decisions-log.jsonl" ;;
  elsewhere) id=$1 a=$2
        apply --arg id "$id" --arg a "$a" --arg chg "$id closed: answered in another session" --arg why "not re-asked" \
          "(.openForHuman[]|select(.id==\$id)) |= (.status=\"answered-elsewhere\"|.answer=\$a|.answeredAt=\$now) $log" ;;
  amend) id=$1 a=$2
        apply --arg id "$id" --arg a "$a" --arg chg "Amended $id" --arg why "human added to an earlier answer" \
          "(.openForHuman[]|select(.id==\$id)) |= (.answer=((.answer // \"\")+\" | amended: \"+\$a)) | (.decisions[]|select(.item==\$id)) |= (.decision=(.decision+\" | amended: \"+\$a)) $log" ;;
  revise) id=$1 q=$2
        apply --arg id "$id" --arg q "$q" --arg chg "Revised $id" --arg why "new facts or call moved the pick" \
          "(.openForHuman[]|select(.id==\$id)) |= (.question=\$q) $log" ;;
  relayto) id=$1 mode=$2; shift 2; rt=$(to_json "$@")
        case $mode in add) expr='.relayTo=((.relayTo//[])+$rt|unique)';; set) expr='.relayTo=$rt';; *) echo "mode must be add|set"; exit 2;; esac
        apply --arg id "$id" --argjson rt "$rt" --arg chg "Relay targets for $id: $mode $*" --arg why "duplicate asker or stopped session" \
          "(.openForHuman[]|select(.id==\$id)) |= ($expr) $log" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
