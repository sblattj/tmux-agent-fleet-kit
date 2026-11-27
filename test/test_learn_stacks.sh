#!/usr/bin/env bash
# test_learn_stacks.sh: the two card stacks of fleet learn (auto and manual) on temp FLEET_STATEs. One PASS/FAIL line
# per assertion; every positive check has a control. Manual cards serve first, the newest manual request on top,
# rebuild keeps them (manual.jsonl, Q78), and they guide auto cards (topic signal, examples, autoPrompt).
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
FL=$ROOT/bin/fleet-learn
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-stacks.XXXXXX" | sed "s#//#/#g")
tmp=$(cd "$tmp" && pwd -P)
trap 'rm -rf "$tmp"' EXIT
unset FLEET_ROLE FLEET_LEARN_NOW
fails=0

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
expect() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
contains() { case $1 in *"$2"*) return 0 ;; *) return 1 ;; esac; }
has() { if contains "$2" "$3"; then pass "$1"; else fail "$1 (missing: $3; got: $2)"; fi; }
hasnt() { if contains "$2" "$3"; then fail "$1 (unexpected: $3)"; else pass "$1"; fi; }
rc_is() { if [ "$2" -eq "$3" ]; then pass "$1"; else fail "$1 (rc=$2, want $3)"; fi; }
new_state() { mkdir -p "$1/learn" && printf '%s\n' '{"scheduler":"fixed"}' > "$1/learn/learn.json"; }
at() { local st=$1 now=$2; shift 2; FLEET_STATE=$st FLEET_LEARN_NOW=$now "$FL" "$@"; }
# first lid of each line of `lessons --json` / `due --json`
lesson_lids() { FLEET_STATE=$1 "$FL" lessons --json | python3 -I -c 'import json,sys; print(" ".join(",".join(d["lids"]) for d in json.load(sys.stdin)))'; }
due_lids() { FLEET_STATE=$1 FLEET_LEARN_NOW=$2 "$FL" due --json | python3 -I -c 'import json,sys
out=[]
for c in json.load(sys.stdin):
    if c["lid"] not in out: out.append(c["lid"])
print(" ".join(out))'; }

# ================================================================ help
out=$("$FL" -h 2>&1)
has "-h lists fleet learn stack" "$out" "fleet learn stack"
has "-h lists fleet learn examples" "$out" "fleet learn examples"
has "-h lists add --manual" "$out" "[--manual]"
has "-h says session --topic is optional" "$out" "fleet learn session [--topic T]"

# ================================================================ add --manual, stack field on rows and cards
S1=$tmp/s1; new_state "$S1"
for i in 1 2 3; do at "$S1" 2026-01-0${i}T00:00:00Z add --kind decision --origin fleet --source manual:a$i --topic widget --quote "widget fact $i holds for every upload" >/dev/null; done
out=$(at "$S1" 2026-01-04T00:00:00Z add --kind doc --origin fleet --source manual:m1 --topic gadget --title "What is gadget rule one?" --quote "gadget rule one says the lid stays shut" --manual 2>&1); rc=$?
rc_is "add --manual exits 0" $rc 0
has "add --manual says manual" "$out" "OK added L4 (fleet, confirmed, manual)"
expect "a manual row has stack manual and stackAt" bash -c 'FLEET_STATE=$2 "$1" show L4 --json | jq -e ".stack==\"manual\" and .stackAt==\"2026-01-04T00:00:00Z\""' _ "$FL" "$S1"
expect "  control: an auto row has stack auto and no stackAt" bash -c 'FLEET_STATE=$2 "$1" show L1 --json | jq -e ".stack==\"auto\" and (has(\"stackAt\")|not)"' _ "$FL" "$S1"
expect "a manual row's cards carry stack manual" bash -c 'jq -se "map(select(.lid==\"L4\"))|length>0 and all(.stack==\"manual\")" "$1"' _ "$S1/learn/cards.jsonl"
expect "  control: an auto row's cards carry stack auto" bash -c 'jq -se "map(select(.lid==\"L1\"))|length>0 and all(.stack==\"auto\")" "$1"' _ "$S1/learn/cards.jsonl"
expect "add --manual writes the request to manual.jsonl" bash -c 'jq -se "length==1 and .[0].stack==\"manual\" and .[0].row.quote==\"gadget rule one says the lid stays shut\"" "$1"' _ "$S1/learn/manual.jsonl"
S1b=$tmp/s1b; new_state "$S1b"
out=$(FLEET_STATE=$S1b "$FL" add --kind done --origin fleet --source route-done:kit-pane-slots --quote "route-done: kit-pane-slots parked (goal met). desk panes pinned" 2>&1)
has "  control: a plumbing add without --manual is skipped" "$out" "SKIP"
out=$(FLEET_STATE=$S1b "$FL" add --kind done --origin fleet --source route-done:kit-pane-slots --quote "route-done: kit-pane-slots parked (goal met). desk panes pinned" --manual 2>&1)
has "add --manual is never filtered: the human asked for it" "$out" "OK added L1"
expect "  and its cards are made, not retired" bash -c 'jq -se "map(select(.lid==\"L1\" and (.retired|not)))|length>0" "$1"' _ "$S1b/learn/cards.jsonl"

# ================================================================ stack verb: existing rows and cards move in place
cid_before=$(jq -sr 'map(select(.lid=="L2" and .format=="recall"))[0].cid' "$S1/learn/cards.jsonl")
out=$(at "$S1" 2026-01-05T00:00:00Z stack L2-L3 --manual 2>&1); rc=$?
rc_is "stack L2-L3 --manual exits 0" $rc 0
has "stack names the moved rows" "$out" "OK 2 row(s) to the manual stack: L2, L3"
expect "existing cards of a moved row switch to manual, cid kept" bash -c 'jq -se --arg c "$2" "map(select(.lid==\"L2\"))|all(.stack==\"manual\") and (map(.cid)|index(\$c)!=null)" "$1"' _ "$S1/learn/cards.jsonl" "$cid_before"
out=$(at "$S1" 2026-01-05T00:00:01Z stack L2 --manual 2>&1)
has "  control: moving a row already on the stack changes nothing" "$out" "OK 0 row(s) to the manual stack; 1 already there"
out=$(FLEET_STATE=$S1 "$FL" stack 2>&1)
has "bare stack lists the manual rows" "$out" "3 manual row(s), served first; 1 auto"
FLEET_STATE=$S1 "$FL" stack L99 --manual >/dev/null 2>&1; rc=$?
rc_is "  control: stack on a missing row exits 1" $rc 1
FLEET_STATE=$S1 "$FL" stack 9 --manual >/dev/null 2>&1; rc=$?
rc_is "  control: stack on a bad id exits 1" $rc 1
FLEET_STATE=$S1 FLEET_ROLE=watch "$FL" stack L1 --manual >/dev/null 2>&1; rc=$?
rc_is "  control: WATCH can't move rows between stacks (exit 4)" $rc 4
expect "LEARN can move rows between stacks" env FLEET_STATE="$S1" FLEET_ROLE=learn FLEET_LEARN_NOW=2026-01-05T00:00:02Z "$FL" stack L1 --manual
expect "  and back to auto" env FLEET_STATE="$S1" FLEET_ROLE=learn "$FL" stack L1 --auto
expect "a row moved back to auto drops stackAt" bash -c 'FLEET_STATE=$2 "$1" show L1 --json | jq -e ".stack==\"auto\" and (has(\"stackAt\")|not)"' _ "$FL" "$S1"

# ================================================================ serve order: manual first, newest request on top
# S2: 3 auto rows on topic old (oldest), a manual request on topic first-ask, then a newer manual request on
# topic second-ask, then an auto row that shares a subject with a manual row (topic signal), then an unrelated auto row.
S2=$tmp/s2; new_state "$S2"
for i in 1 2 3; do at "$S2" 2026-02-01T00:00:0${i}Z add --kind decision --origin fleet --source manual:o$i --topic old --quote "old auto fact $i about the dock" >/dev/null; done
for i in 1 2; do at "$S2" 2026-02-02T00:00:0${i}Z add --kind doc --origin fleet --source manual:f$i --topic first-ask --title "First ask question $i?" --quote "first ask fact $i about PROJ-9 handling" --manual >/dev/null; done
for i in 1 2; do at "$S2" 2026-02-03T00:00:0${i}Z add --kind doc --origin fleet --source manual:s$i --topic second-ask --title "Second ask question $i?" --quote "second ask fact $i about the crane" --manual >/dev/null; done
at "$S2" 2026-02-04T00:00:00Z add --kind change --origin outside --source manual:near --topic other --quote "PROJ-9 gets a new retry window" >/dev/null
at "$S2" 2026-02-04T00:00:01Z add --kind change --origin outside --source manual:far --topic zzz --quote "an unrelated outside change happened" >/dev/null
got=$(lesson_lids "$S2")
want="L6,L7 L4,L5 L8 L1,L2,L3 L9"
[ "$got" = "$want" ] && pass "lessons: newest manual request, older manual request, auto near a manual subject, other auto (oldest first)" || fail "lessons serve order (got: $got; want: $want)"
out=$(FLEET_STATE=$S2 "$FL" lessons 2>&1)
has "lessons marks a manual topic" "$out" "second-ask                     2 row(s), manual"
out=$(at "$S2" 2026-02-05T00:00:00Z lesson --preview 2>&1)
has "a bare lesson takes the newest manual request" "$out" "Lesson: second-ask (2 rows)"
hasnt "  control: and not the oldest auto topic" "$out" "Lesson: old"
# a manual topic that also has an auto row: the manual batch is taught on its own
at "$S2" 2026-02-05T00:00:00Z add --kind change --origin fleet --source manual:mix --topic second-ask --quote "second ask auto follow-up about the crane" >/dev/null
got=$(lesson_lids "$S2" | cut -d' ' -f1)
[ "$got" = "L6,L7" ] && pass "a topic on both stacks: its manual rows are their own first lesson" || fail "manual batch alone (got: $got)"
# re-asking an older request moves it to the top again (the unshift)
at "$S2" 2026-02-06T00:00:00Z stack L4 L5 --auto >/dev/null
at "$S2" 2026-02-06T00:00:01Z stack L4-L5 --manual >/dev/null
got=$(lesson_lids "$S2" | cut -d' ' -f1)
[ "$got" = "L4,L5" ] && pass "a request made again goes back on top" || fail "unshift on re-request (got: $got)"

# due order: manual cards ahead of auto cards that came due earlier
S3=$tmp/s3; new_state "$S3"
for i in 1 2; do at "$S3" 2026-03-01T00:00:0${i}Z add --kind decision --origin fleet --source manual:x$i --topic early --quote "early auto fact $i about the hull" >/dev/null; done
at "$S3" 2026-03-02T00:00:00Z add --kind doc --origin fleet --source manual:y --topic late --title "Late manual question?" --quote "late manual fact about the mast" --manual >/dev/null
for i in 1 2 3; do at "$S3" 2026-03-03T00:00:00Z lesson >/dev/null; done
got=$(due_lids "$S3" 2026-03-04T00:00:00Z)
[ "$got" = "L3 L1 L2" ] && pass "due: the manual card first though auto cards came due earlier" || fail "due order (got: $got)"
out=$(printf '\ng\nq\n' | at "$S3" 2026-03-04T00:00:00Z quiz 2>&1)
has "quiz asks the manual card first" "$out" "Late manual question?"
expect "snapshot counts per stack" bash -c 'FLEET_STATE=$2 FLEET_LEARN_NOW=2026-03-04T00:00:00Z "$1" snapshot | jq -e ".byStack.manual.items==1 and .byStack.auto.items==2 and (.due[0].stack==\"manual\")"' _ "$FL" "$S3"

# ================================================================ session: bare session serves the manual stack first
S4=$tmp/s4; new_state "$S4"
for i in 1 2 3; do at "$S4" 2026-04-01T00:00:0${i}Z add --kind decision --origin fleet --source manual:p$i --topic auto-topic --quote "auto topic fact $i about the pier" >/dev/null; done
# a manual request of 10 rows: more than one session (MAX_ROWS 8)
for i in 0 1 2 3 4 5 6 7 8 9; do at "$S4" 2026-04-02T00:00:0${i}Z add --kind doc --origin fleet --source manual:q$i --topic asked --title "Asked question $i?" --quote "asked fact $i about the gangway" --manual >/dev/null; done
out=$(printf 'q\n' | at "$S4" 2026-04-03T00:00:00Z session 2>&1); rc=$?
rc_is "bare session exits 0" $rc 0
has "bare session teaches the manual request" "$out" "Lesson: asked (8 rows)"
has "  and marks the first 8 manual rows seen, in request order" "$out" "OK marked seen: L4, L5, L6, L7, L8, L9, L10, L11"
hasnt "  control: no auto row in that lesson" "$out" "auto topic fact"
has "bare session ends on one result line" "$out" "session asked: 8 lesson(s)"
got=$(lesson_lids "$S4" | cut -d' ' -f1)
[ "$got" = "L12,L13" ] && pass "the 2 manual rows left still lead the next lesson" || fail "remaining manual rows first (got: $got)"
out=$(printf 'q\n' | at "$S4" 2026-04-03T00:01:00Z session 2>&1)
has "the next bare session finishes the manual request" "$out" "OK marked seen: L12, L13"
out=$(printf 'q\n' | at "$S4" 2026-04-03T00:02:00Z session 2>&1)
has "  then the auto stack" "$out" "Lesson: auto-topic"
FLEET_STATE=$S4 FLEET_ROLE=learn "$FL" session >/dev/null 2>&1; rc=$?
rc_is "  control: a bare session is still human only (exit 4)" $rc 4
S4e=$tmp/s4e; new_state "$S4e"
out=$(printf 'q\n' | FLEET_STATE=$S4e "$FL" session 2>&1); rc=$?
rc_is "  control: a bare session with nothing waiting exits 0" $rc 0
has "  and says so" "$out" "no new lessons and nothing due"
out=$(printf 'q\n' | at "$S4" 2026-04-03T00:03:00Z session --topic asked 2>&1)
has "  control: session --topic still works as before" "$out" "no new lessons on asked"

# ================================================================ rebuild keeps the manual stack (Q78)
S5=$tmp/s5; new_state "$S5"; mkdir -p "$S5"
cat > "$S5/decisions-log.jsonl" <<'J'
{"id":"Q5","at":"2026-05-01T00:00:00Z","asker":"widget-lane","verbatim":"cap PROJ-5 retries at 3, jittered backoff","relayedTo":["widget-lane"]}
{"id":"Q6","at":"2026-05-01T00:00:01Z","asker":"widget-lane","verbatim":"PROJ-6 logs go to the shared bucket for a week","relayedTo":["widget-lane"]}
J
at "$S5" 2026-05-02T00:00:00Z sync >/dev/null 2>&1
n_sync=$(FLEET_STATE=$S5 "$FL" list --json | jq length)
[ "$n_sync" -ge 2 ] && pass "rebuild fixture: sync made rows from the decisions log" || fail "rebuild fixture: sync rows (got $n_sync)"
lid_q6=$(FLEET_STATE=$S5 "$FL" list --json | jq -r '[.[]|select(.quote|test("PROJ-6"))][0].lid')
at "$S5" 2026-05-03T00:00:00Z stack "$lid_q6" --manual >/dev/null
at "$S5" 2026-05-03T00:00:01Z add --kind doc --origin fleet --source manual:hand --topic hand --title "Hand question?" --quote "a hand-made fact about the keel" --manual >/dev/null
at "$S5" 2026-05-03T00:00:02Z add --kind doc --origin fleet --source manual:gone --topic hand --title "Gone question?" --quote "a hand-made fact that was dropped" --manual >/dev/null
lid_gone=$(FLEET_STATE=$S5 "$FL" list --json | jq -r '[.[]|select(.quote|test("dropped"))][0].lid')
FLEET_STATE=$S5 "$FL" drop "$lid_gone" --reason "test drop" >/dev/null
out=$(at "$S5" 2026-05-04T00:00:00Z rebuild 2>&1); rc=$?
rc_is "rebuild exits 0" $rc 0
expect "rebuild keeps manual.jsonl" test -s "$S5/learn/manual.jsonl"
expect "a source row marked manual is manual again after rebuild" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.quote|test(\"PROJ-6\"))][0].stack==\"manual\""' _ "$FL" "$S5"
expect "  control: the other source row stays auto" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.quote|test(\"PROJ-5\"))][0].stack==\"auto\""' _ "$FL" "$S5"
expect "a hand-made manual row comes back after rebuild, with its cards" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.quote==\"a hand-made fact about the keel\" and .stack==\"manual\" and .stackAt==\"2026-05-03T00:00:01Z\")]|length==1"' _ "$FL" "$S5"
expect "  control: a dropped manual row does not come back" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.quote|test(\"dropped\"))]|length==0"' _ "$FL" "$S5"
got=$(lesson_lids "$S5" | cut -d' ' -f1)
hand=$(FLEET_STATE=$S5 "$FL" list --json | jq -r '[.[]|select(.topic=="hand")][0].lid')
[ "$got" = "$hand" ] && pass "after rebuild the newest manual request still serves first" || fail "post-rebuild order (got: $got, want: $hand)"
expect "rebuild counts manual rows" bash -c 'FLEET_STATE=$2 FLEET_LEARN_NOW=2026-05-04T00:00:01Z "$1" rebuild --json | jq -e ".counts.manual==2"' _ "$FL" "$S5"

# ================================================================ manual cards guide auto cards
out=$(FLEET_STATE=$S2 "$FL" examples --json 2>&1)
expect "examples: manual prompts and quotes, newest request first" python3 -I - "$out" <<'PY'
import json, sys
ex = json.loads(sys.argv[1])
assert [e["topic"] for e in ex][:2] == ["first-ask", "first-ask"], ex
assert ex[0]["prompt"] == "First ask question 1?" and "PROJ-9" in ex[0]["quote"], ex[0]
assert all(e["topic"] in ("first-ask", "second-ask") for e in ex), ex
PY
out=$(FLEET_STATE=$S2 "$FL" examples --topic second-ask 2>&1)
has "examples --topic narrows" "$out" "2 manual example(s)"
# autoPrompt: a command that writes the question for an untitled auto row from the manual examples
S6=$tmp/s6; new_state "$S6"
cat > "$tmp/gen.py" <<'PY'
import json, sys
p = json.load(sys.stdin)
assert p["examples"] and p["examples"][0]["prompt"].endswith("?")
print("noise line")
print("Which rule now covers %s?" % p["row"]["quote"].split()[0])
PY
cat > "$tmp/bad.py" <<'PY'
import sys
sys.stdin.read()
print("not a question")
PY
cat > "$tmp/slow.py" <<'PY'
import time
time.sleep(5)
print("Too late to count here?")
PY
cfg() { printf '{"scheduler":"fixed","autoPrompt":{"run":"python3 -I %s","timeoutSec":%s}}\n' "$1" "$2" > "$S6/learn/learn.json"; }
cfg "$tmp/gen.py" 5
FLEET_STATE=$S6 "$FL" add --kind decision --origin fleet --source manual:pre --topic t --quote "Prelude fact with no manual stack yet" >/dev/null
expect "  control: autoPrompt is off while the manual stack is empty" bash -c 'jq -se "map(select(.lid==\"L1\" and .format==\"recall\"))[0] | .prompt==\"What was decided here, and why?\" and (has(\"genPrompt\")|not)" "$1"' _ "$S6/learn/cards.jsonl"
FLEET_STATE=$S6 "$FL" add --kind doc --origin fleet --source manual:man --topic t --title "Which rule covers the dock?" --quote "the dock rule covers every berth" --manual >/dev/null
FLEET_STATE=$S6 "$FL" add --kind decision --origin fleet --source manual:gen --topic t --quote "Berthing moves to the north quay" >/dev/null
expect "autoPrompt: a new untitled auto card takes the command's question" bash -c 'jq -se "map(select(.lid==\"L3\" and .format==\"recall\"))[0] | .prompt==\"Which rule now covers Berthing?\" and .genPrompt==\"Which rule now covers Berthing?\"" "$1"' _ "$S6/learn/cards.jsonl"
FLEET_STATE=$S6 "$FL" sync >/dev/null 2>&1
expect "  the generated prompt survives a card refresh" bash -c 'jq -se "map(select(.lid==\"L3\" and .format==\"recall\"))[0].prompt==\"Which rule now covers Berthing?\"" "$1"' _ "$S6/learn/cards.jsonl"
expect "  control: a manual row keeps its own title" bash -c 'jq -se "map(select(.lid==\"L2\" and .format==\"recall\"))[0].prompt==\"Which rule covers the dock?\"" "$1"' _ "$S6/learn/cards.jsonl"
cfg "$tmp/bad.py" 5
FLEET_STATE=$S6 "$FL" add --kind decision --origin fleet --source manual:bad --topic t --quote "Mooring lines are checked hourly" >/dev/null
expect "  control: a line that is not a question leaves the template prompt" bash -c 'jq -se "map(select(.lid==\"L4\" and .format==\"recall\"))[0] | .prompt==\"What was decided here, and why?\" and (has(\"genPrompt\")|not)" "$1"' _ "$S6/learn/cards.jsonl"
cfg "$tmp/slow.py" 1
t0=$(date +%s)
FLEET_STATE=$S6 "$FL" add --kind decision --origin fleet --source manual:slow --topic t --quote "Tugs wait at the outer mark" >/dev/null
t1=$(date +%s)
[ $((t1 - t0)) -lt 4 ] && pass "  control: a slow autoPrompt command is killed at timeoutSec" || fail "autoPrompt timeout bound ($((t1 - t0))s)"
expect "  and the card keeps the template prompt" bash -c 'jq -se "map(select(.lid==\"L5\" and .format==\"recall\"))[0].prompt==\"What was decided here, and why?\"" "$1"' _ "$S6/learn/cards.jsonl"

if [ "$fails" -eq 0 ]; then echo "---"; echo "PASS test_learn_stacks.sh: all assertions"; else echo "---"; echo "FAIL test_learn_stacks.sh: $fails failed"; fi
[ "$fails" -eq 0 ]
