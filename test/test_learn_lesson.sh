#!/usr/bin/env bash
# test_learn_lesson.sh: teach before test (openspec/specs/learn-*). fleet learn lesson, lessons, session,
# digest, brief, curate, entity and rebuild on a temp FLEET_STATE. One PASS/FAIL line per assertion; every positive
# check has a control that must fail. Fixtures only (generic names), no network.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
FL=$ROOT/bin/fleet-learn
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-lesson.XXXXXX" | sed "s#//#/#g")
tmp=$(cd "$tmp" && pwd -P)
trap 'rm -rf "$tmp"' EXIT
unset FLEET_ROLE FLEET_LEARN_NOW FLEET_STATE
fails=0
T0=2026-01-10T09:00:00Z

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
contains() { case $1 in *"$2"*) return 0 ;; *) return 1 ;; esac; }
has() { if contains "$2" "$3"; then pass "$1"; else fail "$1 (missing: $3; got: $2)"; fi; }
hasnt() { if contains "$2" "$3"; then fail "$1 (unexpected: $3)"; else pass "$1"; fi; }
rc_is() { if [ "$2" -eq "$3" ]; then pass "$1"; else fail "$1 (rc=$2, want $3)"; fi; }
nlines() { if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi; }
# st STATE [now] -- args: run fleet-learn on a pinned fixed scheduler and clock
st() { local s=$1; shift; mkdir -p "$s/learn"; [ -e "$s/learn/learn.json" ] || printf '%s\n' '{"scheduler":"fixed"}' > "$s/learn/learn.json"
  FLEET_STATE=$s FLEET_LEARN_NOW=${NOW:-$T0} "$FL" "$@"; }

case $tmp in "${TMPDIR:-/tmp}"*|/tmp/*|/private/*|/var/*) ;; *) echo "FAIL refusing to run outside a temp dir"; exit 1 ;; esac

# Fixture: three answered calls on one PR topic, a call line heard on the call, a row on another topic.
seed() {
  local s=$1; mkdir -p "$s"
  cat > "$s/decisions-log.jsonl" <<'EOF'
{"id":"Q1","at":"2026-01-08T09:00:00Z","asker":"lane-a","verbatim":"a: cap upload retries at 3, jittered","relayedTo":["lane-a"]}
{"id":"Q2","at":"2026-01-08T09:05:00Z","asker":"lane-a","verbatim":"b: retries log one line each","relayedTo":["lane-a"]}
{"id":null,"at":"2026-01-08T09:10:00Z","asker":"SPEAKER_01","verbatim":"we move the retry work to next sprint","relayedTo":["ROUTE"],"kind":"decided-on-call","confirmed":false,"from":"WATCH"}
EOF
  cat > "$s/decisions.jsonl" <<'EOF'
{"id":"Q1","at":"2026-01-08T08:00:00Z","from":"lane-a","question":"acme/widget#7: cap the upload retries?","options":["a","b"],"status":"answered","answer":"a"}
{"id":"Q2","at":"2026-01-08T08:01:00Z","from":"lane-a","question":"acme/widget#7: how do retries log?","options":["a","b"],"status":"answered","answer":"b"}
EOF
  st "$s" sync >/dev/null 2>&1
  st "$s" add --kind decision --origin fleet --source manual:docs --topic docs --quote "setup needs Python 3.8" >/dev/null
}

# ================================================================ help
out=$(st "$tmp/h" -h 2>&1)
for v in lesson lessons session digest brief curate entity rebuild sources; do has "-h lists fleet learn $v" "$out" "fleet learn $v"; done

# ================================================================ teach before test
S=$tmp/s1; seed "$S"
out=$(st "$S" due 2>&1)
has "before any lesson nothing is due (cards wait for the lesson)" "$out" "0 due"
out=$(st "$S" lessons 2>&1)
has "lessons lists the topics waiting" "$out" "topic(s) waiting for a lesson"
has "lessons names the docs topic" "$out" "docs"
out=$(st "$S" lesson --preview 2>&1); rc=$?
rc_is "lesson --preview exits 0" $rc 0
has "a lesson starts with the terms" "$out" "Terms"
has "then each row in context" "$out" "In context"
has "--preview says nothing was recorded" "$out" "preview: nothing recorded"
[ ! -s "$S/learn/seen.jsonl" ] && pass "  control: --preview wrote no seen row" || fail "  control: --preview wrote no seen row"
out=$(st "$S" lesson 2>&1); rc=$?
rc_is "lesson exits 0" $rc 0
has "lesson marks the rows it showed seen" "$out" "OK marked seen:"
has "lesson says how many rows still wait" "$out" "more row"
n=$(nlines "$S/learn/seen.jsonl")
[ "$n" -ge 1 ] && pass "lesson appended seen rows ($n)" || fail "lesson appended seen rows"
out=$(st "$S" due 2>&1)
hasnt "after the first lesson some cards are due" "$out" " 0 due"
# every context line carries a verbatim quote and its source in brackets
st "$S" lesson >/dev/null 2>&1; st "$S" lesson >/dev/null 2>&1
out=$(st "$S" lesson 2>&1)
has "with every topic seen, a bare lesson says so" "$out" "no new lessons"
out=$(st "$S" lesson --topic docs --preview 2>&1)
has "  control: a seen topic has no new lesson" "$out" "no new lessons on docs"

# ================================================================ quotes, sources, heard, dropped lines
S=$tmp/s2; seed "$S"
st "$S" curate L1 --what "cap upload retries at 3" --why "a fourth try never worked" >/dev/null 2>&1
out=$(st "$S" lesson --topic acme/widget#7 --preview 2>&1)
python3 -I - "$out" <<'PY' && pass "every In context row line is a quote plus [source]" || fail "every In context row line is a quote plus [source]"
import re, sys
body = sys.argv[1].split("In context", 1)[1]
rows = [l for l in body.splitlines() if re.match(r"  L\d+ ", l)]
assert rows, body
for l in rows:
    assert re.search(r'"[^"]+" \[[a-z-]+:[^\]]+\]$', l), l
PY
has "a quote-backed what line is shown with its quote" "$out" 'what: cap upload retries at 3 "'
has "an unbacked why line is dropped and counted" "$out" "1 line dropped (not backed by its quote)"
hasnt "  control: the unbacked why text never appears" "$out" "a fourth try never worked"
out=$(st "$S" lessons --json 2>&1)
heard_topic=$(printf '%s' "$out" | python3 -I -c 'import json,sys; print([d["topic"] for d in json.load(sys.stdin) if d["heard"]][0])')
out=$(st "$S" lesson --topic "$heard_topic" --preview 2>&1)
has "a heard call line shows as not decided yet" "$out" "heard, not decided yet"
has "  and still quotes the call verbatim" "$out" '"we move the retry work to next sprint"'

# ================================================================ only the human takes lessons and quizzes
S=$tmp/s3; seed "$S"
for v in "lesson" "session --topic docs" "digest" "quiz"; do
  out=$(FLEET_ROLE=worker st "$S" $v </dev/null 2>&1); rc=$?
  rc_is "role worker: $v is refused (exit 4)" $rc 4
done
has "  the refusal says only the human" "$out" "only the human"
out=$(FLEET_ROLE=learn st "$S" lesson 2>&1); rc=$?
rc_is "role learn: lesson is refused too" $rc 4
[ ! -s "$S/learn/seen.jsonl" ] && pass "  control: no seen row from a refused role" || fail "  control: no seen row from a refused role"
out=$(FLEET_ROLE=worker st "$S" lesson --preview 2>&1); rc=$?
rc_is "a preview (records nothing) is open to any role" $rc 0
out=$(FLEET_ROLE=worker st "$S" brief --for docs 2>&1); rc=$?
rc_is "brief is read-only and open to any role" $rc 0
out=$(FLEET_ROLE=worker st "$S" rebuild 2>&1); rc=$?
rc_is "role worker: rebuild is refused (exit 4)" $rc 4
out=$(FLEET_ROLE=learn st "$S" rebuild 2>&1); rc=$?
rc_is "role learn: rebuild is allowed" $rc 0

# ================================================================ the 24 h clock starts on seen, not on add
S=$tmp/s4; seed "$S"
NOW=2026-01-12T09:00:00Z st "$S" lesson --topic docs >/dev/null 2>&1
lid=$(st "$S" list --json | python3 -I -c 'import json,sys; print([r["lid"] for r in json.load(sys.stdin) if r["topic"]=="docs"][0])')
cards=$(python3 -I -c 'import json,sys; print(" ".join(c["cid"]+":"+c["format"] for c in map(json.loads, open(sys.argv[1])) if c["lid"]==sys.argv[2] and not c.get("retired")))' "$S/learn/cards.jsonl" "$lid")
rec=$(printf '%s\n' $cards | grep ':recall' | cut -d: -f1); src=$(printf '%s\n' $cards | grep ':source' | cut -d: -f1)
NOW=2026-01-12T10:00:00Z st "$S" review "$rec" --answer "setup needs Python 3.8" --rating good >/dev/null 2>&1
NOW=2026-01-12T20:00:00Z st "$S" review "$src" --answer "setup needs Python 3.8" >/dev/null 2>&1
learned=$(cat "$S/learn/proof.json" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]]["learned"])' "$lid")
[ "$learned" = False ] && pass "passes 11 h after seen do not prove it (the row was added days before)" || fail "passes 11 h after seen do not prove it (got $learned)"
first=$(cat "$S/learn/proof.json" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]]["firstSeen"])' "$lid")
[ "$first" = 2026-01-12T09:00:00Z ] && pass "firstSeen is the lesson time" || fail "firstSeen is the lesson time (got $first)"
NOW=2026-01-13T10:00:00Z st "$S" review "$src" --answer "setup needs Python 3.8" >/dev/null 2>&1
learned=$(cat "$S/learn/proof.json" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]]["learned"])' "$lid")
[ "$learned" = True ] && pass "a checkable pass 25 h after seen proves it" || fail "a checkable pass 25 h after seen proves it (got $learned)"
out=$(st "$S" proof --check 2>&1); has "proof.json matches a rebuild" "$out" "OK proof.json matches"

# ================================================================ session: lesson, quiz on that topic only, one line
S=$tmp/s5; seed "$S"
out=$(printf '\ng\n\ng\n\ng\n\ng\nq\n' | st "$S" session --topic docs 2>&1); rc=$?
rc_is "session exits 0" $rc 0
has "session teaches first" "$out" "Lesson: docs"
has "  then quizzes" "$out" "card(s) due"
hasnt "  control: no card from another topic" "$out" "cap upload retries"
line=$(printf '%s\n' "$out" | tail -1)
case $line in "session docs: 1 lesson(s), "*" card(s), "*" pass, "*" fail, 0 learned") pass "session ends with one result line, 0 learned on first exposure" ;;
  *) fail "session ends with one result line, 0 learned on first exposure (got: $line)" ;; esac
out=$(st "$S" session --topic nosuch </dev/null 2>&1)
has "  control: a session on a topic with nothing waiting says so" "$out" "no new lessons on nosuch"

# ================================================================ digest
S=$tmp/s6; seed "$S"
out=$(NOW=2026-01-10T11:00:00Z st "$S" digest --since 1h --preview 2>&1)
has "digest --since 1h shows nothing older (rows arrived at 09:00)" "$out" "nothing new since 2026-01-10T10:00:00Z"
out=$(st "$S" digest --since 2026-01-01T00:00:00Z 2>&1); rc=$?
rc_is "digest exits 0" $rc 0
has "digest teaches every new row" "$out" "OK digest since 2026-01-01T00:00:00Z"
out=$(NOW=2026-01-11T09:00:00Z st "$S" digest 2>&1)
has "a second digest starts at the last one" "$out" "nothing new since 2026-01-10T09:00:00Z"
out=$(st "$S" digest --since yesterday 2>&1); rc=$?
rc_is "  control: a bad --since is refused (exit 1)" $rc 1

# ================================================================ brief: selector, fallback, read-only
S=$tmp/s7; seed "$S"
out=$(st "$S" brief --for acme/widget 2>&1)
has "brief shows the rows on X's subtree" "$out" "cap upload retries at 3"
hasnt "  control: not rows from another topic" "$out" "Python 3.8"
out=$(st "$S" brief --for nosuch 2>&1)
has "brief on an unknown X falls back to the most recent rows" "$out" "no rows on nosuch, most recent shown"
cat > "$tmp/sel.sh" <<'EOF'
#!/bin/sh
cat >/dev/null; echo L99; echo L3
EOF
chmod +x "$tmp/sel.sh"
printf '{"scheduler":"fixed","brief":{"selector":"%s","timeoutSec":5}}\n' "$tmp/sel.sh" > "$S/learn/learn.json"
out=$(st "$S" brief --for acme/widget --json 2>&1)
printf '%s' "$out" | python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert [c["lid"] for c in d["context"]]==["L3"], d' \
  && pass "a selector picks the rows (an unknown lid is dropped)" || fail "a selector picks the rows (got $out)"
printf '{"scheduler":"fixed","brief":{"selector":"exit 7","timeoutSec":5}}\n' > "$S/learn/learn.json"
out=$(st "$S" brief --for acme/widget 2>&1)
has "a failing selector falls back and says so" "$out" "fallback used"
has "  the fallback still shows X's rows" "$out" "cap upload retries at 3"
[ ! -e "$S/learn/seen.jsonl" ] && pass "brief records nothing seen" || fail "brief records nothing seen"

# ================================================================ entities and rebuild
S=$tmp/s8; seed "$S"
out=$(st "$S" entity tree 2>&1)
has "entity tree puts the PR under its repo" "$out" "acme/widget (repo)
  acme/widget#7 (pr)"
st "$S" lesson --topic acme/widget#7 >/dev/null 2>&1; st "$S" lesson --topic docs >/dev/null 2>&1
nseen=$(nlines "$S/learn/seen.jsonl"); before=$(st "$S" entity list --json | jq -c '[.[]|select(.id!="docs")]')
out=$(st "$S" rebuild 2>&1); rc=$?
rc_is "rebuild exits 0" $rc 0
has "rebuild reports what it kept" "$out" "seen"
after=$(st "$S" entity list --json | jq -c '[.[]|select(.id!="docs")]')
[ "$before" = "$after" ] && pass "the entity links come back after a rebuild" || fail "the entity links come back after a rebuild"
has "  a manual add is not a source: its seen row is reported orphaned" "$out" "orphaned 0 review(s), 1 seen"
[ "$(nlines "$S/learn/seen.jsonl")" = "$nseen" ] && pass "rebuild keeps seen.jsonl" || fail "rebuild keeps seen.jsonl"
out=$(st "$S" lesson --topic acme/widget#7 2>&1)
has "  so a seen lesson stays seen" "$out" "no new lessons on acme/widget#7"
[ -d "$S/learn/rebuild-prev" ] && pass "the old derived files are kept in rebuild-prev/" || fail "the old derived files are kept in rebuild-prev/"
out=$(st "$S" proof --check 2>&1); has "proof matches after a rebuild" "$out" "OK proof.json matches"

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_learn_lesson.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_learn_lesson.sh: all assertions"
