#!/usr/bin/env bash
# test_learn.sh: fleet learn (bin/fleet-learn, lib/fleet_learn.py) on a temp FLEET_STATE. One PASS/FAIL line per
# assertion. Phase 3 acceptance (docs/design/four-desks.md section 7, phase 3) plus controls that must fail.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
FL=$ROOT/bin/fleet-learn
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-learn.XXXXXX" | sed "s#//#/#g")
tmp=$(cd "$tmp" && pwd -P)
trap 'rm -rf "$tmp"' EXIT
export FLEET_STATE=$tmp/state
unset FLEET_ROLE FLEET_LEARN_NOW
L=$FLEET_STATE/learn
LED=$L/ledger.jsonl
fails=0

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
expect() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
expect_not() { local label=$1; shift; if "$@" >/dev/null 2>&1; then fail "$label"; else pass "$label"; fi; }
contains() { case $1 in *"$2"*) return 0 ;; *) return 1 ;; esac; }
has() { if contains "$2" "$3"; then pass "$1"; else fail "$1 (missing: $3; got: $2)"; fi; }
hasnt() { if contains "$2" "$3"; then fail "$1 (unexpected: $3)"; else pass "$1"; fi; }
rc_is() { if [ "$2" -eq "$3" ]; then pass "$1"; else fail "$1 (rc=$2, want $3)"; fi; }
nlines() { if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi; }
# Phase 3 checks pin the fixed 1/3/7/21 d scheduler (the default is fsrs, tested at the end).
pin_fixed() { [ -e "$1/learn/learn.json" ] || { mkdir -p "$1/learn" && printf '%s\n' '{"scheduler":"fixed"}' > "$1/learn/learn.json"; }; }
fl() { pin_fixed "$FLEET_STATE"; "$FL" "$@"; }
jqs() { local st=$1; shift; pin_fixed "$st"; FLEET_STATE=$st "$FL" "$@"; }
# Teach before test: a card comes due only once its row's lesson was seen. seen_all shows every waiting lesson.
seen_all() { local st=$1 i o; pin_fixed "$st"; for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  o=$(FLEET_STATE=$st "$FL" lesson 2>&1); contains "$o" "no new lessons" && return 0; done; return 1; }

case $FLEET_STATE in "$tmp"/*) ;; *) echo "FAIL refusing to run outside a temp FLEET_STATE"; exit 1 ;; esac

# ================================================================ help and wiring
out=$(fl -h 2>&1); rc=$?
rc_is "fleet-learn -h exits 0" $rc 0
for v in add list show confirm drop sync lesson lessons session digest brief curate entity rebuild sources due quiz review proof snapshot export serve; do has "-h lists fleet learn $v" "$out" "fleet learn $v"; done
fl >/dev/null 2>&1; rc=$?
rc_is "bare fleet-learn is a usage error (exit 2)" $rc 2
mkdir -p "$tmp/prefix/bin" && ln -s "$FL" "$tmp/prefix/bin/fleet-learn"
expect "fleet-learn works through an installer-style bin symlink" "$tmp/prefix/bin/fleet-learn" list

# ================================================================ sync with no decisions log
out=$(fl sync 2>&1); rc=$?
rc_is "sync with no decisions-log.jsonl exits 0" $rc 0
has "sync says there is no decisions log yet" "$out" "decisions log: none yet"
[ ! -e "$LED" ] && pass "sync with no log writes no ledger rows" || fail "sync with no log writes no ledger rows"

# ================================================================ add and its controls
out=$(fl add --kind decision --origin fleet --source decisions-log:Q12 --quote "a: cap at 3, jittered" --title "Retry cap" --tag widget 2>&1); rc=$?
rc_is "add exits 0" $rc 0
has "add prints the new id" "$out" "OK added L1 (fleet, confirmed)"
expect "ledger row per 4.1: lid, kind, origin, status, source{type,ref}, quote, topic" jq -se '.[0] | .lid=="L1" and .kind=="decision" and .origin=="fleet" and .status=="confirmed" and .source=={"type":"decisions-log","ref":"Q12"} and .quote=="a: cap at 3, jittered" and .topic=="widget"' "$LED"
n0=$(nlines "$LED")
fl add --kind decision --origin fleet --source manual:x >/dev/null 2>&1; rc=$?
rc_is "control: add without --quote is a usage error (2)" $rc 2
out=$(fl add --kind decision --origin fleet --source manual:x --quote "" 2>&1); rc=$?
rc_is "control: add with an empty quote exits 1" $rc 1
has "control: the refusal names the quote" "$out" "FAIL quote"
expect_not "control: whitespace-only quote refused" fl add --kind decision --origin fleet --source manual:x --quote "   "
out=$(fl add --kind decision --origin elsewhere --source manual:x --quote q 2>&1); rc=$?
rc_is "control: bad origin exits 1" $rc 1
has "control: bad origin names fleet|outside" "$out" "origin must be fleet or outside"
expect_not "control: bad kind refused" fl add --kind rumor --origin fleet --source manual:x --quote q
expect_not "control: add --status superseded refused" fl add --kind decision --origin fleet --source manual:x --quote q --status superseded
expect_not "control: empty source refused" fl add --kind decision --origin fleet --source "" --quote q
[ "$(nlines "$LED")" = "$n0" ] && pass "control: refused adds wrote nothing" || fail "control: refused adds wrote nothing"

# ================================================================ heard -> confirmed needs another source
fl add --kind decision --origin fleet --status heard --source call:standup-1 --quote "we ship friday" >/dev/null
expect "a heard row gets no cards (never taught as decided)" bash -c '! jq -e "select(.lid==\"L2\")" "$1" >/dev/null 2>&1' _ "$L/cards.jsonl"
expect_not "control: confirm with its own heard source refused" fl confirm L2 --source call:standup-1 --quote "we ship friday"
expect_not "control: confirm with an empty quote refused" fl confirm L2 --source decisions-log:Q40 --quote ""
fl confirm L2 --quote x >/dev/null 2>&1; rc=$?
rc_is "control: confirm without --source is a usage error (2)" $rc 2
expect "L2 still heard after refused confirms" bash -c '"$1" show L2 --json | jq -e ".status==\"heard\""' _ "$FL"
expect "confirm with a desk answer source" fl confirm L2 --source decisions-log:Q40 --quote "yes, ship friday"
expect "confirmed row records confirmedBy, keeps its own quote" bash -c '"$1" show L2 --json | jq -e ".status==\"confirmed\" and .confirmedBy.source.ref==\"Q40\" and .quote==\"we ship friday\""' _ "$FL"
expect "a confirmed row gets cards" jq -se '[.[]|select(.lid=="L2")]|length>=2' "$L/cards.jsonl"
expect_not "control: confirming a confirmed row refused" fl confirm L1 --source decisions-log:Q99 --quote x
expect_not "control: show a path-shaped id fails" fl show ../x
expect_not "control: drop without a reason refused" fl drop L1 --reason ""

# ================================================================ ACCEPTANCE: decided-on-call stays heard (sync fixture)
S2=$tmp/s2; mkdir -p "$S2"
cat > "$S2/decisions.jsonl" <<'EOF'
{"id":"Q1","at":"2026-01-01T00:00:00Z","from":"lane-a","question":"Cap the retries?","options":["a","b"],"status":"answered","answer":"a"}
EOF
cat > "$S2/decisions-log.jsonl" <<'EOF'
{"id":"Q1","at":"2026-01-01T00:01:00Z","asker":"lane-a","verbatim":"a: cap at 3","relayedTo":["lane-a"]}
{"id":"Q2","at":"2026-01-01T00:02:00Z","asker":"lane-b","verbatim":"b","relayedTo":["lane-b"],"retry":true}
{"id":"Q3","at":"2026-01-01T00:03:00Z","asker":"lane-c","verbatim":"","relayedTo":[]}
not json
{"id":null,"at":"2026-01-01T00:04:00Z","asker":"SPEAKER_01","verbatim":"we move PROJ-1 to next sprint","relayedTo":["ROUTE"],"kind":"decided-on-call","confirmed":false,"from":"WATCH","clock":"00:12:01"}
{"id":null,"at":"2026-01-01T00:05:00Z","asker":"SPEAKER_02","verbatim":"keep the old port","relayedTo":["ROUTE"],"kind":"decided-on-call","confirmed":false,"from":"WATCH"}
{"id":"Q4","at":"2026-01-01T00:06:00Z","asker":"lane-d","verbatim":"confirmed: Keep the old port.","relayedTo":["lane-d"]}
EOF
out=$(jqs "$S2" sync 2>&1); rc=$?
rc_is "sync on the fixture exits 0" $rc 0
has "sync read 7 rows, skipped 3 (retry, empty, malformed)" "$out" "read 7 rows, skipped 3"
has "sync: 4 added, 1 confirmed" "$out" "added 4, superseded 0, confirmed 1"
expect "ACCEPT phase3: a decided-on-call row stays heard" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.quote==\"we move PROJ-1 to next sprint\")] | length==1 and .[0].status==\"heard\" and .[0].source.kind==\"decided-on-call\""' _ "$FL" "$S2"
expect "  control: a desk answer row is confirmed, titled from decisions.jsonl" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.source.ref==\"Q1\")][0] | .status==\"confirmed\" and .title==\"Cap the retries?\" and .origin==\"fleet\""' _ "$FL" "$S2"
expect "join rule: a heard row quoted by a desk answer becomes confirmed" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.quote==\"keep the old port\")][0] | .status==\"confirmed\" and .confirmedBy.source.ref==\"Q4\""' _ "$FL" "$S2"
expect "the heard row has no cards" bash -c 'lid=$(FLEET_STATE=$2 "$1" list --json | jq -r ".[]|select(.status==\"heard\")|.lid"); ! grep -q "\"lid\": \"$lid\"" "$3"' _ "$FL" "$S2" "$S2/learn/cards.jsonl"
expect "retry and empty rows made no rows" bash -c 'FLEET_STATE=$2 "$1" list --json --all | jq -e "[.[]|select(.source.ref==\"Q2\" or .source.ref==\"Q3\")]|length==0"' _ "$FL" "$S2"
n1=$(nlines "$S2/learn/ledger.jsonl")
out=$(jqs "$S2" sync 2>&1)
has "sync again adds nothing" "$out" "added 0, superseded 0, confirmed 0, new cards 0"
[ "$(nlines "$S2/learn/ledger.jsonl")" = "$n1" ] && pass "sync again writes no ledger rows" || fail "sync again writes no ledger rows"
printf '%s\n' '{"id":"Q1","at":"2026-01-02T00:00:00Z","asker":"lane-a","verbatim":"revised: cap at 4","relayedTo":["lane-a"]}' >> "$S2/decisions-log.jsonl"
out=$(jqs "$S2" sync 2>&1)
has "a newer answer for Q1 supersedes its row" "$out" "added 0, superseded 1"
expect "superseded row retired, new row supersedes it, its cards retired" bash -c 'FLEET_STATE=$2 "$1" list --json --all | jq -e "[.[]|select(.source.ref==\"Q1\")] | (map(.status)|sort)==[\"confirmed\",\"superseded\"] and (map(select(.status==\"confirmed\"))[0].supersedes==\"L1\")" && jq -se "[.[]|select(.lid==\"L1\")]|all(.retired==true)" "$3"' _ "$FL" "$S2" "$S2/learn/cards.jsonl"

# ---- WATCH LEARN-ITEMs and goal files
mkdir -p "$S2/watch"
cat > "$S2/watch/events.jsonl" <<'EOF'
{"eid":"github:acme/widget:7:merged","source":"acme-gh","sourceKind":"github","kind":"fyi","at":"2026-01-03T00:00:00Z","type":"pr.merged","ref":"acme/widget#7","url":"https://example.com/acme/widget/pull/7","title":"Retry the upload on 503","quote":"Merged: retry the upload on 503","origin":"outside","score":2,"to":"DECIDE"}
{"eid":"github:acme/widget:8:labels","source":"acme-gh","kind":"fyi","type":"issue.field:labels","ref":"acme/widget#8","quote":"label added","origin":"outside","score":0,"to":"DECIDE"}
EOF
out=$(jqs "$S2" sync 2>&1)
has "a LEARN-ITEM above the noise bar is added (one)" "$out" "added 1,"
expect "the WATCH row is origin outside, kind change, keyed by eid" bash -c 'FLEET_STATE=$2 "$1" list --origin outside --json | jq -e "length==1 and .[0].kind==\"change\" and .[0].source.type==\"watch\" and .[0].source.ref==\"github:acme/widget:7:merged\""' _ "$FL" "$S2"
expect "  control: a score-0 event made no row" bash -c 'FLEET_STATE=$2 "$1" list --all --json | jq -e "[.[]|select(.quote==\"label added\")]|length==0"' _ "$FL" "$S2"
out=$(jqs "$S2" sync 2>&1)
has "events are not re-read (cursor)" "$out" "added 0,"
cat > "$tmp/lane-x.goal.json" <<'EOF'
{"goal":"Ship PROJ-1","done":[{"phase":"P1","evidence":"PROJ-1: uploads now retry 3 times on a 503"},"wired the dispatch line","issues filed"],"decisions":[{"id":"D1","decision":"use stdlib only"}]}
EOF
out=$(jqs "$S2" sync --goal "$tmp/lane-x.goal.json" --lane lane-x 2>&1)
has "a finished lane's goal file adds its work done[] and decisions[] (2)" "$out" "added 2,"
has "  its process steps and step names are skipped (2)" "$out" "skipped: plumbing 2"
expect "goal rows are lessons/decisions with origin fleet and topic = the work subject" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.source.type==\"goal\")] | length==2 and all(.origin==\"fleet\" and .topic==\"PROJ-1\") and (map(.kind)|sort)==[\"decision\",\"done\"]"' _ "$FL" "$S2"
out=$(jqs "$S2" sync --goal "$tmp/lane-x.goal.json" --lane lane-x 2>&1)
has "  control: the same goal file again adds 0" "$out" "added 0,"
python3 -I -c 'import json,sys; json.dump({"lane-y":{"name":"lane-y","goalState":"achieved"},"lane-z":{"name":"lane-z","goalState":"active"}}, open(sys.argv[1],"w"))' "$S2/roster.json"
mkdir -p "$S2/launch"
cp "$tmp/lane-x.goal.json" "$tmp/lane-y.goal.json"; cp "$tmp/lane-x.goal.json" "$tmp/lane-z.goal.json"
printf '/goal follow %s; the file wins, or stop after 40 turns\n' "$tmp/lane-y.goal.json" > "$S2/launch/lane-y.txt"
printf '/goal follow %s; the file wins, or stop after 40 turns\n' "$tmp/lane-z.goal.json" > "$S2/launch/lane-z.txt"
out=$(jqs "$S2" sync 2>&1)
has "sync reads the goal file of a roster lane whose goalState is achieved" "$out" "added 2,"
expect "  control: the active lane's goal file was not read" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.topic==\"lane-z\")]|length==0"' _ "$FL" "$S2"

# ================================================================ ACCEPTANCE: multiple-choice distractors are other rows' text
for q in "PR 7 adds jittered backoff" "PR 7 keeps the old port" "PR 7 needs two approvals"; do
  jqs "$S2" add --kind change --origin outside --source manual:notes --topic acme/widget#7 --quote "$q" >/dev/null; done
expect "ACCEPT phase3: every choice distractor is another ledger row's quote" python3 -I - "$S2/learn" <<'PY'
import json, sys, os
d = sys.argv[1]
rows = [json.loads(l) for l in open(os.path.join(d, "ledger.jsonl")) if l.strip()]
cards = [json.loads(l) for l in open(os.path.join(d, "cards.jsonl")) if l.strip()]
choice = [c for c in cards if c["format"] == "choice"]
assert choice, "no choice cards"
for c in choice:
    others = {r["quote"] for r in rows if r["lid"] != c["lid"]}
    own = {r["quote"] for r in rows if r["lid"] == c["lid"]}
    assert len(c["options"]) == 4 and c["answer"] in c["options"] and c["answer"] in own, c
    ds = [o for o in c["options"] if o != c["answer"]]
    assert len(ds) == 3 and all(o in others for o in ds), (c["cid"], ds)
    me = [r for r in rows if r["lid"] == c["lid"]][-1]
    tops = {r["quote"]: r["topic"] for r in rows if r["lid"] != c["lid"]}
    assert all(tops[o].lower() == me["topic"].lower() for o in ds), ("distractor off topic", c["cid"], ds)
PY
S5=$tmp/s5
for q in "one fact here" "two facts here" "three facts here"; do jqs "$S5" add --kind decision --origin fleet --source manual:x --quote "$q" >/dev/null; done
expect "  control: with only 2 other rows there is no choice card (never an invented distractor)" bash -c '! grep -q "\"format\": \"choice\"" "$1"' _ "$S5/learn/cards.jsonl"
jqs "$S5" add --kind decision --origin fleet --source manual:x --quote "four facts here" >/dev/null
expect "  control: a 4th row makes choice cards" grep -q '"format": "choice"' "$S5/learn/cards.jsonl"

# ================================================================ ACCEPTANCE: source check is a verbatim substring
S3=$tmp/s3
jqs "$S3" add --kind decision --origin fleet --source decisions-log:Q7 --quote "a: cap retries at 3, jittered 1 s to 8 s" --title "Retry cap" >/dev/null
src=$(jq -r 'select(.format=="source")|.cid' "$S3/learn/cards.jsonl")
rec=$(jq -r 'select(.format=="recall")|.cid' "$S3/learn/cards.jsonl")
out=$(jqs "$S3" review "$src" --answer "cap retries at three" 2>&1)
has "ACCEPT phase3: a source-check answer that is not a verbatim substring fails" "$out" "fail (verbatim)"
out=$(jqs "$S3" review "$src" --answer "cap retries at 3" 2>&1)
has "  control: a verbatim substring passes" "$out" "pass (verbatim)"
out=$(jqs "$S3" review "$src" --answer "a" 2>&1)
has "  control: a too-short substring fails" "$out" "fail (verbatim)"
expect_not "control: recall needs a rating" jqs "$S3" review "$rec" --answer "cap at 3"

# ================================================================ ACCEPTANCE: 24 h spacing with an injected clock
S4=$tmp/s4
export FLEET_LEARN_NOW=2026-03-01T09:00:00Z
jqs "$S4" add --kind decision --origin fleet --source decisions-log:Q8 --quote "uploads stop after 3 tries" --title "Upload retries" >/dev/null
src=$(jq -r 'select(.format=="source")|.cid' "$S4/learn/cards.jsonl")
rec=$(jq -r 'select(.format=="recall")|.cid' "$S4/learn/cards.jsonl")
jqs "$S4" review "$rec" --answer "3 tries" --rating good >/dev/null
FLEET_LEARN_NOW=2026-03-01T15:00:00Z jqs "$S4" review "$src" --answer "stop after 3 tries" >/dev/null
expect "ACCEPT phase3: a same-day pass (generative + checkable) does not mark learned" jq -e '.L1.learned==false and (.L1.passes|length)==2' "$S4/learn/proof.json"
FLEET_LEARN_NOW=2026-03-02T10:00:00Z jqs "$S4" review "$src" --answer "uploads stop after 3 tries" >/dev/null
expect "ACCEPT phase3: a pass faked 25 h later (clock injected) does" jq -e '.L1.learned==true and .L1.firstSeen=="2026-03-01T09:00:00Z"' "$S4/learn/proof.json"
expect "snapshot: fleet 1 of 1 learned, outside 0 (never blended)" bash -c 'FLEET_STATE=$2 "$1" snapshot | jq -e ".byOrigin.fleet.learned==1 and .byOrigin.fleet.items==1 and .byOrigin.outside.items==0 and (has(\"total\")|not) and (.topics[0].pct==100)"' _ "$FL" "$S4"
FLEET_LEARN_NOW=2026-03-03T10:00:00Z jqs "$S4" review "$src" --answer "nope, not in it" >/dev/null
expect "a lapse after the last pass unmarks learned" jq -e '.L1.learned==false and .L1.lapsedAt=="2026-03-03T10:00:00Z"' "$S4/learn/proof.json"
# control: two passes 25 h apart but only checkable ones: not learned (needs a generative pass too)
S6=$tmp/s6
FLEET_LEARN_NOW=2026-03-01T09:00:00Z jqs "$S6" add --kind decision --origin fleet --source manual:x --quote "only checkable passes here" >/dev/null
s6=$(jq -r 'select(.format=="source")|.cid' "$S6/learn/cards.jsonl")
FLEET_LEARN_NOW=2026-03-01T09:00:00Z jqs "$S6" review "$s6" --answer "only checkable passes" >/dev/null
FLEET_LEARN_NOW=2026-03-02T10:00:00Z jqs "$S6" review "$s6" --answer "checkable passes here" >/dev/null
expect "  control: spaced checkable passes alone do not mark learned" jq -e '.L1.learned==false' "$S6/learn/proof.json"
unset FLEET_LEARN_NOW

# ---- fixed-interval schedule
expect "recall rated good at step 0 is due 1 d later" bash -c 'jq -se "[.[]|select(.format==\"recall\")][0].sched | .due==\"2026-03-02T09:00:00Z\" and .fixedStep==1 and .reps==1 and .lastReview==\"2026-03-01T09:00:00Z\"" "$1"' _ "$S4/learn/cards.jsonl"
expect "a failed review resets to step 0 and counts a lapse" bash -c 'jq -se "[.[]|select(.format==\"source\")][0].sched | .fixedStep==0 and .lapses==1" "$1"' _ "$S4/learn/cards.jsonl"

# ================================================================ ACCEPTANCE: proof.json equals a rebuild from reviews.jsonl
expect "ACCEPT phase3: proof.json rebuilt from reviews.jsonl equals the cached one" jqs "$S4" proof --check
cp "$S4/learn/proof.json" "$tmp/proof.bak"
python3 -I -c 'import json,sys; p=json.load(open(sys.argv[1])); p["L1"]["learned"]=not p["L1"]["learned"]; json.dump(p,open(sys.argv[1],"w"))' "$S4/learn/proof.json"
expect_not "  control: a tampered proof.json fails the check" jqs "$S4" proof --check
rm "$S4/learn/proof.json"
jqs "$S4" proof --rebuild >/dev/null
expect "  rebuild from reviews.jsonl alone restores the same proof" bash -c 'diff <(jq -S . "$1") <(jq -S . "$2")' _ "$tmp/proof.bak" "$S4/learn/proof.json"

# ================================================================ quiz TUI (stdin-driven)
S7=$tmp/s7
for q in "alpha fact one" "beta fact two" "gamma fact three" "delta fact four"; do jqs "$S7" add --kind decision --origin fleet --source manual:x --quote "$q" >/dev/null; done
seen_all "$S7"
c1=$(jq -r 'select(.lid=="L1" and .format=="recall")|.cid' "$S7/learn/cards.jsonl")
out=$(printf '\ng\nq\n' | FLEET_STATE=$S7 "$FL" quiz 2>&1); rc=$?
rc_is "quiz exits 0" $rc 0
has "quiz shows the origin with the card" "$out" "(recall, fleet)"
has "quiz reveals the source quote after a recall" "$out" "source quote: alpha fact one"
has "quiz ends with the per-origin status line" "$out" "fleet: 0 of 4 learned"
expect "quiz wrote exactly one review, through review()" bash -c 'test "$(wc -l < "$1" | tr -d " ")" = 1 && jq -e ".cid==\"$2\" and .grade==\"pass\" and .gradedBy==\"self\"" "$1"' _ "$S7/learn/reviews.jsonl" "$c1"
expect "quiz kept proof.json in step with reviews.jsonl" jqs "$S7" proof --check
out=$(printf 'q\n' | FLEET_STATE=$tmp/empty "$FL" quiz 2>&1)
has "quiz with nothing due says so" "$out" "nothing due"

# ================================================================ export and the write fence
out=$(jqs "$S2" export 2>&1); rc=$?
rc_is "export (default dir) exits 0" $rc 0
expect "export writes one markdown per topic with verbatim quotes and origin" bash -c 'grep -q "^> Merged: retry the upload on 503" "$1/learn/export/acme-widget-7.md" && grep -q "origin: outside" "$1/learn/export/acme-widget-7.md" && test -f "$1/learn/export/PROJ-1.md"' _ "$S2"
expect "human export --to an outside dir works" env FLEET_STATE="$S2" "$FL" export --to "$tmp/notes"
out=$(FLEET_STATE=$S2 FLEET_ROLE=learn "$FL" export --to "$tmp/escape" 2>&1); rc=$?
rc_is "control: role learn export outside learn/ exits 1" $rc 1
has "control: the refusal names the rule" "$out" "role learn may export only under"
[ ! -e "$tmp/escape" ] && pass "control: refused export created nothing" || fail "control: refused export created nothing"
expect "role learn export inside learn/ works" env FLEET_STATE="$S2" FLEET_ROLE=learn "$FL" export --to "$S2/learn/export2"
S8=$tmp/s8; mkdir -p "$S8/learn"; printf 'outside\n' > "$tmp/outside.jsonl"
ln -s "$tmp/outside.jsonl" "$S8/learn/ledger.jsonl"
out=$(jqs "$S8" add --kind decision --origin fleet --source manual:x --quote q 2>&1); rc=$?
rc_is "control: a ledger symlinked outside learn/ is refused" $rc 1
has "control: the refusal says write outside" "$out" "write outside"
[ "$(cat "$tmp/outside.jsonl")" = "outside" ] && pass "control: the outside file is unchanged" || fail "control: the outside file is unchanged"
before=$(cd "$S2" && find . -type f -not -path './learn/*' | LC_ALL=C sort | xargs cat | cksum)
jqs "$S2" sync >/dev/null; jqs "$S2" add --kind done --origin fleet --source manual:y --quote "zz top" >/dev/null
after=$(cd "$S2" && find . -type f -not -path './learn/*' | LC_ALL=C sort | xargs cat | cksum)
[ "$before" = "$after" ] && pass "sync and add change nothing in FLEET_STATE outside learn/" || fail "sync and add change nothing in FLEET_STATE outside learn/"

# ================================================================ Python API (contract)
expect "API: add_item, add_learn_item dedupe, items, snapshot via import" python3 -I - "$ROOT/lib" "$tmp/s9" <<'PY'
import os, sys
os.environ["FLEET_STATE"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import fleet_learn as fl
r = fl.add_item(kind="decision", origin="fleet", quote="the human's own words", source={"type": "decide-learn-this", "ref": "Q12"}, by="decide")
assert r["lid"] == "L1" and r["by"] == "decide"
ev = {"eid": "jira:ACME:PROJ-1:t:status", "quote": "Status: In Review -> Done", "type": "issue.transition", "ref": "PROJ-1", "score": 2}
a = fl.add_learn_item(ev); b = fl.add_learn_item(ev)
assert a and a["origin"] == "outside" and b is None
try:
    fl.add_item(kind="decision", origin="fleet", quote="", source={"type": "manual", "ref": "x"}); raise SystemExit("no refusal")
except fl.Refused:
    pass
s = fl.snapshot()
assert set(s) >= {"at", "dir", "byOrigin", "topics", "due", "heard", "recent"} and set(s["byOrigin"]) == {"fleet", "outside"}
seen = []
fl.use_scheduler(lambda sched, rating, now: dict(sched, due="2099-01-01T00:00:00Z", stability=1.0, seen=seen.append(rating)))
c = [c for c in fl.make_cards() + fl.due() if c["format"] == "recall"][0]
fl.review(c["cid"], "x", rating="easy")
assert seen == ["easy"] and fl.card(c["cid"])["sched"]["stability"] == 1.0
PY
expect "API: explain/apply cards on request, an external grade counts as a generative pass" python3 -I - "$ROOT/lib" "$tmp/s10" <<'PY'
import os, sys, datetime
os.environ["FLEET_STATE"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import fleet_learn as fl
fl.use_scheduler(fl.fixed_schedule)
t0 = datetime.datetime(2026, 4, 1, 9, tzinfo=datetime.timezone.utc)
r = fl.add_item(kind="decision", origin="fleet", quote="cap at 3", what="retries stop at three", why="a 4th never worked",
                source={"type": "manual", "ref": "x"}, now=t0)
fl.mark_seen([r["lid"]], now=t0)
assert [c for c in fl.make_cards(now=t0) if c["format"] in ("explain", "apply")] == [], "phase 3 default made explain"
new = {c["format"]: c for c in fl.make_cards(now=t0, formats=["explain", "apply"])}
assert set(new) == {"explain", "apply"} and new["explain"]["answer"] == "retries stop at three", new
try:
    fl.review(new["explain"]["cid"], "my words", now=t0); raise SystemExit("explain without a grade accepted")
except fl.Refused:
    pass
fl.review(new["explain"]["cid"], "my words", rating="good", grade="pass", gradedBy="self", detail={"covered": ["cap"]}, now=t0)
src = [c for c in fl.due(now=t0) if c["format"] == "source"][0]
fl.review(src["cid"], "cap at 3", now=t0 + datetime.timedelta(hours=25))
p = fl.proof()["L1"]
assert p["learned"] and [x["kind"] for x in p["passes"]] == ["generative", "checkable"], p
assert len(fl.reviews("L1")) == 2
PY

# ================================================================ phase 5 hooks (stub modules on a temp lib copy)
SL=$tmp/stublib; mkdir -p "$SL/lib" "$SL/bin"; cp "$ROOT/lib/"*.py "$SL/lib/"; cp "$FL" "$SL/bin/"
rm -f "$SL/lib/fleet_learn_explain.py" "$SL/lib/fleet_fsrs.py"
S11=$tmp/s11; pin_fixed "$S11"
python3 -I -c 'import sys, os; sys.path.insert(0, sys.argv[1]); os.environ["FLEET_STATE"]=sys.argv[2]; import fleet_learn as f
f.add_item(kind="decision", origin="fleet", quote="cap at 3", what="retries stop at three", source={"type": "manual", "ref": "x"})
f.make_cards(formats=["explain"]); f.mark_seen(["L1"])' "$SL/lib" "$S11"
out=$(printf 'q\n' | FLEET_STATE=$S11 "$SL/bin/fleet-learn" quiz 2>&1)
has "  control: without lib/fleet_learn_explain.py the quiz skips explain cards" "$out" "skipped: explain cards need"
cat > "$SL/lib/fleet_learn_explain.py" <<'PY'
def load_config():
    return {}
def quiz_card(fl, c, cfg, inp, out, now, show_prompt=True):
    lines = []
    for line in inp:
        if not line.strip():
            break
        lines.append(line.rstrip("\n"))
    else:
        raise EOFError
    return fl.review(c["cid"], " ".join(lines), rating="good", grade="pass", gradedBy="command", now=now)
PY
out=$(printf 'retries stop\nat three\n\nq\n' | FLEET_STATE=$S11 "$SL/bin/fleet-learn" quiz 2>&1)
has "quiz hands an explain card to fleet_learn_explain.quiz_card" "$out" "PASS. source: \"cap at 3\""
expect "the explain review is recorded through review()" bash -c 'jq -se "map(select(.format==\"explain\")) | length==1 and .[0].gradedBy==\"command\" and .[0].answer==\"retries stop at three\"" "$1"' _ "$S11/learn/reviews.jsonl"
cat > "$SL/lib/fleet_fsrs.py" <<'PY'
import json, os
def configured():
    c = json.load(open(os.path.join(os.environ["FLEET_STATE"], "learn", "learn.json"))).get("fsrs", {})
    if not 0 < c.get("desiredRetention", 0.9) < 1:
        raise ValueError("desiredRetention must be in (0, 1)")
def schedule(sched, rating, now):
    return dict(sched, stability=2.0)
PY
printf '%s\n' '{"scheduler":"fsrs","fsrs":{"desiredRetention":1.5}}' > "$S11/learn/learn.json"
python3 -I -c 'import sys, os; sys.path.insert(0, sys.argv[1]); os.environ["FLEET_STATE"]=sys.argv[2]; import fleet_learn as f; f.make_cards(formats=["source"])' "$SL/lib" "$S11"
n=$(nlines "$S11/learn/reviews.jsonl"); c=$(jq -r 'select(.format=="source")|.cid' "$S11/learn/cards.jsonl")
out=$(FLEET_STATE=$S11 "$SL/bin/fleet-learn" review "$c" --answer "cap at 3" 2>&1); rc=$?
rc_is "control: a bad fsrs section fails at scheduler selection (exit 1)" $rc 1
has "control: the refusal names learn.json fsrs" "$out" "FAIL learn.json fsrs"
[ "$(nlines "$S11/learn/reviews.jsonl")" = "$n" ] && pass "control: no review row appended" || fail "control: no review row appended"
printf '%s\n' '{"scheduler":"fsrs","fsrs":{"desiredRetention":0.9}}' > "$S11/learn/learn.json"
FLEET_STATE=$S11 "$SL/bin/fleet-learn" review "$c" --answer "cap at 3" >/dev/null 2>&1
expect "learn.json scheduler fsrs routes review() through fleet_fsrs.schedule" bash -c 'jq -se "map(select(.cid==\"$2\"))[0].sched.stability==2.0" "$1"' _ "$S11/learn/cards.jsonl" "$c"

# ---- due() orders card ids numerically (C2 before C10)
expect "due(): cards with the same due time come in numeric id order (C2 before C10)" python3 -I - "$ROOT/lib" "$tmp/s13" <<'PY'
import os, sys, datetime
os.environ["FLEET_STATE"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import fleet_learn as fl
t = datetime.datetime(2026, 5, 1, 9, tzinfo=datetime.timezone.utc)
for i in range(6):
    fl.add_item(kind="decision", origin="fleet", quote="row number %d here" % i, source={"type": "manual", "ref": "x"}, now=t)
fl.make_cards(now=t)
fl.mark_seen([r["lid"] for r in fl.items()], now=t)
ids = [c["cid"] for c in fl.due(now=t)]
assert len(ids) >= 11 and "C10" in ids, ids
assert ids == sorted(ids, key=lambda i: int(i[1:])), ids
assert ids != sorted(ids), "control: string order would differ"
PY

# ---- the default scheduler is fsrs; no silent fallback
rm -f "$SL/lib/fleet_fsrs.py"
S12=$tmp/s12
python3 -I -c 'import sys, os; sys.path.insert(0, sys.argv[1]); os.environ["FLEET_STATE"]=sys.argv[2]; import fleet_learn as f
f.add_item(kind="decision", origin="fleet", quote="default sched row", source={"type": "manual", "ref": "x"}); f.make_cards()' "$SL/lib" "$S12"
c=$(jq -r 'select(.format=="source")|.cid' "$S12/learn/cards.jsonl")
out=$(FLEET_STATE=$S12 "$SL/bin/fleet-learn" review "$c" --answer "default sched row" 2>&1); rc=$?
rc_is "default scheduler is fsrs: with no lib/fleet_fsrs.py a review fails loudly (exit 1)" $rc 1
has "  the refusal names fleet_fsrs and the fixed opt-out" "$out" 'needs lib/fleet_fsrs.py'
[ ! -s "$S12/learn/reviews.jsonl" ] && pass "  no review row appended on the scheduler error" || fail "  no review row appended on the scheduler error"
cat > "$SL/lib/fleet_fsrs.py" <<'PY'
def schedule(sched, rating, now):
    return dict(sched, stability=3.0)
PY
FLEET_STATE=$S12 "$SL/bin/fleet-learn" review "$c" --answer "default sched row" >/dev/null 2>&1
expect "default scheduler is fsrs: with no learn.json, review() calls fleet_fsrs.schedule" bash -c 'jq -se "map(select(.cid==\"$2\"))[0].sched | .stability==3.0 and .fixedStep==0" "$1"' _ "$S12/learn/cards.jsonl" "$c"
printf '%s\n' '{"scheduler":"fixed"}' > "$S12/learn/learn.json"
FLEET_STATE=$S12 "$SL/bin/fleet-learn" review "$c" --answer "default sched row" >/dev/null 2>&1
expect "  control: learn.json scheduler fixed opts out (fixedStep set)" bash -c 'jq -se "map(select(.cid==\"$2\"))[0].sched.fixedStep==1" "$1"' _ "$S12/learn/cards.jsonl" "$c"
printf '%s\n' '{"scheduler":"sm2"}' > "$S12/learn/learn.json"
expect_not "  control: an unknown scheduler name is refused" env FLEET_STATE="$S12" "$SL/bin/fleet-learn" review "$c" --answer "default sched row"

# ================================================================ #23 #24: teach meeting material, not fleet plumbing
# Synthetic fixture shaped like a real day: desk chatter, bare option letters, kit lanes, process steps, and the
# work answers the human needs in a meeting. Nothing here comes from a real ledger.
S20=$tmp/s20; mkdir -p "$S20/launch"
cat > "$S20/decisions.jsonl" <<'J'
{"id":"Q21","from":"widget-retry-lane","question":"PROJ-7 upload retries: the vendor API returns 503 under load and the client gives up at once. Cap the retries at 3 with jitter, or retry forever?","options":["(rec) cap at 3, jittered","retry forever"]}
{"id":"Q22","from":"widget-retry-lane","question":"Q21 amendment: one number was wrong; same question, same options.","options":["(rec) cap at 3, jittered"]}
{"id":"Q23","from":"kit-pane-slots","question":"Pin the desk panes to slots 0 and 1?","options":["yes","no"]}
J
cat > "$S20/decisions-log.jsonl" <<'J'
{"id":"Q901","at":"2026-02-01T10:00:00Z","asker":"widget-retry-lane","verbatim":"a","relayedTo":["ROUTE"]}
{"id":"Q21","at":"2026-02-01T10:01:00Z","asker":"widget-retry-lane","verbatim":"Human typed \"a\" (desk Q901): (a) cap PROJ-7 upload retries at 3, jittered 1 s to 8 s, then fail loudly","relayedTo":["widget-retry-lane"]}
{"id":"Q22","at":"2026-02-01T10:02:00Z","asker":"widget-retry-lane","verbatim":"Human typed \"a\" (desk Q901): (a) cap PROJ-7 upload retries at 3, jittered 1 s to 8 s, then fail loudly (merged with Q21)","relayedTo":["widget-retry-lane"]}
{"id":"Q902","at":"2026-02-01T10:03:00Z","asker":"ROUTE","verbatim":"launch two lanes to look into the new concerns","relayedTo":["ROUTE"]}
{"id":"Q903","at":"2026-02-01T10:04:00Z","asker":"ROUTE","verbatim":"17-a","relayedTo":["ROUTE"]}
{"id":"Q904","at":"2026-02-01T10:05:00Z","asker":"widget-retry-lane","verbatim":"yours recs on all = a","relayedTo":["ROUTE"]}
{"id":"Q23","at":"2026-02-01T10:06:00Z","asker":"kit-pane-slots","verbatim":"yes, pin the desk panes to slots 0 and 1 by default","relayedTo":["kit-pane-slots"]}
{"id":"Q905","at":"2026-02-01T10:07:00Z","asker":"ROUTE","verbatim":"refresh everything","relayedTo":["ROUTE"]}
{"id":"Q906","at":"2026-02-01T10:08:00Z","asker":"ROUTE","verbatim":"ship acme/widget#9 before the PROJ-8 demo, it fixes the login loop","relayedTo":["ROUTE"]}
J
cat > "$tmp/kit-pane-slots.goal.json" <<'J'
{"goal":"pin desk panes","done":["git push origin feat:main: abc..def; PR #10 MERGED","Lesson: killing a lane's MCP server does not respawn it"],"decisions":[]}
J
cat > "$tmp/widget-retry-lane.goal.json" <<'J'
{"goal":"Fix PROJ-7 upload retries","done":["Wrote ~/notes/findings.md with the 503 table for PROJ-7","issues filed","acme/widget#9 head 4ae28fc: the retry cap lives in upload.ts and the PR needs one more approval","fleet learn sync ran clean"],"decisions":[{"decision":"PROJ-7 retry cap is 3, jittered"}]}
J
python3 -I -c 'import json,sys; json.dump({"kit-pane-slots":{"name":"kit-pane-slots","goalState":"achieved"},"widget-retry-lane":{"name":"widget-retry-lane","goalState":"achieved"}}, open(sys.argv[1],"w"))' "$S20/roster.json"
printf '/goal follow %s\n' "$tmp/kit-pane-slots.goal.json" > "$S20/launch/kit-pane-slots.txt"
printf '/goal follow %s\n' "$tmp/widget-retry-lane.goal.json" > "$S20/launch/widget-retry-lane.txt"
out=$(jqs "$S20" sync 2>&1); rc=$?
rc_is "#24 sync on the meeting fixture exits 0" $rc 0
expect "#24 sync keeps only the work rows (Q21, Q906, the PR fact and the retry-cap decision)" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "map(.source.type+\":\"+(.source.ref|tostring))|sort == [\"decisions-log:Q21\",\"decisions-log:Q906\",\"goal:widget-retry-lane:decisions[0]\",\"goal:widget-retry-lane:done[2]\"]"' _ "$FL" "$S20"
expect "#23 no row from a plumbing lane, a desk instruction, or a process step" bash -c 'FLEET_STATE=$2 "$1" list --all --json | jq -e "[.[]|select((.source.ref|tostring)|test(\"kit-pane|Q902|Q905|Q23\$|done\\\\[0\\\\]|done\\\\[3\\\\]\"))]|length==0"' _ "$FL" "$S20"
expect "#24 no answerless row (bare letters, recs = a, step names)" bash -c 'FLEET_STATE=$2 "$1" list --all --json | jq -e "[.[]|select(.quote|test(\"^(a|17-a|yours recs on all = a|issues filed)\$\"))]|length==0"' _ "$FL" "$S20"
expect "#24 the bare answer Q901 folds into Q21 (link kept), the repeat Q22 folds too" bash -c 'FLEET_STATE=$2 "$1" sync --json | jq -e "true" >/dev/null && FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.source.ref==\"Q21\")][0].links|map(.ref)|sort==[\"Q22\",\"Q901\"]"' _ "$FL" "$S20"
has "#24 sync reports what it skipped" "$out" "skipped: plumbing"
expect "#24 Q21 topic is its work subject (PROJ-7), not the lane" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.source.ref==\"Q21\")][0].topic==\"PROJ-7\""' _ "$FL" "$S20"
expect "#23 every prompt is a plain question: ends in ?, never 'say about', never the lane or source ref" python3 -I - "$S20/learn" <<'PY'
import json, os, sys
cards = [json.loads(l) for l in open(os.path.join(sys.argv[1], "cards.jsonl")) if l.strip()]
assert cards
for c in cards:
    p = c["prompt"]
    assert p.endswith("?"), p
    assert "say about" not in p and "Paste the line" not in p and "Which line is the source" not in p, p
    for bad in ("widget-retry-lane", "kit-pane-slots", "decisions-log", "Q21", "Q906", "goal:"):
        assert bad not in p, (bad, p)
PY
out=$(jqs "$S20" sync 2>&1)
has "#24 a second sync adds nothing" "$out" "added 0, superseded 0, confirmed 0, new cards 0"

# ---- title verbatim
S21=$tmp/s21
jqs "$S21" add --kind change --origin fleet --source route-done:widget-retry-lane --quote "acme/widget#9 adds jittered retries to the upload client" --title "Which PR adds X?" >/dev/null
expect "#23 a card with --title uses it verbatim as the prompt" jq -se 'length>=2 and all(.prompt=="Which PR adds X?")' "$S21/learn/cards.jsonl"

# ---- overrides: learn: teach / learn: skip
S22=$tmp/s22; mkdir -p "$S22"
cat > "$tmp/kit-y.goal.json" <<'J'
{"goal":"kit work","done":["learn: teach. the fleet kit now pins desk panes to slots 0 and 1 by default","git log -1 = abc"]}
J
cat > "$tmp/work-z.goal.json" <<'J'
{"goal":"PROJ-9 cleanup","learn":"skip","done":["PROJ-9: the cleanup job now runs nightly at 02:00"]}
J
out=$(jqs "$S22" sync --goal "$tmp/kit-y.goal.json" --lane kit-y 2>&1)
has "#24 'learn: teach' in a done line keeps it, even in a plumbing lane" "$out" "added 1,"
out=$(jqs "$S22" sync --goal "$tmp/work-z.goal.json" --lane work-z 2>&1)
has "#24 {\"learn\": \"skip\"} in a goal file skips the whole lane" "$out" "added 0,"

# ---- route-done adds: plumbing refused unless taught
S23=$tmp/s23
out=$(jqs "$S23" add --kind done --origin fleet --source route-done:kit-pane-slots --quote "route-done: kit-pane-slots parked (goal met). desk panes pinned to slots 0 and 1" 2>&1); rc=$?
rc_is "#23 a plumbing route-done add exits 0" $rc 0
has "#23 a plumbing route-done add says SKIP" "$out" "SKIP route-done:kit-pane-slots"
[ ! -s "$S23/learn/ledger.jsonl" ] && pass "#23 the skipped route-done add wrote no row" || fail "#23 the skipped route-done add wrote no row"
out=$(jqs "$S23" add --kind done --origin fleet --source route-done:kit-pane-slots --title "kit-pane-slots done" --quote "route-done: kit-pane-slots parked (goal met). desk panes pinned to slots 0 and 1" 2>&1)
has "#24 a title that is not a question does not exempt a plumbing row" "$out" "SKIP route-done:kit-pane-slots"
expect "#23 --teach keeps it" jqs "$S23" add --kind done --origin fleet --source route-done:kit-pane-slots --quote "route-done: kit-pane-slots parked (goal met). desk panes pinned to slots 0 and 1" --teach
out=$(jqs "$S23" add --kind done --origin fleet --source route-done:widget-retry-lane --quote "route-done: widget-retry-lane parked (goal met). acme/widget#9 caps PROJ-7 upload retries at 3" 2>&1)
has "#24 a work route-done add is added, topic = its subject" "$out" "OK added"
expect "#24 that row's topic is PROJ-7" bash -c 'FLEET_STATE=$2 "$1" list --json | jq -e "[.[]|select(.source.ref==\"widget-retry-lane\")][0].topic==\"PROJ-7\""' _ "$FL" "$S23"

# ---- same-topic distractors and quiz --topic
S24=$tmp/s24
for q in "PROJ-1 retries cap at 3" "PROJ-1 backoff is jittered" "PROJ-1 fails loudly after the cap" "PROJ-1 logs each retry"; do
  jqs "$S24" add --kind decision --origin fleet --source manual:a --topic PROJ-1 --quote "$q" >/dev/null; done
for q in "PROJ-2 moves the login page" "PROJ-2 drops the old theme" "PROJ-2 ships behind a flag"; do
  jqs "$S24" add --kind decision --origin fleet --source manual:b --topic PROJ-2 --quote "$q" >/dev/null; done
expect "#24 choice distractors come from the same topic only" python3 -I - "$S24/learn" <<'PY'
import json, os, sys
d = sys.argv[1]
rows = {json.loads(l)["lid"]: json.loads(l) for l in open(os.path.join(d, "ledger.jsonl")) if l.strip()}
cards = [json.loads(l) for l in open(os.path.join(d, "cards.jsonl")) if l.strip()]
ch = [c for c in cards if c["format"] == "choice" and not c.get("retired")]
assert ch and all(rows[c["lid"]]["topic"] == "PROJ-1" for c in ch), [rows[c["lid"]]["topic"] for c in ch]
for c in ch:
    assert all(o.startswith("PROJ-1") for o in c["options"]), c["options"]
PY
seen_all "$S24"
out=$(jqs "$S24" due --topic PROJ-2 2>&1)
has "#24 due --topic PROJ-2 lists only PROJ-2 cards (3 rows x recall+source)" "$out" "6 due"
out=$(printf '\ng\nq\n' | FLEET_STATE=$S24 "$FL" quiz --topic proj-2 2>&1); rc=$?
rc_is "#24 quiz --topic exits 0" $rc 0
has "#24 quiz --topic asks a PROJ-2 card" "$out" "source quote: PROJ-2"
hasnt "#24   control: no PROJ-1 card in a PROJ-2 quiz" "$out" "PROJ-1"
expect "#23 the source shows only after grading" python3 -I - "$out" <<'PY'
import sys
out = sys.argv[1]
before, _, after = out.partition("source quote:")
assert "manual:b" not in before and "[from manual:b]" in after, out
PY

# ---- migration: old prompts refresh in place, plumbing rows' cards retire
S25=$tmp/s25; mkdir -p "$S25/learn"
cat > "$S25/learn/ledger.jsonl" <<'J'
{"lid":"L1","at":"2026-01-01T00:00:00Z","kind":"decision","origin":"fleet","status":"confirmed","title":"","what":"","why":"","source":{"type":"decisions-log","ref":"Q1"},"quote":"cap PROJ-3 retries at 3, jittered","links":[],"tags":[],"topic":"work-lane","supersedes":null,"by":"sync","confirmedBy":null}
{"lid":"L2","at":"2026-01-01T00:00:00Z","kind":"decision","origin":"fleet","status":"confirmed","title":"","what":"","why":"","source":{"type":"decisions-log","ref":"Q2"},"quote":"9a","links":[],"tags":[],"topic":"work-lane","supersedes":null,"by":"sync","confirmedBy":null}
{"lid":"L3","at":"2026-01-01T00:00:00Z","kind":"lesson","origin":"fleet","status":"confirmed","title":"","what":"","why":"","source":{"type":"route-done","ref":"kit-pane-slots"},"quote":"route-done: kit-pane-slots parked (goal met)","links":[],"tags":[],"topic":"kit-pane-slots","supersedes":null,"by":"manual","confirmedBy":null}
J
cat > "$S25/learn/cards.jsonl" <<'J'
{"cid":"C1","lid":"L1","format":"recall","origin":"fleet","prompt":"What did Q1 say about: decisions-log Q1?","answer":"cap PROJ-3 retries at 3, jittered","grounding":{"quote":"cap PROJ-3 retries at 3, jittered","source":"Q1"},"sched":{"due":"2026-01-05T00:00:00Z","fixedStep":2,"reps":2,"lapses":0,"lastReview":"2026-01-02T00:00:00Z"}}
{"cid":"C2","lid":"L2","format":"recall","origin":"fleet","prompt":"What did Q2 say about: decisions-log Q2?","answer":"9a","grounding":{"quote":"9a","source":"Q2"},"sched":{"due":"2026-01-02T00:00:00Z","fixedStep":0,"reps":0,"lapses":0,"lastReview":null}}
{"cid":"C3","lid":"L3","format":"recall","origin":"fleet","prompt":"What did kit-pane-slots say about: route-done kit-pane-slots?","answer":"x","grounding":{"quote":"x","source":"kit-pane-slots"},"sched":{"due":"2026-01-02T00:00:00Z","fixedStep":0,"reps":0,"lapses":0,"lastReview":null}}
J
jqs "$S25" sync >/dev/null 2>&1
expect "#23 migration: an old 'What did X say about' prompt is rewritten, cid and schedule kept" bash -c 'jq -se "map(select(.cid==\"C1\"))[0] | .prompt==\"What was decided about PROJ-3?\" and .sched.fixedStep==2 and (.retired|not)" "$1"' _ "$S25/learn/cards.jsonl"
expect "#24 migration: cards of an answerless row and a plumbing row are retired" bash -c 'jq -se "map(select(.cid==\"C2\" or .cid==\"C3\"))|all(.retired==true)" "$1"' _ "$S25/learn/cards.jsonl"
expect "#23 migration: no live card keeps a 'say about' prompt" bash -c '! jq -se "map(select((.retired|not) and (.prompt|test(\"say about\"))))|length>0" "$1" | grep -q true' _ "$S25/learn/cards.jsonl"

# ---- the page hides the source until graded
expect "#23 serve: a due card on the page carries no source before grading" python3 -I - "$ROOT/lib" "$S24" <<'PY'
import os, sys
os.environ["FLEET_STATE"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import fleet_learn as fl, fleet_learn_serve as sv
c = fl.due()[0]
v = sv._public(c)
assert "source" not in v and "grounding" not in v, v
PY

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_learn.sh: $fails failing assertion(s)"; exit 1; fi
echo "PASS test_learn.sh: all assertions"
