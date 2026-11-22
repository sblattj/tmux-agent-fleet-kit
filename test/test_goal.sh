#!/usr/bin/env bash
# test_goal.sh: fleet-goal new/check/condition and the decisions desk helper. One PASS/FAIL line per assertion.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
GOAL=$ROOT/bin/fleet-goal
DESK=$ROOT/skills/running-the-decisions-desk/desk.sh
export FLEET_GOAL_BIN=$GOAL
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-goal.XXXXXX" | sed "s#//#/#g")
trap 'rm -rf "$tmp"' EXIT
fails=0

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
# expect <label> <condition-exit-status-0-means-pass> : usage: expect "label" cmd args...
expect() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
expect_not() { local label=$1; shift; if "$@" >/dev/null 2>&1; then fail "$label"; else pass "$label"; fi; }
contains() { case $1 in *"$2"*) return 0 ;; *) return 1 ;; esac; }
has() { if contains "$2" "$3"; then pass "$1"; else fail "$1 (missing: $3)"; fi; }

g=$tmp/lane-a.goal.json

# --- new / check
out=$("$GOAL" new "$g" --goal "Ship the thing" --report-to ORCHESTRATOR 2>&1); rc=$?
[ $rc -eq 0 ] && pass "new exits 0" || fail "new exits 0 ($out)"
[ -f "$g" ] && pass "new wrote the goal file" || fail "new wrote the goal file"
out=$("$GOAL" check "$g" 2>&1); rc=$?
[ "$out" = "OK" ] && [ $rc -eq 0 ] && pass "check on new file prints OK, exit 0" || fail "check on new file prints OK, exit 0 (rc=$rc out=$out)"
expect_not "new refuses to overwrite an existing file" "$GOAL" new "$g" --goal "again"
expect "new records reportTo" jq -e '.reportTo == "ORCHESTRATOR"' "$g"
expect "new has all required keys" jq -e 'has("goal") and has("phases") and has("nextAction") and has("done") and has("openForHuman") and has("decisions") and has("gates") and has("mustNot") and has("changelog")' "$g"

jq '.keepTools=["Workflow"]' "$g" > "$g.kt" && expect "check accepts keepTools [Workflow]" "$GOAL" check "$g.kt"
jq '.keepTools=["Bogus"]' "$g" > "$g.kt" && expect_not "check refuses an unknown keepTools entry" "$GOAL" check "$g.kt"
jq '.keepTools="Workflow"' "$g" > "$g.kt" && expect_not "check refuses a non-list keepTools" "$GOAL" check "$g.kt"
rm -f "$g.kt"

# --- condition lint
cond=$("$GOAL" condition "$g" --report-to ORCHESTRATOR --turns 40 2>&1); rc=$?
[ $rc -eq 0 ] && pass "condition exits 0" || fail "condition exits 0 ($cond)"
case $cond in "/goal "*) pass "lint: starts with '/goal '" ;; *) fail "lint: starts with '/goal '" ;; esac
[ ${#cond} -le 4000 ] && pass "lint: <= 4000 chars (${#cond})" || fail "lint: <= 4000 chars (${#cond})"
has "lint: names the .json path" "$cond" "$g"
has "lint: says the file wins" "$cond" "the file wins"
has "lint: has a turn bound" "$cond" "stop after 40 turns"
has "condition: GATE line per gates key" "$cond" "GATE G1-evidence PASS"
has "condition: names the fleet-goal check OK line" "$cond" "fleet-goal check $g"
has "condition: first line OK required" "$cond" "first line OK"
has "condition: GOAL HALT exit" "$cond" "GOAL HALT: <reason>"
has "condition: only-openForHuman exit" "$cond" "When only openForHuman items remain, say so once and end the turn"
has "condition: reports to ORCHESTRATOR" "$cond" "ORCHESTRATOR"
if printf '%s' "$cond" | grep -q "$(printf '\342\200\224')"; then fail "condition has no em dash"; else pass "condition has no em dash"; fi
cond2=$("$GOAL" condition "$g" 2>&1)
has "condition: default turn bound is 150" "$cond2" "stop after 150 turns"

# --- gates with several keys
g2=$tmp/multi.goal.json
"$GOAL" new "$g2" --goal "multi" >/dev/null
jq '.gates = {"G1-a":"x","G2-b":"y"}' "$g2" > "$g2.t" && mv "$g2.t" "$g2"
cond3=$("$GOAL" condition "$g2" 2>&1)
has "condition: first of two gates" "$cond3" "GATE G1-a PASS"
has "condition: second of two gates" "$cond3" "GATE G2-b PASS"

# --- control: a broken file must FAIL
b=$tmp/broken.goal.json
jq 'del(.nextAction)' "$g" > "$b"
out=$("$GOAL" check "$b" 2>&1); rc=$?
case $out in "FAIL $b: nextAction"*) pass "control: missing nextAction prints FAIL <path>: nextAction..." ;; *) fail "control: missing nextAction prints FAIL line (got: $out)" ;; esac
[ $rc -eq 1 ] && pass "control: missing nextAction exits 1" || fail "control: missing nextAction exits 1 (rc=$rc)"
expect_not "control: condition refuses a broken file" "$GOAL" condition "$b"

printf '{ not json' > "$tmp/bad.json"
out=$("$GOAL" check "$tmp/bad.json" 2>&1); rc=$?
case $out in FAIL*) [ $rc -eq 1 ] && pass "control: invalid JSON fails" || fail "control: invalid JSON exit code" ;; *) fail "control: invalid JSON fails (got: $out)" ;; esac

jq '.changelog[0].at = "yesterday"' "$g" > "$tmp/badat.json"
out=$("$GOAL" check "$tmp/badat.json" 2>&1)
case $out in FAIL*ISO-8601*) pass "control: non-ISO at is rejected" ;; *) fail "control: non-ISO at is rejected (got: $out)" ;; esac

jq '.openForHuman = [{"id":"Q1","question":"q"}]' "$g" > "$tmp/noststatus.json"
out=$("$GOAL" check "$tmp/noststatus.json" 2>&1)
case $out in FAIL*openForHuman*) pass "control: openForHuman item without status is rejected" ;; *) fail "control: openForHuman item without status is rejected (got: $out)" ;; esac

jq '.decisions = [{"id":"D1"}]' "$g" > "$tmp/nodec.json"
out=$("$GOAL" check "$tmp/nodec.json" 2>&1)
case $out in FAIL*decisions*) pass "control: decision without 'decision' is rejected" ;; *) fail "control: decision without 'decision' is rejected (got: $out)" ;; esac

jq '.phases = [{"id":"P1"}]' "$g" > "$tmp/nophase.json"
out=$("$GOAL" check "$tmp/nophase.json" 2>&1)
case $out in FAIL*phases*) pass "control: phase without title is rejected" ;; *) fail "control: phase without title is rejected (got: $out)" ;; esac

# --- desk.sh on a temp goal file
d=$tmp/desk.goal.json
export FLEET_DESK_GOAL=$d
export FLEET_STATE=$tmp/state
dlog=$FLEET_STATE/decisions-log.jsonl
"$GOAL" new "$d" --goal "Run the decisions desk" >/dev/null
out=$("$DESK" add Q1 lane-a "Ship now? (a, rec) yes (b) no" lane-a ORCHESTRATOR 2>&1); rc=$?
[ "$out" = "OK" ] && [ $rc -eq 0 ] && pass "desk add prints OK" || fail "desk add prints OK (rc=$rc out=$out)"
out=$("$DESK" open 2>&1)
has "desk open lists Q1" "$out" "Q1"
has "desk open shows the asker" "$out" "lane-a"
expect "desk add stored relayTo" jq -e '.openForHuman[0].relayTo == ["lane-a","ORCHESTRATOR"]' "$d"
"$DESK" add Q2 lane-b "Second call" >/dev/null 2>&1
out=$("$DESK" relayto Q2 add lane-c 2>&1)
[ "$out" = "OK" ] && pass "desk relayto add prints OK" || fail "desk relayto add (out=$out)"
expect "desk relayto stored the target" jq -e '.openForHuman[1].relayTo == ["lane-c"]' "$d"
out=$("$DESK" revise Q2 "Second call, revised" 2>&1)
[ "$out" = "OK" ] && expect "desk revise changed the question" jq -e '.openForHuman[1].question == "Second call, revised"' "$d" || fail "desk revise (out=$out)"
out=$("$DESK" answer Q1 "yes, ship it" lane-a ORCHESTRATOR 2>&1)
[ "$out" = "OK" ] && pass "desk answer prints OK" || fail "desk answer prints OK (out=$out)"
expect "desk answer closed Q1" jq -e '.openForHuman[0].status == "answered" and .openForHuman[0].answer == "yes, ship it"' "$d"
expect "desk answer appended decision D1" jq -e '.decisions | length == 1 and .[0].id == "D1" and .[0].decision == "yes, ship it"' "$d"
[ -f "$dlog" ] && [ "$(wc -l < "$dlog" | tr -d ' ')" = 1 ] && pass "desk answer appended one decisions-log line" || fail "desk answer appended one decisions-log line"
expect "decisions-log line has id, asker, verbatim, relayedTo, at" jq -e '.id == "Q1" and .asker == "lane-a" and .verbatim == "yes, ship it" and .relayedTo == ["lane-a","ORCHESTRATOR"] and (.at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T"))' "$dlog"
"$DESK" add Q3 lane-d "Third call" >/dev/null 2>&1
"$DESK" open >/dev/null 2>&1
[ "$(wc -l < "$dlog" | tr -d ' ')" = 1 ] && pass "control: add/open do not touch the decisions log" || fail "control: add/open do not touch the decisions log"
"$DESK" answer Q3 "no, hold" lane-d >/dev/null 2>&1
expect "second answer appends a second line for its own asker" jq -es 'length == 2 and .[1].id == "Q3" and .[1].asker == "lane-d" and .[1].verbatim == "no, hold"' "$dlog"
out=$("$DESK" open 2>&1)
if contains "$out" "Q1"; then fail "desk open no longer lists answered Q1"; else pass "desk open no longer lists answered Q1"; fi
has "desk open still lists Q2" "$out" "Q2"
"$DESK" amend Q1 "and tag it" >/dev/null 2>&1
expect "desk amend extends the decision" jq -e '.decisions[0].decision | contains("amended: and tag it")' "$d"
out=$("$DESK" elsewhere Q2 "no (typed in ORCHESTRATOR)" 2>&1)
[ "$out" = "OK" ] && expect "desk elsewhere closes Q2" jq -e '.openForHuman[1].status == "answered-elsewhere"' "$d" || fail "desk elsewhere (out=$out)"
out=$("$GOAL" check "$d" 2>&1)
[ "$out" = "OK" ] && pass "desk goal file still validates after all edits" || fail "desk goal file validates (out=$out)"
expect "desk wrote ISO-8601 at stamps" jq -e '[.. | objects | .at? | select(. != null)] | all(test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z$"))' "$d"
expect_not "control: desk refuses a missing goal file" env FLEET_DESK_GOAL="$tmp/nope.json" "$DESK" add Q9 x "q"

# --- retire roster.sh (bash 3.2 safe, no declare -A)
if grep -q 'declare -A' "$ROOT/skills/retiring-the-fleet-into-handoffs/roster.sh"; then fail "roster.sh avoids declare -A"; else pass "roster.sh avoids declare -A"; fi
mkdir -p "$tmp/peers"
printf 'Was: old-lane-a\nrest\n' > "$tmp/peers/new-lane-a.md"
out=$(bash "$ROOT/skills/retiring-the-fleet-into-handoffs/roster.sh" "$tmp/peers" old-lane-a old-lane-b 2>&1)
has "roster.sh lists the filed handoff" "$out" "new-lane-a"
has "roster.sh names the missing retiree" "$out" "old-lane-b"
if printf '%s\n' "$out" | grep -E '^  old-lane-a$' >/dev/null; then fail "roster.sh does not flag the filed retiree"; else pass "roster.sh does not flag the filed retiree"; fi

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_goal.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_goal.sh: all assertions"
