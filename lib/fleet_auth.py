#!/usr/bin/env python3
"""fleet auth: the AUTH desk's engine (docs/design/auth-desk.md). Python 3.8+ stdlib only.

AUTH is the fifth standing desk, always open in window deskA, top left beside DECIDE. It owns every login and auth problem for
the fleet and is the only session that runs interactive sign-ins as the human. This module holds its state and verbs,
the same snapshot()/verbs split as the other desks, so a page can sit on top later.

  fleet auth ask --what W [--why Y]                     any lane: hand an auth failure to AUTH, keep working
  fleet auth begin --what W [--flow session|password] [--for LANE] [--match NN] [--ask A<n>]
                                                        AUTH only: take the one sign-in slot, post a one-line heads-up
                                                        to DECIDE, log `started`. --flow session (reuse an already
                                                        signed-in browser session, e.g. confirm a device code there)
                                                        is preferred over a fresh password sign-in (default password)
  fleet auth number NN [A<n>]                           AUTH only: the sign-in page shows a number to match. Prints it
                                                        big in AUTH's pane, logs `number-shown` (time, who asked),
                                                        tells DECIDE
  fleet auth end A<n> --result ok|failed|cancelled [--note N]
                                                        AUTH only: log the result, free the slot, tell the asking lane
  fleet auth unexpected-push [--at TIME] [--note N] [--quote Q]
                                                        anyone: a push or number prompt that no heads-up announced.
                                                        Logs `push-unexpected` with the rows near TIME and routes it to
                                                        DECIDE
  fleet auth confirm A<n> --as-expected|--unexpected [--quote Q]
                                                        the human (or DECIDE relaying their typed words in --quote):
                                                        were the prompts on their phone only the announced ones? only
                                                        --as-expected makes an ok sign-in pass
  fleet auth log [--last N] [--for LANE] [--json]       the append-only auth log, newest last
  fleet auth trace TIME [--window MIN] [--json]         rows within MIN minutes (default 10) of TIME: who asked for
                                                        the sign-in behind a prompt, in one lookup
  fleet auth freeze --reason R | fleet auth thaw [--quote Q]
                                                        stop or resume every sign-in (human, ROUTE or DECIDE)
  fleet auth status [--json]                            snapshot: the slot, freeze, open asks, verdicts, last rows
  fleet auth helpers                                    the human's own sign-in helpers, by reference only

Files, all under $FLEET_STATE/auth/:
  auth-log.jsonl   append-only, one row per event: {id, at, event, by, for, what, flow?, why?, match?, result?, note?,
                   ask?, quote?, near?}. event is requested | started | number-shown | ended | confirmed |
                   push-unexpected | slot-broken | freeze | thaw. Rows are never rewritten or deleted.
  slot.json        the sign-in in progress ({id, at, by, for, what, flow}); absent when the slot is free
  freeze.json      present while sign-ins are frozen ({at, by, reason})
  helpers.json     optional, written by the human, never by the kit: [{name, ref, use}] pointing at their own local
                   scripts and skills ($FLEET_AUTH_HELPERS overrides the path). The kit ships no helper content.

Rules the code enforces:
  - begin, number and end run only as role auth or in the human's own shell (no FLEET_ROLE); every other role is
    refused with the hand-off hint (`fleet auth ask`).
  - one sign-in at a time: begin is refused while the slot is held; a slot older than FLEET_AUTH_SLOT_SECS (default
    900) is broken with a `slot-broken` row, so a crashed sign-in can't block the fleet forever.
  - the heads-up reaches DECIDE's inbox before the sign-in is logged as started; if it can't be written, nothing
    starts. A push or number prompt during an announced sign-in is expected.
  - a push or prompt no heads-up announced is the anomaly: `unexpected-push` (or the human's `confirm --unexpected`)
    logs it with the rows around that moment and routes it to DECIDE.
  - a sign-in passes only when the human confirms the prompts were the announced ones (`confirm --as-expected`),
    never on a log flag or a script's output; `ended ok` alone shows as "ok-unconfirmed".
  - begin is refused while sign-ins are frozen (`fleet auth freeze`), until `fleet auth thaw`.
  - no secret in any field: a value that looks like a token, key or password (the WATCH secret patterns) is refused.
"""
import argparse
import datetime
import fcntl
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mesh = _load("fleet_mesh")
SLOT_SECS_DEFAULT = 900
RESULTS = ("ok", "failed", "cancelled")
FLOWS = ("password", "session")
SIGNIN_ROLES = ("", "auth")
HUMAN_ROLES = ("", "decide")  # the human's own shell, or DECIDE relaying the human's typed words (--quote required)
FREEZE_ROLES = ("", "route", "decide")


class Refused(Exception):
    pass


# ---------------------------------------------------------------- paths / env

def auth_dir():
    return os.path.join(mesh.state_dir(), "auth")


def log_path():
    return os.path.join(auth_dir(), "auth-log.jsonl")


def slot_path():
    return os.path.join(auth_dir(), "slot.json")


def freeze_path():
    return os.path.join(auth_dir(), "freeze.json")


def helpers_path():
    return os.environ.get("FLEET_AUTH_HELPERS") or os.path.join(auth_dir(), "helpers.json")


def auth_name():
    """AUTH desk lane: $FLEET_AUTH, else AUTH."""
    return os.environ.get("FLEET_AUTH") or "AUTH"


def me():
    return os.environ.get("FLEET_NAME") or "human"


def role():
    return os.environ.get("FLEET_ROLE") or ""


def slot_secs():
    try:
        return max(1, int(os.environ.get("FLEET_AUTH_SLOT_SECS") or SLOT_SECS_DEFAULT))
    except ValueError:
        return SLOT_SECS_DEFAULT


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    """ISO time (Z or offset, seconds optional) or HH:MM today in local time -> aware datetime."""
    s = (s or "").strip()
    if not s:
        raise ValueError("empty time")
    if len(s) <= 5 and ":" in s:
        h, m = s.split(":")
        return datetime.datetime.now().astimezone().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.astimezone()
    return t


# ---------------------------------------------------------------- secrets

_SECRET = None


def is_secret(text):
    global _SECRET
    if _SECRET is None:
        _SECRET = _load("fleet_listen").is_secret
    return _SECRET(text or "")


def _clean(field, value, limit=300):
    v = " ".join(str(value or "").split())
    if is_secret(v):
        raise Refused("%s looks like a secret; a token, key or password never goes in the auth log or a message" % field)
    if len(v) > limit:
        v = v[:limit - 3] + "..."
    return v


# ---------------------------------------------------------------- log (append-only)

def read_log():
    rows = []
    try:
        with open(log_path()) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        continue
    except FileNotFoundError:
        pass
    return rows


class _Locked:
    """Exclusive flock on auth/.lock: ids, the slot and the log move together."""

    def __enter__(self):
        os.makedirs(auth_dir(), exist_ok=True)
        self.f = open(os.path.join(auth_dir(), ".lock"), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _next_id(rows):
    n = 0
    for r in rows:
        rid = str(r.get("id") or "")
        if rid.startswith("A") and rid[1:].isdigit():
            n = max(n, int(rid[1:]))
    return "A%d" % (n + 1)


def _append(row):
    with open(log_path(), "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _read_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _write_json(p, obj):
    with open(p + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(p + ".tmp", p)


def read_slot():
    return _read_json(slot_path())


def read_freeze():
    return _read_json(freeze_path())


def _age(at):
    try:
        return (datetime.datetime.now(datetime.timezone.utc) - parse_time(at)).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def _guard_human(verb, quote):
    if role() not in HUMAN_ROLES:
        raise Refused("only the human (their own shell, or DECIDE relaying their typed answer) may %s" % verb)
    if role() == "decide" and not quote:
        raise Refused("DECIDE records this only with the human's typed words: --quote \"<their answer>\"")


def _guard_signin(verb):
    if role() not in SIGNIN_ROLES:
        raise Refused("only AUTH runs sign-ins (role %s may not %s); hand it over and keep working: "
                      "fleet auth ask --what \"<what failed>\"" % (role(), verb))


# ---------------------------------------------------------------- big digits (the number to match, readable at a glance)

_FONT = {
    "0": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "  ## ", " #   ", "#####"],
    "3": ["#### ", "    #", " ### ", "    #", "#### "],
    "4": ["#   #", "#   #", "#####", "    #", "    #"],
    "5": ["#####", "#    ", "#### ", "    #", "#### "],
    "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
    "7": ["#####", "    #", "   # ", "  #  ", "  #  "],
    "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
    "9": [" ### ", "#   #", " ####", "    #", " ### "],
}


def big(number):
    """The digits of number as a 5-row block banner, each glyph doubled in width so it reads across a pane."""
    rows = []
    for i in range(5):
        rows.append("   ".join("".join(c * 2 for c in _FONT[d][i]) for d in number if d in _FONT))
    return "\n".join(r.rstrip() for r in rows)


# ---------------------------------------------------------------- verbs

def ask(what, why=""):
    what = _clean("what", what)
    if not what:
        raise Refused("--what is empty")
    why = _clean("why", why)
    with _Locked():
        rows = read_log()
        rid = _next_id(rows)
        row = {"id": rid, "at": now_iso(), "event": "requested", "by": me(), "for": me(), "what": what}
        if why:
            row["why"] = why
        _append(row)
    text = "AUTH-ASK %s from %s: %s%s" % (rid, me(), what, (" (why: %s)" % why) if why else "")
    res = mesh.deliver(me(), auth_name(), text)
    return row, res


def _match_ok(match):
    match = _clean("match", match, 16)
    if match and not match.isalnum():
        raise Refused("the number to match is letters and digits only")
    return match


def begin(what, for_lane="", match="", ask_id="", flow="password"):
    _guard_signin("begin a sign-in")
    what = _clean("what", what)
    if not what:
        raise Refused("--what is empty")
    if flow not in FLOWS:
        raise Refused("--flow must be one of %s" % ", ".join(FLOWS))
    for_lane = _clean("for", for_lane or "human", 64)
    match = _match_ok(match)
    notes = []
    with _Locked():
        rows = read_log()
        if ask_id and not any(r.get("id") == ask_id and r.get("event") == "requested" for r in rows):
            raise Refused("no ask %s in the auth log" % ask_id)
        if ask_id and for_lane == "human":
            for_lane = next(r.get("by") for r in rows if r.get("id") == ask_id and r.get("event") == "requested")
        fr = read_freeze()
        if fr:
            raise Refused("sign-ins are frozen since %s by %s: %s (fleet auth thaw lifts it)" % (
                fr.get("at"), fr.get("by"), fr.get("reason")))
        slot = read_slot()
        if slot:
            age = _age(slot.get("at"))
            if age <= slot_secs():
                raise Refused("sign-in %s is in progress (%s for %s since %s); one at a time: wait for "
                              "fleet auth end %s" % (slot.get("id"), slot.get("what"), slot.get("for"), slot.get("at"),
                                                     slot.get("id")))
            _append({"id": slot.get("id"), "at": now_iso(), "event": "slot-broken", "by": me(),
                     "for": slot.get("for"), "what": slot.get("what"),
                     "note": "slot held %ds > FLEET_AUTH_SLOT_SECS %d" % (age, slot_secs())})
            notes.append("WARN broke stale sign-in slot %s (%ds old)" % (slot.get("id"), age))
        rid = _next_id(rows)
        heads = "AUTH heads-up: sign-in %s, %s for %s, starting now (%s)%s" % (
            rid, what, for_lane, "reuses a signed-in browser session" if flow == "session" else "fresh password sign-in",
            ("; match %s on the prompt" % match) if match else "; any number to match shows in AUTH's pane")
        res = mesh.deliver(me(), mesh.desk_name(), heads)  # raises before anything starts if the inbox can't be written
        at = now_iso()
        row = {"id": rid, "at": at, "event": "started", "by": me(), "for": for_lane, "what": what, "flow": flow}
        if match:
            row["match"] = match
        if ask_id:
            row["ask"] = ask_id
        _append(row)
        _write_json(slot_path(), {"id": rid, "at": at, "by": me(), "for": for_lane, "what": what, "flow": flow})
    return row, res, notes


def number(match, rid=""):
    """AUTH: the page shows a number to match. Log it (time, who asked), tell DECIDE; the caller prints it big."""
    _guard_signin("show a number to match")
    match = _match_ok(match)
    if not match:
        raise Refused("no number given")
    with _Locked():
        slot = read_slot()
        if not rid:
            if not slot:
                raise Refused("no sign-in in progress: a number with no announced sign-in is an unexpected prompt "
                              "(fleet auth unexpected-push)")
            rid = slot.get("id")
        st = [r for r in read_log() if r.get("id") == rid and r.get("event") == "started"]
        if not st:
            raise Refused("no started sign-in %s in the auth log" % rid)
        st = st[-1]
        row = {"id": rid, "at": now_iso(), "event": "number-shown", "by": me(), "for": st.get("for"),
               "what": st.get("what"), "match": match}
        _append(row)
    try:
        res = mesh.deliver(me(), mesh.desk_name(), "AUTH %s: match %s now (%s for %s)" % (
            rid, match, st.get("what"), st.get("for")))
    except (OSError, ValueError) as ex:  # the number is already on screen and logged; DECIDE's copy is a courtesy
        res = "DECIDE not told: %s" % ex
    return row, res


def end(rid, result, note=""):
    _guard_signin("end a sign-in")
    if result not in RESULTS:
        raise Refused("--result must be one of %s" % ", ".join(RESULTS))
    note = _clean("note", note)
    with _Locked():
        rows = read_log()
        started = [r for r in rows if r.get("id") == rid and r.get("event") == "started"]
        if not started:
            raise Refused("no started sign-in %s in the auth log" % rid)
        if any(r.get("id") == rid and r.get("event") == "ended" for r in rows):
            raise Refused("sign-in %s already ended" % rid)
        st = started[-1]
        row = {"id": rid, "at": now_iso(), "event": "ended", "by": me(), "for": st.get("for"), "what": st.get("what"),
               "result": result}
        if note:
            row["note"] = note
        _append(row)
        slot = read_slot()
        if slot and slot.get("id") == rid:
            os.remove(slot_path())
    res = None
    lane = st.get("for") or ""
    if lane and lane not in ("human", me()):
        try:
            mesh._check_name(lane)
            res = mesh.deliver(me(), lane, "AUTH %s %s%s: %s%s" % (
                rid, result, ", unconfirmed until the human says the prompts were the announced ones"
                if result == "ok" else "", st.get("what"), (" (%s)" % note) if note else ""))
        except (OSError, ValueError):
            res = None
    return row, res


def unexpected(at="", note="", quote="", rid=""):
    """Anyone: a push or number prompt no heads-up announced. Log it with the rows near that moment, route to DECIDE."""
    if role() == "decide" and not quote:
        raise Refused("DECIDE records this only with the human's typed words: --quote \"<their answer>\"")
    note = _clean("note", note)
    quote = _clean("quote", quote)
    t = parse_time(at) if at else datetime.datetime.now(datetime.timezone.utc)
    with _Locked():
        rows = read_log()
        near = []
        for r in rows:
            try:
                if abs((parse_time(r.get("at")) - t).total_seconds()) <= 600:
                    near.append(r)
            except (ValueError, TypeError):
                continue
        slot = read_slot()
        row = {"id": rid or _next_id(rows), "at": now_iso(), "event": "push-unexpected", "by": me(), "for": "human",
               "what": "a push or number prompt no heads-up announced, around %s" % t.astimezone(
                   datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "near": sorted(set("%s %s by %s" % (r.get("id"), r.get("event"), r.get("by")) for r in near))[:20]}
        if note:
            row["note"] = note
        if quote:
            row["quote"] = quote
        _append(row)
    lead = ("sign-in %s was open (%s for %s), so check it was that one" % (slot.get("id"), slot.get("what"),
                                                                        slot.get("for")) if slot else
            "no sign-in was open")
    text = ("AUTH ANOMALY %s: a push or number prompt reached the human that no heads-up announced (around %s); %s. "
            "Auth log rows within 10 min: %s. Ask the human whether they approved it; ROUTE or DECIDE can "
            "fleet auth freeze." % (row["id"], t.astimezone(datetime.timezone.utc).strftime("%H:%M:%SZ"), lead,
                                    ", ".join(row["near"]) or "none"))
    res = mesh.deliver(me(), mesh.desk_name(), text)
    return row, res


def confirm(rid, unexpected_seen, quote=""):
    _guard_human("confirm which prompts reached their phone", quote)
    quote = _clean("quote", quote)
    with _Locked():
        rows = read_log()
        ended = [r for r in rows if r.get("id") == rid and r.get("event") == "ended"]
        if not ended:
            raise Refused("no ended sign-in %s in the auth log (confirm after fleet auth end)" % rid)
        row = {"id": rid, "at": now_iso(), "event": "confirmed", "by": me(), "for": ended[-1].get("for"),
               "what": ended[-1].get("what"), "result": "unexpected" if unexpected_seen else "as-expected"}
        if quote:
            row["quote"] = quote
        _append(row)
    res = None
    if unexpected_seen:
        _, res = unexpected(note="the human saw a prompt beyond the announced ones during %s" % rid, quote=quote,
                            rid=rid)
    return row, res


def freeze(reason):
    if role() not in FREEZE_ROLES:
        raise Refused("only the human, ROUTE or DECIDE may freeze sign-ins (role %s)" % role())
    reason = _clean("reason", reason)
    if not reason:
        raise Refused("--reason is empty")
    with _Locked():
        row = {"id": _next_id(read_log()), "at": now_iso(), "event": "freeze", "by": me(), "for": "fleet",
               "what": reason}
        _append(row)
        _write_json(freeze_path(), {"at": row["at"], "by": me(), "reason": reason})
    return row


def thaw(quote=""):
    if role() not in FREEZE_ROLES:
        raise Refused("only the human, ROUTE or DECIDE may thaw sign-ins (role %s)" % role())
    quote = _clean("quote", quote)
    with _Locked():
        fr = read_freeze()
        if not fr:
            raise Refused("sign-ins are not frozen")
        row = {"id": _next_id(read_log()), "at": now_iso(), "event": "thaw", "by": me(), "for": "fleet",
               "what": "lifts: %s" % fr.get("reason")}
        if quote:
            row["quote"] = quote
        _append(row)
        os.remove(freeze_path())
    return row


def verdicts(rows=None):
    """{sign-in id: pass | fail | ok-unconfirmed | open | broken}: pass needs ended ok AND the human's as-expected."""
    rows = read_log() if rows is None else rows
    out = {}
    for r in rows:
        rid, ev = r.get("id"), r.get("event")
        if ev == "started":
            out[rid] = "open"
        elif ev == "slot-broken" and out.get(rid) == "open":
            out[rid] = "broken"
        elif ev == "ended" and rid in out:
            out[rid] = "ok-unconfirmed" if r.get("result") == "ok" else "fail"
        elif ev == "push-unexpected" and rid in out:
            out[rid] = "fail"
        elif ev == "confirmed" and rid in out:
            if r.get("result") == "unexpected":
                out[rid] = "fail"
            elif out[rid] == "ok-unconfirmed":
                out[rid] = "pass"
    return out


def open_asks(rows=None):
    rows = read_log() if rows is None else rows
    served = set(r.get("ask") for r in rows if r.get("event") == "started" and r.get("ask"))
    return [r for r in rows if r.get("event") == "requested" and r.get("id") not in served]


def trace(t, window_min=10):
    t = parse_time(t)
    out = []
    for r in read_log():
        try:
            d = abs((parse_time(r.get("at")) - t).total_seconds())
        except (ValueError, TypeError):
            continue
        if d <= window_min * 60:
            out.append(r)
    return out


def snapshot():
    rows = read_log()
    return {"slot": read_slot(), "freeze": read_freeze(), "openAsks": open_asks(rows), "verdicts": verdicts(rows),
            "numbers": [r for r in rows if r.get("event") == "number-shown"][-10:],
            "anomalies": [r for r in rows if r.get("event") == "push-unexpected"][-10:],
            "last": rows[-10:], "rows": len(rows), "log": log_path(), "helpers": helpers_path()}


def helpers():
    p = helpers_path()
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return p, None
    if not isinstance(data, list):
        raise Refused("%s must be a JSON list of {name, ref, use}" % p)
    return p, [{"name": str(h.get("name", "")), "ref": str(h.get("ref", "")), "use": str(h.get("use", ""))}
               for h in data if isinstance(h, dict)]


# ---------------------------------------------------------------- cli

def fmt(r):
    bits = ["%s %s %-15s by=%s for=%s" % (r.get("at"), r.get("id"), r.get("event"), r.get("by"), r.get("for")),
            "what=%s" % json.dumps(r.get("what"))]
    for k in ("result", "flow", "match", "ask", "why", "note", "quote"):
        if r.get(k):
            bits.append("%s=%s" % (k, json.dumps(r[k]) if k in ("why", "note", "quote") else r[k]))
    return " ".join(bits)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet auth", description="AUTH desk: sign-ins, the auth log, hand-offs")
    sp = ap.add_subparsers(dest="cmd")
    a = sp.add_parser("ask", help="hand an auth failure to AUTH and keep working")
    a.add_argument("--what", required=True)
    a.add_argument("--why", default="")
    b = sp.add_parser("begin", help="AUTH only: take the sign-in slot, heads-up to DECIDE, log started")
    b.add_argument("--what", required=True)
    b.add_argument("--for", dest="for_lane", default="")
    b.add_argument("--match", default="")
    b.add_argument("--ask", dest="ask_id", default="")
    b.add_argument("--flow", choices=FLOWS, default="password",
                   help="session: reuse an already signed-in browser session (preferred); password: a fresh sign-in")
    n = sp.add_parser("number", help="AUTH only: show the number to match big, log it, tell DECIDE")
    n.add_argument("match")
    n.add_argument("id", nargs="?", default="")
    e = sp.add_parser("end", help="AUTH only: log the result and free the slot")
    e.add_argument("id")
    e.add_argument("--result", required=True, choices=RESULTS)
    e.add_argument("--note", default="")
    u = sp.add_parser("unexpected-push", help="a push or number prompt no heads-up announced: log it, tell DECIDE")
    u.add_argument("--at", default="", help="when it reached the phone (ISO or HH:MM; default now)")
    u.add_argument("--note", default="")
    u.add_argument("--quote", default="")
    lg = sp.add_parser("log", help="the append-only auth log")
    lg.add_argument("--last", type=int, default=0)
    lg.add_argument("--for", dest="for_lane", default="")
    lg.add_argument("--json", action="store_true")
    tr = sp.add_parser("trace", help="rows near a time: who asked for the sign-in behind a prompt")
    tr.add_argument("time")
    tr.add_argument("--window", type=int, default=10, help="minutes either side (default 10)")
    tr.add_argument("--json", action="store_true")
    c = sp.add_parser("confirm", help="the human: were the prompts on your phone only the announced ones?")
    c.add_argument("id")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--as-expected", action="store_true")
    g.add_argument("--unexpected", action="store_true")
    c.add_argument("--quote", default="")
    fz = sp.add_parser("freeze", help="stop every sign-in")
    fz.add_argument("--reason", required=True)
    th = sp.add_parser("thaw", help="resume sign-ins")
    th.add_argument("--quote", default="")
    st = sp.add_parser("status", help="the slot, freeze, open asks, verdicts, last rows")
    st.add_argument("--json", action="store_true")
    sp.add_parser("helpers", help="the human's own sign-in helpers, by reference")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ask":
            row, res = ask(args.what, args.why)
            print("OK %s asked AUTH: %s" % (row["id"], row["what"]))
            print(res)
        elif args.cmd == "begin":
            row, res, notes = begin(args.what, args.for_lane, args.match, args.ask_id, args.flow)
            for x in notes:
                print(x)
            print("OK %s started: %s for %s (%s)%s" % (row["id"], row["what"], row["for"], row["flow"],
                                                       (" match %s" % row["match"]) if row.get("match") else ""))
            print("heads-up -> %s: %s" % (mesh.desk_name(), res))
            if row.get("match"):
                print("\nMATCH THIS NUMBER (%s, %s for %s):\n\n%s\n" % (row["id"], row["what"], row["for"],
                                                                       big(row["match"])))
        elif args.cmd == "number":
            row, res = number(args.match, args.id)
            print("\nMATCH THIS NUMBER (%s, %s for %s, %s):\n\n%s\n" % (row["id"], row["what"], row["for"], row["at"],
                                                                       big(row["match"])))
            print("OK %s number-shown %s; -> %s: %s" % (row["id"], row["match"], mesh.desk_name(), res))
        elif args.cmd == "end":
            row, res = end(args.id, args.result, args.note)
            print("OK %s %s: %s" % (row["id"], row["result"], row["what"]))
            if res:
                print("told %s: %s" % (row["for"], res))
        elif args.cmd == "unexpected-push":
            row, res = unexpected(args.at, args.note, args.quote)
            print("OK %s push-unexpected logged; -> %s: %s" % (row["id"], mesh.desk_name(), res))
        elif args.cmd == "confirm":
            row, res = confirm(args.id, args.unexpected, args.quote)
            if args.unexpected:
                print("OK %s confirmed unexpected prompt: FAIL; routed to %s: %s" % (row["id"], mesh.desk_name(), res))
            else:
                print("OK %s confirmed as expected: %s" % (row["id"], verdicts().get(row["id"])))
        elif args.cmd == "freeze":
            row = freeze(args.reason)
            print("OK sign-ins frozen: %s" % row["what"])
        elif args.cmd == "thaw":
            thaw(args.quote)
            print("OK sign-ins thawed")
        elif args.cmd == "log":
            rows = read_log()
            if args.for_lane:
                rows = [r for r in rows if r.get("for") == args.for_lane or r.get("by") == args.for_lane]
            if args.last:
                rows = rows[-args.last:]
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print("\n".join(fmt(r) for r in rows) if rows else "(auth log empty: %s)" % log_path())
        elif args.cmd == "trace":
            rows = trace(args.time, args.window)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print("\n".join(fmt(r) for r in rows) if rows else
                      "(no auth log rows within %d min of %s: that prompt did not come from AUTH)" % (args.window,
                                                                                                    args.time))
        elif args.cmd == "status":
            s = snapshot()
            if args.json:
                print(json.dumps(s, indent=2))
            else:
                sl = s["slot"]
                print("slot: %s" % ("free" if not sl else "%s %s for %s since %s (%s)" % (
                    sl["id"], sl["what"], sl["for"], sl["at"], sl.get("flow"))))
                fr = s["freeze"]
                print("freeze: %s" % ("none" if not fr else "since %s by %s: %s" % (fr["at"], fr["by"], fr["reason"])))
                pend = [k for k, v in s["verdicts"].items() if v in ("open", "ok-unconfirmed")]
                print("sign-ins: %s" % (", ".join("%s %s" % (k, s["verdicts"][k]) for k in pend) or "none pending"))
                print("anomalies: %d" % len(s["anomalies"]))
                print("open asks: %d" % len(s["openAsks"]))
                for r in s["openAsks"]:
                    print("  " + fmt(r))
                print("log: %s (%d rows)" % (s["log"], s["rows"]))
        elif args.cmd == "helpers":
            p, hs = helpers()
            if hs is None:
                print("(no helpers file: %s; the human lists their own sign-in scripts and skills there as "
                      "[{\"name\", \"ref\", \"use\"}])" % p)
            else:
                for h in hs:
                    print("%s\t%s\t%s" % (h["name"], h["ref"], h["use"]))
        else:
            ap.print_help()
            return 2
    except Refused as ex:
        print("FAIL %s" % ex, file=sys.stderr)
        return 1
    except (ValueError, OSError) as ex:  # OSError: an inbox or state file can't be written; nothing is half-done
        print("FAIL %s" % ex, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
