#!/usr/bin/env bash
# Tests for bin/fleet-lifecycle. Private tmux server, temp state/claude/codex/kiro homes, fake fleet binary.
# Never touches the default tmux server, ~/.claude, ~/.codex or ~/.kiro. Exit 1 on any FAIL.
set -u
HERE=$(cd "$(dirname "$0")" && pwd -P)
LC=$HERE/../bin/fleet-lifecycle
FIX=$HERE/fixtures/lifecycle
T=$(mktemp -d "${TMPDIR:-/tmp}/lctest.XXXXXX")
export FLEET_TMUX_SOCKET=lctest-$$
export FLEET_TMUX_SESSION=lcs
export FLEET_STATE=$T/state
export FLEET_CLAUDE_HOME=$T/claude
export CODEX_HOME=$T/codex
export KIRO_HOME=$T/kiro
unset FLEET_ROLE FLEET_NAME TMUX FLEET_BIN FLEET_QUIET_SECS FLEET_PARK_WAIT FLEET_TAB_WARN
# never read the real Chrome: a closed port by default, a stub server in the chrome tabs section
export FLEET_CDP_URL=http://127.0.0.1:1
export CDP_ARTIFACT_DIR=$T/cdp
W=$T/work
mkdir -p "$FLEET_STATE" "$FLEET_CLAUDE_HOME/projects/slug" "$CODEX_HOME/sessions/2026/11/18" "$KIRO_HOME/sessions" "$W"
SRVPID=
cleanup() {
  [ -z "$SRVPID" ] || kill "$SRVPID" 2>/dev/null
  tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1
  rm -rf "$T"
}
trap cleanup EXIT
tm() { tmux -L "$FLEET_TMUX_SOCKET" "$@"; }
tmux -L "$FLEET_TMUX_SOCKET" -f /dev/null new-session -d -s "$FLEET_TMUX_SESSION" -n home -c "$T" || { echo "FAIL could not start private tmux"; exit 1; }

FAILS=0
t() { # label cmd...
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi
}
eq() { # label expected actual
  if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1 (expected '$2', got '$3')"; FAILS=$((FAILS+1)); fi
}
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnt() { ! printf '%s' "$1" | grep -qF -- "$2"; }

# control: the harness really reports a failing check
ctl=$(FAILS=0; t "inner" false; echo "inner_fails=$FAILS")
t "control: a failing check prints FAIL and counts it" bash -c 'printf "%s" "$1" | grep -q "^FAIL inner" && printf "%s" "$1" | grep -q "inner_fails=1"' _ "$ctl"

# ---- helpers
uid() { printf '00000000-0000-4000-8000-%012d' "$1"; }
ago() { python3 -c 'import sys,time;print(int(time.time())-int(sys.argv[1]))' "$1"; }
iso_ago() { python3 -c 'import sys,time;print(time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(time.time()-int(sys.argv[1]))))' "$1"; }
reset_state() {
  rm -rf "$FLEET_STATE" ; mkdir -p "$FLEET_STATE/inbox"; printf '{}\n' > "$FLEET_STATE/roster.json"
  local w
  for w in $(tm list-windows -t "$FLEET_TMUX_SESSION" -F '#{window_id} #{window_name}' | grep -v ' home$' | cut -d' ' -f1); do tm kill-window -t "$w"; done
}
mkwin() { tm new-window -d -P -F '#{pane_id}' -t "$FLEET_TMUX_SESSION:" -n "$1" "${2:-sleep 300}"; }
addlane() { # name cli role sid pane [extra-json]
  local extra=${6:-'{}'} t2
  t2=$(mktemp "$T/r.XXXXXX")
  jq --arg n "$1" --arg c "$2" --arg r "$3" --arg s "$4" --arg p "$5" --arg d "$W" --argjson x "$extra" \
    '.[$n] = ({name:$n,cli:$c,role:$r,mode:"goal",pane:$p,session_id:(if $s=="" then null else $s end),port:null,dir:$d,model:"opus",pid:null,at:"2026-01-01T00:00:00Z"} + $x)' \
    "$FLEET_STATE/roster.json" > "$t2" && mv "$t2" "$FLEET_STATE/roster.json"
}
cfix() { mkdir -p "$FLEET_CLAUDE_HOME/projects/slug"; cp "$FIX/$1.jsonl" "$FLEET_CLAUDE_HOME/projects/slug/$2.jsonl"; }
kfix() { # fixture sid [no-session-json]: a kiro session dir under KIRO_HOME (cwd-hash dir is arbitrary)
  local d=$KIRO_HOME/sessions/h$(printf '%s' "$2" | cksum | cut -d' ' -f1)/$2
  mkdir -p "$d"; cp "$FIX/$1.jsonl" "$d/messages.jsonl"
  [ -n "${3:-}" ] || sed "s/sess_KSID/$2/" "$FIX/kiro-session.json" > "$d/session.json"
  printf '%s' "$d"
}
epoch() { python3 -c 'import sys,calendar,time;print(calendar.timegm(time.strptime(sys.argv[1],"%Y-%m-%dT%H:%M:%SZ")))' "$1"; }
ticks() { "$LC" tick --json; }
field() { # name field
  ticks | jq -r --arg n "$1" --arg f "$2" '.[]|select(.name==$n)|.[$f]'
}
inbox_line() { # name secs-ago
  mkdir -p "$FLEET_STATE/inbox"
  printf '{"id":"m1","at":"%s","from":"X","to":"%s","text":"hi"}\n' "$(iso_ago "$2")" "$1" >> "$FLEET_STATE/inbox/$1.jsonl"
}

# ---- opencode fake server
cat > "$T/oc_server.py" <<'PY'
import json, sys, http.server
sess = [
 {"id": "ses_oa", "directory": "/work", "title": "a", "time": {"created": 1, "updated": 1000}, "metadata": {"goal": {"state": "complete"}}},
 {"id": "ses_ob", "directory": "/work", "title": "b", "time": {"created": 2, "updated": 1000}, "metadata": {"goal": {"state": "active"}}},
 {"id": "ses_oc", "directory": "/work", "title": "c", "time": {"created": 3, "updated": 1000}, "metadata": {"goal": {"state": "paused"}}},
 {"id": "ses_od", "directory": "/work", "title": "d", "time": {"created": 4, "updated": 1000}, "metadata": {}},
]
msgs = [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "Add dark mode"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Dark mode added."},
                                                  {"type": "tool", "state": {"input": {"filePath": "/work/ui.css"}}}]}]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/session":
            body = sess
        elif self.path == "/session/ses_ocomp/message":
            import os
            done = os.path.exists(sys.argv[1] + ".compacted")
            body = msgs + ([{"info": {"role": "assistant", "summary": True, "mode": "compaction", "time": {"completed": 5}}, "parts": []}] if done else [])
        elif self.path.startswith("/session/") and self.path.endswith("/message"):
            body = msgs
        else:
            self.send_response(404); self.end_headers(); return
        b = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
open(sys.argv[1], "w").write(str(s.server_address[1]))
s.serve_forever()
PY
python3 "$T/oc_server.py" "$T/oc_port" >/dev/null 2>&1 &
SRVPID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$T/oc_port" ] && break; sleep 1; done
OCPORT=$(cat "$T/oc_port" 2>/dev/null || echo 0)
t "opencode fake server is up" test "$OCPORT" != 0

# ---- codex goals db
python3 - "$CODEX_HOME/goals_1.sqlite" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("create table thread_goals(thread_id text, goal_id text, objective text, status text)")
for tid, st in (("cx-a", "complete"), ("cx-b", "active"), ("cx-c", "paused"), ("cx-e", "blocked"), ("cx-f", "usage_limited")):
    c.execute("insert into thread_goals values(?,?,?,?)", (tid, "g-" + tid, "obj", st))
c.commit()
PY

# ================================================================ 1. goal-state derivation per CLI
reset_state
cfix claude-achieved "$(uid 1)"; cfix claude-active "$(uid 2)"; cfix claude-stopped "$(uid 3)"; cfix claude-none "$(uid 4)"
addlane cA claude worker "$(uid 1)" %901; addlane cB claude worker "$(uid 2)" %901
addlane cC claude worker "$(uid 3)" %901; addlane cD claude worker "$(uid 4)" %901
addlane xA codex worker cx-a %901; addlane xB codex worker cx-b %901; addlane xC codex worker cx-c %901
addlane xD codex worker cx-none %901; addlane xE codex worker cx-e %901; addlane xF codex worker cx-f %901
for s in a b c d; do addlane "o$s" opencode worker "ses_o$s" %901 "{\"port\":$OCPORT}"; done
eq "claude achieved"  achieved "$(field cA goalState)"
eq "claude active"    active   "$(field cB goalState)"
eq "claude stopped (Goal paused)" stopped "$(field cC goalState)"
eq "claude none"      none     "$(field cD goalState)"
eq "codex complete -> achieved" achieved "$(field xA goalState)"
eq "codex active"     active   "$(field xB goalState)"
eq "codex paused -> stopped" stopped "$(field xC goalState)"
eq "codex no row -> none" none "$(field xD goalState)"
eq "codex blocked -> stopped" stopped "$(field xE goalState)"
eq "codex usage_limited -> stopped" stopped "$(field xF goalState)"
eq "opencode complete -> achieved" achieved "$(field oa goalState)"
eq "opencode active"  active   "$(field ob goalState)"
eq "opencode paused -> stopped" stopped "$(field oc goalState)"
eq "opencode no goal -> none" none "$(field od goalState)"
kfix kiro-goal-achieved sess_ka >/dev/null; kfix kiro-goal-active sess_kb >/dev/null; kfix kiro-goal-failed sess_kc >/dev/null
kfix kiro-none sess_kd nosession >/dev/null
touch -t 202601020000 "$KIRO_HOME"/sessions/*/sess_kd/messages.jsonl
addlane kA kiro worker sess_ka %901; addlane kB kiro worker sess_kb %901; addlane kC kiro worker sess_kc %901
addlane kD kiro worker sess_kd %901; addlane kE kiro worker sess_knodir %901
eq "kiro run_start + run_complete(completed) -> achieved" achieved "$(field kA goalState)"
eq "kiro run_start only -> active" active "$(field kB goalState)"
eq "kiro run_complete(failed) -> stopped" stopped "$(field kC goalState)"
eq "kiro no goal markers -> none" none "$(field kD goalState)"
eq "kiro session dir missing -> none (roster fallback)" none "$(field kE goalState)"
eq "kiro stopped-since is the run_complete timestamp" "$(epoch 2026-01-01T10:04:30Z)" "$(field kC goalStoppedAt)"
eq "kiro last activity = session.json lastModifiedAt" "$(epoch 2026-01-01T10:05:00Z)" "$(field kA lastActive)"
t "kiro mtime fallback differs from a session.json lastModifiedAt (control)" test "$(field kD lastActive)" != "$(field kA lastActive)"
eq "kiro mtime fallback is what tick reports" "$(field kD lastActive)" "$(python3 -c 'import os,glob,sys;print(int(os.path.getmtime(glob.glob(sys.argv[1]+"/sessions/*/sess_kd/messages.jsonl")[0])))' "$KIRO_HOME")"
eq "claude stopped-since comes from the transcript record timestamp" 1767262200 "$(field cC goalStoppedAt)"
tick_txt=$("$LC" tick)
t "tick table has the header" has "$tick_txt" "NAME"
t "tick table header names every column" bash -c 'for c in NAME CLI ROLE TIER LAST-ACTIVE GOAL UNREAD ACTION; do printf "%s" "$1" | head -1 | grep -q "$c" || exit 1; done' _ "$tick_txt"

# ================================================================ 2. tick ACTION
reset_state
cfix claude-achieved "$(uid 11)"; cfix claude-achieved "$(uid 12)"; cfix claude-achieved "$(uid 13)"; cfix claude-achieved "$(uid 14)"
cfix claude-achieved "$(uid 15)"; cfix claude-achieved "$(uid 16)"; cfix claude-achieved "$(uid 17)"; cfix claude-none "$(uid 18)"
cfix claude-stopped "$(uid 19)"; cfix claude-stopped "$(uid 20)"; cfix claude-stopped "$(uid 21)"; cfix claude-active "$(uid 22)"
addlane wPark claude worker "$(uid 11)" %901
addlane orch claude orchestrator "$(uid 12)" %901
addlane desk claude desk "$(uid 13)" %901
addlane lstn claude listener "$(uid 14)" %901
addlane wNew claude worker "$(uid 15)" %901;   inbox_line wNew 600
addlane wOld claude worker "$(uid 16)" %901;   inbox_line wOld 7200
addlane wRead claude worker "$(uid 17)" %901;  inbox_line wRead 600; printf '1\n' > "$FLEET_STATE/inbox/wRead.cursor"
addlane wNone claude worker "$(uid 18)" %901
addlane wStopTs claude worker "$(uid 19)" %901
addlane wStopNew claude worker "$(uid 20)" %901 "{\"goalStoppedAt\":$(ago 100)}"
addlane wStopOld claude worker "$(uid 21)" %901 "{\"goalStoppedAt\":$(ago 1000)}"
addlane wActive claude worker "$(uid 22)" %901
kfix kiro-goal-achieved sess_t1 >/dev/null; kfix kiro-goal-failed sess_t2 >/dev/null; kfix kiro-goal-active sess_t3 >/dev/null
addlane kPark kiro worker sess_t1 %901; addlane kFail kiro worker sess_t2 %901; addlane kAct kiro worker sess_t3 %901
addlane kOrch kiro orchestrator sess_t1 %901
eq "tick: worker with achieved goal -> park" park "$(field wPark action)"
eq "tick: orchestrator with achieved goal -> not park" "-" "$(field orch action)"
eq "tick: desk with achieved goal -> not park" "-" "$(field desk action)"
eq "tick: listener with achieved goal -> not park" "-" "$(field lstn action)"
eq "tick: unread line 10 min old blocks park" "-" "$(field wNew action)"
eq "tick: control, only a 2 h old unread -> park" park "$(field wOld action)"
eq "tick: young line already read (cursor) -> park" park "$(field wRead action)"
eq "tick: unread counts total/young" "1 1" "$(field wNew unread) $(field wNew unreadYoung)"
eq "tick: goal-less worker -> orchestrator-decides" orchestrator-decides "$(field wNone action)"
eq "tick: stopped 100 s (goalStoppedAt override) -> no park" "-" "$(field wStopNew action)"
eq "tick: stopped 1000 s (goalStoppedAt override) -> park" park "$(field wStopOld action)"
eq "tick: stopped, no override, old transcript timestamp -> park" park "$(field wStopTs action)"
eq "tick: active goal -> no action" "-" "$(field wActive action)"
eq "tick: kiro worker with achieved goal -> park" park "$(field kPark action)"
eq "tick: kiro worker with a failed goal (stopped long ago) -> park" park "$(field kFail action)"
eq "tick: kiro worker with an active goal -> no action" "-" "$(field kAct action)"
eq "tick: kiro orchestrator with achieved goal -> not park" "-" "$(field kOrch action)"
t "tick text row shows park for wPark" bash -c '"$1" tick | grep "^wPark" | grep -q "park$"' _ "$LC"
before=$(cksum "$FLEET_STATE/roster.json"); ticks >/dev/null; "$LC" tick >/dev/null
eq "tick is read-only (roster unchanged)" "$before" "$(cksum "$FLEET_STATE/roster.json")"

# ---------------------------------------------------------------- tick: chrome tabs line (read-only, fail open)
tabs_txt=$("$LC" tick); rc=$?
eq "tick with Chrome down exits 0" 0 "$rc"
t "tick with Chrome down says unavailable" has "$tabs_txt" "chrome tabs: unavailable"
t "tick with Chrome down still prints the table" has "$tabs_txt" "NAME"
mkdir -p "$T/cdpsrv/json" "$CDP_ARTIFACT_DIR"
python3 -c 'import json,sys
pages=[{"id":"P%d"%i,"type":"page","url":"about:blank"} for i in range(3)]
pages+=[{"id":"B1","type":"browser_ui"},{"id":"W1","type":"service_worker"}]
json.dump(pages,open(sys.argv[1],"w"))' "$T/cdpsrv/json/list"
: > "$CDP_ARTIFACT_DIR/origin-chrome-P0.json"
: > "$CDP_ARTIFACT_DIR/origin-chrome-GONE.json"
CDPPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
python3 -m http.server "$CDPPORT" --bind 127.0.0.1 --directory "$T/cdpsrv" >/dev/null 2>&1 &
CDPPID=$!
for _ in $(seq 50); do curl -s -m 1 "http://127.0.0.1:$CDPPORT/json/list" >/dev/null 2>&1 && break; sleep 0.1; done
tabs_txt=$(FLEET_CDP_URL=http://127.0.0.1:$CDPPORT "$LC" tick)
t "tick counts page targets only, agent by origin record" has "$tabs_txt" "chrome tabs: 3 (agent 1, other 2)"
t "tick: no WARN at or under the default 50" hasnt "$tabs_txt" "WARN"
tabs_txt=$(FLEET_CDP_URL=http://127.0.0.1:$CDPPORT FLEET_TAB_WARN=2 "$LC" tick)
t "tick: WARN when tabs > FLEET_TAB_WARN" has "$tabs_txt" "WARN chrome tabs 3 > 2"
tabs_txt=$(FLEET_CDP_URL=http://127.0.0.1:$CDPPORT FLEET_TAB_WARN=3 "$LC" tick)
t "tick: no WARN when tabs == FLEET_TAB_WARN" hasnt "$tabs_txt" "WARN"
tabs_txt=$(FLEET_CDP_URL=http://127.0.0.1:$CDPPORT CDP_ARTIFACT_DIR=$T/nosuch "$LC" tick)
t "tick: no origin dir -> just the count" bash -c 'printf "%s\n" "$1" | grep -qx "chrome tabs: 3"' _ "$tabs_txt"
j=$(FLEET_CDP_URL=http://127.0.0.1:$CDPPORT "$LC" tick --json)
t "tick --json stays a pure lane list" bash -c 'printf "%s" "$1" | jq -e "type==\"array\"" >/dev/null' _ "$j"
kill "$CDPPID" 2>/dev/null; wait "$CDPPID" 2>/dev/null

# ================================================================ 3. park
reset_state
cfix claude-achieved "$(uid 31)"
CF=$FLEET_CLAUDE_HOME/projects/slug/$(uid 31).jsonl
pane=$(mkwin pk "python3 $HERE/stubs/compactor claude $CF")
addlane pk claude worker "$(uid 31)" "$pane"
t "park setup: stub window is alive" bash -c 'tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$pane"
out=$("$LC" park pk 2>&1); rc=$?
eq "park exits 0" 0 "$rc"
t "park killed the stub window" bash -c '! tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$pane"
eq "park sets status parked" parked "$(jq -r .pk.status "$FLEET_STATE/roster.json")"
t "park records parkedAt" test -n "$(jq -r '.pk.parkedAt // empty' "$FLEET_STATE/roster.json")"
t "park records a resume command with the session id" has "$(jq -r .pk.resume "$FLEET_STATE/roster.json")" "--resume $(uid 31)"
eq "park records goalState" achieved "$(jq -r .pk.goalState "$FLEET_STATE/roster.json")"
t "park prints OK" has "$out" "OK parked pk"
t "park typed /compact into the lane" grep -qx /compact "$CF.typed"
t "park waited for the compact record (transcript has compact_boundary)" grep -q compact_boundary "$CF"
t "park says it compacted" has "$out" "OK compacted pk"
t "park records compactedAt" test -n "$(jq -r '.pk.compactedAt // empty' "$FLEET_STATE/roster.json")"
t "compact finished before the park was recorded (boundary ts <= compactedAt <= parkedAt)" python3 -c '
import json,sys
b=[json.loads(l) for l in open(sys.argv[1]) if "compact_boundary" in l][-1]["timestamp"]
e=json.load(open(sys.argv[2]))["pk"]
assert b <= e["compactedAt"] <= e["parkedAt"], (b, e)' "$CF" "$FLEET_STATE/roster.json"
# a lane that never compacts: park refuses, the pane stays, status unchanged
cfix claude-achieved "$(uid 32)"
CF2=$FLEET_CLAUDE_HOME/projects/slug/$(uid 32).jsonl
pane=$(mkwin nc "python3 $HERE/stubs/compactor never $CF2")
addlane nc claude worker "$(uid 32)" "$pane"
out=$(FLEET_COMPACT_WAIT=3 "$LC" park nc 2>&1); rc=$?
t "park refuses when the compact is never confirmed" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "compact of nc not confirmed"' _ "$rc" "$out"
t "refused park left the pane alive" bash -c 'tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$pane"
eq "refused park left the status unparked" live "$(jq -r '.nc.status // "live"' "$FLEET_STATE/roster.json")"
out=$("$LC" park nc --no-compact 2>&1); rc=$?
eq "park --no-compact parks it" parked "$(jq -r .nc.status "$FLEET_STATE/roster.json")"
t "park --no-compact records no compactedAt" test -z "$(jq -r '.nc.compactedAt // empty' "$FLEET_STATE/roster.json")"
eq "park --no-compact typed /compact once only (from the refused run)" 1 "$(grep -cx /compact "$CF2.typed")"
# transcript missing: refuse rather than park blind
pane=$(mkwin nt "python3 $HERE/stubs/compactor never $T/nt")
addlane nt claude worker "$(uid 33)" "$pane"
out=$("$LC" park nt 2>&1); rc=$?
t "park refuses when the transcript can't be found" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "transcript of nt"' _ "$rc" "$out"
t "park with no transcript typed nothing" test ! -e "$T/nt.typed"
# codex: compacted record in the rollout
RO=$CODEX_HOME/sessions/2026/11/18/rollout-2026-11-18T09-00-00-cx-pk.jsonl
sed 's/CODEX_SID/cx-pk/' "$FIX/codex-rollout.jsonl" > "$RO"
pane=$(mkwin cpk "python3 $HERE/stubs/compactor codex $RO")
addlane cpk codex worker cx-pk "$pane"
out=$("$LC" park cpk 2>&1); rc=$?
eq "codex park exits 0 after the compacted record" 0 "$rc"
t "codex rollout gained a compacted record" grep -q '"type": "compacted"' "$RO"
eq "codex lane parked" parked "$(jq -r .cpk.status "$FLEET_STATE/roster.json")"
# opencode: completed summary message from the server
pane=$(mkwin opk "python3 $HERE/stubs/compactor touch $T/oc_port.compacted")
addlane opk opencode worker ses_ocomp "$pane" "{\"port\":$OCPORT}"
out=$("$LC" park opk 2>&1); rc=$?
eq "opencode park exits 0 after the summary message completes" 0 "$rc"
eq "opencode lane parked" parked "$(jq -r .opk.status "$FLEET_STATE/roster.json")"
t "opencode park says it compacted" has "$out" "OK compacted opk"
# kiro: tombstone(kind=summarization) in messages.jsonl
KD=$(kfix kiro-goal-achieved sess_kpk); KF=$KD/messages.jsonl
before_n=$(grep -c tombstone "$KF")
pane=$(mkwin kpk "python3 $HERE/stubs/compactor kiro $KF")
addlane kpk kiro worker sess_kpk "$pane"
out=$("$LC" park kpk 2>&1); rc=$?
eq "kiro park exits 0 after the tombstone" 0 "$rc"
t "kiro park typed /compact into the lane" grep -qx /compact "$KF.typed"
eq "kiro transcript gained exactly one summarization tombstone" "$((before_n+1))" "$(grep -c '"kind": "summarization"' "$KF")"
t "kiro park says it compacted" has "$out" "OK compacted kpk"
eq "kiro lane parked" parked "$(jq -r .kpk.status "$FLEET_STATE/roster.json")"
t "kiro park records compactedAt" test -n "$(jq -r '.kpk.compactedAt // empty' "$FLEET_STATE/roster.json")"
t "kiro park records parkedAt" test -n "$(jq -r '.kpk.parkedAt // empty' "$FLEET_STATE/roster.json")"
eq "kiro park records the resume command with --resume-id" "kiro-cli chat --agent-engine v3 --trust-all-tools --trust-workspace --resume-id sess_kpk" "$(jq -r .kpk.resume "$FLEET_STATE/roster.json")"
eq "kiro park records goalState" achieved "$(jq -r .kpk.goalState "$FLEET_STATE/roster.json")"
t "kiro park killed the stub window" bash -c '! tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$pane"
# CONTROL: a kiro lane that never compacts must refuse to park
KD2=$(kfix kiro-goal-achieved sess_knc); KF2=$KD2/messages.jsonl
pane=$(mkwin knc "python3 $HERE/stubs/compactor never $KF2")
addlane knc kiro worker sess_knc "$pane"
out=$(FLEET_COMPACT_WAIT=3 "$LC" park knc 2>&1); rc=$?
t "kiro control: park refuses when no tombstone appears" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "compact of knc not confirmed"' _ "$rc" "$out"
t "kiro control: refused park left the pane alive" bash -c 'tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$pane"
eq "kiro control: refused park left the status unparked" live "$(jq -r '.knc.status // "live"' "$FLEET_STATE/roster.json")"
# a non-summarization tombstone does not count as a compact
printf '%s\n' '{"id":"x","timestamp":"2026-01-01T11:00:00Z","payload":{"type":"tombstone","kind":"other"}}' >> "$KF2"
out=$(FLEET_COMPACT_WAIT=3 "$LC" park knc 2>&1); rc=$?
t "kiro control: a tombstone of another kind is not a finished compact" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "compact of knc not confirmed"' _ "$rc" "$out"
# kiro transcript missing: refuse rather than park blind
pane=$(mkwin knt "python3 $HERE/stubs/compactor never $T/knt")
addlane knt kiro worker sess_nodir "$pane"
out=$("$LC" park knt 2>&1); rc=$?
t "kiro park refuses when the transcript can't be found" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "transcript of knt"' _ "$rc" "$out"
# stubborn process: ignores C-c, so park must fall through to the kill path
pane=$(mkwin stub "bash -c 'trap \"\" INT; sleep 300'")
addlane stub claude worker "$(uid 31)" "$pane"
FLEET_PARK_WAIT=2 "$LC" park stub --no-compact >/dev/null 2>&1
t "park falls back to kill-pane for a process that ignores C-c" bash -c '! tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$pane"
eq "stubborn lane is parked too" parked "$(jq -r .stub.status "$FLEET_STATE/roster.json")"
out=$(FLEET_ROLE=worker "$LC" park pk 2>&1); rc=$?
t "role guard: a worker cannot park" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "role worker can.t park"' _ "$rc" "$out"

# ================================================================ 4. handoff
reset_state
cfix claude-achieved "$(uid 41)"
mkdir -p "$CODEX_HOME/sessions/2026/11/18"
sed 's/CODEX_SID/cx-h/' "$FIX/codex-rollout.jsonl" > "$CODEX_HOME/sessions/2026/11/18/rollout-2026-11-18T10-00-00-cx-h.jsonl"
addlane h1 claude worker "$(uid 41)" %901 "{\"status\":\"parked\"}"
addlane h2 codex worker cx-h %901 "{\"status\":\"parked\"}"
addlane h3 opencode worker ses_oa %901 "{\"status\":\"parked\",\"port\":$OCPORT}"
"$LC" handoff h1 >/dev/null 2>&1; HF=$FLEET_STATE/handoff/h1.md
t "handoff (claude) wrote handoff/NAME.md" test -s "$HF"
eq "handoff (claude) first line is 'Was: h1'" "Was: h1" "$(head -1 "$HF")"
t "handoff (claude) carries the last goal" grep -q 'make the retry loop stop after 3 attempts' "$HF"
t "handoff (claude) carries assistant text" grep -q 'Tests pass, retry cap verified.' "$HF"
t "handoff (claude) lists files touched" grep -q '/work/src/retry.py' "$HF"
"$LC" handoff h2 >/dev/null 2>&1
eq "handoff (codex) first line" "Was: h2" "$(head -1 "$FLEET_STATE/handoff/h2.md")"
t "handoff (codex) goal, assistant text and file" bash -c 'f=$1; grep -q "Port the parser" "$f" && grep -q "Parser ported" "$f" && grep -q "src/parser.py" "$f"' _ "$FLEET_STATE/handoff/h2.md"
"$LC" handoff h3 >/dev/null 2>&1
eq "handoff (opencode) first line" "Was: h3" "$(head -1 "$FLEET_STATE/handoff/h3.md")"
t "handoff (opencode) goal, assistant text and file" bash -c 'f=$1; grep -q "Add dark mode" "$f" && grep -q "Dark mode added" "$f" && grep -q "/work/ui.css" "$f"' _ "$FLEET_STATE/handoff/h3.md"
addlane h4 kiro worker sess_kh %901 '{"status":"parked"}'
kfix kiro-goal-achieved sess_kh >/dev/null
"$LC" handoff h4 >/dev/null 2>&1
eq "handoff (kiro) first line" "Was: h4" "$(head -1 "$FLEET_STATE/handoff/h4.md")"
t "handoff (kiro) goal and assistant text" bash -c 'f=$1; grep -q "rename the widget module to gadget" "$f" && grep -q "Renamed widget to gadget" "$f"' _ "$FLEET_STATE/handoff/h4.md"
t "handoff (kiro) leaves workflow markers out" bash -c '! grep -q "_kiro/workflow" "$1"' _ "$FLEET_STATE/handoff/h4.md"
addlane h5 kiro worker sess_kh5 %901 '{"status":"parked"}'
kfix kiro-none sess_kh5 >/dev/null
"$LC" handoff h5 >/dev/null 2>&1
t "handoff (kiro, list content blocks) carries the assistant text" grep -q "Hello from the fixture." "$FLEET_STATE/handoff/h5.md"
addlane h6 kiro worker sess_missing %901 '{"status":"parked"}'
out=$("$LC" handoff h6 2>&1); rc=$?
t "handoff (kiro) with no session dir fails loud" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "^FAIL"' _ "$rc" "$out"
out=$("$LC" handoff nosuch 2>&1); rc=$?
t "handoff of an unknown lane fails loud" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "^FAIL"' _ "$rc" "$out"

# ================================================================ 5. reopen
reset_state
cat > "$T/fakefleet" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/fake.log"
exit 0
EOF
chmod +x "$T/fakefleet"
export FLEET_BIN=$T/fakefleet
cfix claude-achieved "$(uid 51)"
addlane rc claude worker "$(uid 51)" %901 '{"status":"parked"}'
addlane rx codex orchestrator cx-r %901 '{"status":"parked"}'
addlane ro opencode worker ses_oa %901 '{"status":"done","model":null}'
addlane rl claude worker "$(uid 51)" %901 '{"status":"live"}'
"$LC" reopen rc >/dev/null 2>&1
eq "reopen claude runs fleet launch ... resume <id>" "launch --cli claude --model opus --role worker --dir $W --keep-tools all rc resume $(uid 51)" "$(tail -1 "$T/fake.log")"
"$LC" reopen rx >/dev/null 2>&1
eq "reopen codex keeps the role and resumes the thread" "launch --cli codex --model opus --role orchestrator --dir $W --keep-tools all rx resume cx-r" "$(tail -1 "$T/fake.log")"
addlane rk kiro worker sess_rk %901 '{"status":"parked"}'
"$LC" reopen rk >/dev/null 2>&1
eq "reopen kiro resumes the session id through fleet launch" "launch --cli kiro --model opus --role worker --dir $W --keep-tools all rk resume sess_rk" "$(tail -1 "$T/fake.log")"
eq "reopen kiro flips the roster status to live" live "$(jq -r .rk.status "$FLEET_STATE/roster.json")"
"$LC" reopen ro >/dev/null 2>&1
eq "reopen opencode (status done, model null: no --model) resumes the ses_ id" "launch --cli opencode --role worker --dir $W --keep-tools all ro resume ses_oa" "$(tail -1 "$T/fake.log")"
eq "reopen flips the roster status to live" live "$(jq -r .rc.status "$FLEET_STATE/roster.json")"
n=$(wc -l < "$T/fake.log" | tr -d ' ')
out=$("$LC" reopen rl 2>&1); rc=$?
t "reopen refuses a live lane and launches nothing" bash -c '[ "$1" != 0 ] && [ "$2" = "$3" ]' _ "$rc" "$n" "$(wc -l < "$T/fake.log" | tr -d ' ')"
addlane rw claude worker "$(uid 51)" %901 '{"status":"parked","keepTools":"Workflow","blockedTools":"ScheduleWakeup,ReportFindings"}'
"$LC" reopen rw >/dev/null 2>&1
eq "reopen keeps a lane's recorded keepTools" "launch --cli claude --model opus --role worker --dir $W --keep-tools Workflow rw resume $(uid 51)" "$(tail -1 "$T/fake.log")"
addlane rb claude worker "$(uid 51)" %901 '{"status":"parked","keepTools":"","blockedTools":"Workflow,ScheduleWakeup,ReportFindings"}'
"$LC" reopen rb >/dev/null 2>&1
eq "reopen of a lane launched with the full worker block passes no --keep-tools" "launch --cli claude --model opus --role worker --dir $W rb resume $(uid 51)" "$(tail -1 "$T/fake.log")"
roster_set() { jq --arg n "$1" '.[$n].status="parked"' "$FLEET_STATE/roster.json" > "$T/r.tmp" && mv "$T/r.tmp" "$FLEET_STATE/roster.json"; }
roster_set rc
rm -f "$FLEET_STATE/handoff/rc.md"
"$LC" reopen rc --fresh >/dev/null 2>&1
eq "reopen --fresh launches with prompt <handoff file>" "launch --cli claude --model opus --role worker --dir $W --keep-tools all rc prompt $FLEET_STATE/handoff/rc.md" "$(tail -1 "$T/fake.log")"
eq "reopen --fresh generated the missing handoff (Was: rc)" "Was: rc" "$(head -1 "$FLEET_STATE/handoff/rc.md" 2>/dev/null)"
roster_set rc
out=$(FLEET_ROLE=desk "$LC" reopen rc 2>&1); rc=$?
t "role guard: a desk cannot reopen" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "role desk can.t reopen"' _ "$rc" "$out"
unset FLEET_BIN

# ================================================================ 6. cleanup
reset_state
cfix claude-achieved "$(uid 61)"; cfix claude-none "$(uid 62)"
p1=$(mkwin live1); addlane live1 claude worker "$(uid 61)" "$p1"
p2=$(mkwin quiet1 "python3 $HERE/stubs/compactor claude $FLEET_CLAUDE_HOME/projects/slug/$(uid 62).jsonl"); addlane quiet1 claude worker "$(uid 62)" "$p2"
p3=$(mkwin deskl); addlane deskl claude desk "$(uid 62)" "$p3"
addlane ghost claude worker "$(uid 61)" %9999
mkwin stray >/dev/null
mkdir -p "$FLEET_STATE/launch"
printf '#!/usr/bin/env bash\nsleep 300\n:\n' > "$FLEET_STATE/launch/ghost.sh"
bash "$FLEET_STATE/launch/ghost.sh" >/dev/null 2>&1 &
GPID=$!
sleep 2
snap() { (cd "$FLEET_STATE" && find . -type f | sort | while read -r f; do cksum "$f"; done); tm list-windows -t "$FLEET_TMUX_SESSION" -F '#{window_name}' | sort; }
before=$(snap)
dry=$(FLEET_QUIET_SECS=1 "$LC" cleanup 2>&1)
t "cleanup dry run lists the orphan tmux window" has "$dry" "ORPHAN-WINDOW stray"
t "cleanup dry run lists the roster entry whose pane is gone" has "$dry" "DEAD-LANE ghost pane=%9999"
t "cleanup dry run lists a goal-less worker quiet beyond the threshold" has "$dry" "QUIET quiet1"
t "cleanup dry run lists a cli process whose argv names a pane-less lane" has "$dry" "ORPHAN-PROC pid=$GPID lane=ghost"
t "cleanup dry run: control, a healthy lane is not reported" hasnt "$dry" "live1"
t "cleanup dry run: control, a quiet desk is never reported" hasnt "$dry" "deskl"
t "cleanup dry run: control, the home window is not an orphan" hasnt "$dry" "ORPHAN-WINDOW home"
eq "cleanup dry run writes nothing (state files and windows unchanged)" "$before" "$(snap)"
t "cleanup dry run created no archive" test ! -d "$FLEET_STATE/archive"
cp "$FLEET_STATE/roster.json" "$T/roster.before"
FLEET_QUIET_SECS=1 "$LC" cleanup --apply >/dev/null 2>&1
ARC=$(ls "$FLEET_STATE"/archive/roster-*.json 2>/dev/null | head -1)
t "cleanup --apply archived the roster" test -s "${ARC:-/nonexistent}"
t "cleanup archive is the pre-apply roster (dead entry still live in it)" bash -c 'cmp -s "$1" "$2" && [ "$(jq -r ".ghost.status // \"live\"" "$2")" = live ]' _ "$T/roster.before" "${ARC:-/nonexistent}"
eq "cleanup --apply marked the dead entry parked" parked "$(jq -r .ghost.status "$FLEET_STATE/roster.json")"
t "cleanup --apply recorded parkedAt for it" test -n "$(jq -r '.ghost.parkedAt // empty' "$FLEET_STATE/roster.json")"
eq "cleanup --apply parked the quiet goal-less worker" parked "$(jq -r .quiet1.status "$FLEET_STATE/roster.json")"
t "cleanup --apply closed the quiet worker's window" bash -c '! tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$p2"
eq "cleanup --apply left the healthy lane live" live "$(jq -r '.live1.status // "live"' "$FLEET_STATE/roster.json")"
t "cleanup --apply left the desk lane's window" bash -c 'tmux -L "$1" list-panes -a -F "#{pane_id}" | grep -qx "$2"' _ "$FLEET_TMUX_SOCKET" "$p3"
t "cleanup --apply without --kill-orphans keeps the orphan window" bash -c 'tmux -L "$1" list-windows -t "$2" -F "#{window_name}" | grep -qx stray' _ "$FLEET_TMUX_SOCKET" "$FLEET_TMUX_SESSION"
t "cleanup --apply without --kill-orphans keeps the orphan process" kill -0 "$GPID"
"$LC" cleanup --apply --kill-orphans >/dev/null 2>&1
t "cleanup --apply --kill-orphans kills the orphan window" bash -c '! tmux -L "$1" list-windows -t "$2" -F "#{window_name}" | grep -qx stray' _ "$FLEET_TMUX_SOCKET" "$FLEET_TMUX_SESSION"
sleep 1
t "cleanup --apply --kill-orphans kills the orphan process" bash -c '! kill -0 "$1" 2>/dev/null' _ "$GPID"
out=$(FLEET_ROLE=listener "$LC" cleanup --apply 2>&1); rc=$?
t "role guard: a listener cannot cleanup --apply" bash -c '[ "$1" != 0 ] && printf "%s" "$2" | grep -q "role listener can.t cleanup"' _ "$rc" "$out"
kill "$GPID" 2>/dev/null

# ---- exact session targeting: a session whose name only STARTS with the fleet session name is never touched
tm new-session -d -s "nosuch-$$decoy" -n prefixwin "sleep 300"
t "control: an unpinned tmux target prefix-matches the decoy session" bash -c 'tmux -L "$1" list-panes -s -t "nosuch-$2" -F "#W" 2>/dev/null | grep -qx prefixwin' _ "$FLEET_TMUX_SOCKET" "$$"
dry=$(FLEET_TMUX_SESSION=nosuch-$$ "$LC" cleanup --apply --kill-orphans 2>&1)
t "cleanup with a missing session does not report a prefix-matching session's window" bash -c '! printf "%s" "$1" | grep -q prefixwin' _ "$dry"
t "cleanup with a missing session does not kill a prefix-matching session's window" bash -c 'tmux -L "$1" list-windows -t "=nosuch-$2decoy" -F "#W" | grep -qx prefixwin' _ "$FLEET_TMUX_SOCKET" "$$"
tm kill-session -t "=nosuch-$$decoy" 2>/dev/null

echo "---"
if [ "$FAILS" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILS FAILED"; exit 1; fi
