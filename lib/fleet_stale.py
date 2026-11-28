#!/usr/bin/env python3
"""Stale open calls (issue #63): claim refs that changed, and calls past their recheck time.

Pure helpers first (no file or mesh access), then the two small ledgers, then a CLI the refresh path runs.

  match(calls, events)        open calls x WATCH events -> one hit per (call, claim, ref): the ref changed
  stale_line(hit)             'STALE Q92 claim c1: ref acme/widget#333 changed (pr.merged) <eid>'  (WATCH -> DECIDE)
  recheck_due(calls, now)     open calls whose recheckAfter has passed
  recheck_line(call)          'RECHECK Q12 from <asker>: recheckAfter <iso> passed'               (WATCH -> DECIDE)
  parse_recheck(value, now)   ISO 8601 UTC or a duration (30m, 4h, 2d) -> ISO 8601 UTC

Matching is on machine refs only, never free text: a claim ref matches an event when it equals the event's ref or
its url, after trimming. A claim ref must use WATCH's ref form ("owner/repo#12", "PROJ-1") or the full URL; a bare
"#12" matches nothing, so one repo's #12 never flags a call about another repo's #12.

Ledgers (append-only JSONL, each key once):
  $FLEET_STATE/watch/stale.jsonl     written by WATCH: {at, id, claim, ref, eid, type, url, line}
  $FLEET_STATE/watch/recheck.jsonl   written by WATCH: {at, id, recheckAfter, line}
  $FLEET_STATE/decide/recheck-pinged.jsonl   written by DECIDE intake: {at, id, recheckAfter, to}

  python3 lib/fleet_stale.py candidates    JSON lines for a `candidates` refresh source (kind "stale")
  python3 lib/fleet_stale.py scan          what WATCH would flag now, without writing (debug)

Python 3.8+ stdlib only.
"""
import datetime
import json
import os
import re
import sys

STALE_LINE = re.compile(r"^STALE\s+(Q\d+)\s+claim\s+([A-Za-z0-9_.-]+):\s+ref\s+(\S+)\s+changed\s+\(([^)]*)\)\s+(\S+)\s*$")
RECHECK_LINE = re.compile(r"^RECHECK\s+(Q\d+)\s+from\s+(\S+):\s+recheckAfter\s+(\S+)\s+passed\s*$")
_DUR = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.I)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
REF_MAX = 500


# ---------------------------------------------------------------- time

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not isinstance(s, str) or not _ISO.match(s.strip()):
        return None
    try:
        return datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_recheck(value, now=None):
    """recheckAfter as given by the asker: an ISO 8601 time, or a duration from now (30m, 4h, 2d). Returns ISO UTC."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("`recheckAfter` must be an ISO 8601 time or a duration like 30m, 4h, 2d")
    m = _DUR.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if n <= 0:
            raise ValueError("`recheckAfter` duration must be positive")
        delta = {"m": datetime.timedelta(minutes=n), "h": datetime.timedelta(hours=n),
                 "d": datetime.timedelta(days=n)}[unit]
        return iso((now or utcnow()) + delta)
    t = parse_iso(value)
    if t is None:
        raise ValueError("`recheckAfter` must be an ISO 8601 time or a duration like 30m, 4h, 2d (got %r)" % value)
    return iso(t)


# ---------------------------------------------------------------- claims (one accessor: the #61 shape lives here)

def claims_of(call):
    """The claims a call rests on: decision_open's `claims` list (#61), each {text, source, options, refs}."""
    c = call.get("claims") if isinstance(call, dict) else None
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def norm_ref(r):
    if not isinstance(r, str):
        return None
    r = r.strip()
    if not r or len(r) > REF_MAX or "\n" in r:
        return None
    return r


def claim_id(cl, k):
    """A claim's id (#58 schema: "c1", "c2"...), or c<k> by position when the row predates claim ids."""
    i = cl.get("id")
    return i.strip() if isinstance(i, str) and re.match(r"^\s*[A-Za-z0-9_.-]{1,40}\s*$", i) else "c%d" % k


def claim_refs(call):
    """[(claim id, ref)] for every machine ref of every claim."""
    out = []
    for n, cl in enumerate(claims_of(call), 1):
        k = claim_id(cl, n)
        refs = cl.get("refs")
        if isinstance(refs, str):
            refs = [refs]
        for r in refs if isinstance(refs, list) else []:
            n = norm_ref(r)
            if n and (k, n) not in out:
                out.append((k, n))
    return out


def _get(ev, k):
    return ev.get(k) if isinstance(ev, dict) else getattr(ev, k, None)


def event_keys(ev):
    return {n for n in (norm_ref(_get(ev, "ref")), norm_ref(_get(ev, "url"))) if n}


def is_open(call):
    return isinstance(call, dict) and call.get("status", "open") == "open"


def match(calls, events):
    """One hit per (call id, claim, ref): the latest event whose ref or url equals the claim ref and that happened at
    or after the call opened. Calls that are not open, and events without an eid, are skipped."""
    hits = {}
    for call in calls:
        if not is_open(call) or not call.get("id"):
            continue
        refs = claim_refs(call)
        if not refs:
            continue
        opened = parse_iso(call.get("at"))
        for ev in events:
            eid = _get(ev, "eid")
            if not eid:
                continue
            keys = event_keys(ev)
            if not keys:
                continue
            at = parse_iso(_get(ev, "at"))
            if opened and at and at < opened:
                continue  # the call was asked after this change: its claims already saw it
            for k, ref in refs:
                if ref not in keys:
                    continue
                key = (call["id"], k, ref)
                prev = hits.get(key)
                if prev and (prev["at"] or "") > (_get(ev, "at") or ""):
                    continue
                hits[key] = {"id": call["id"], "from": call.get("from"), "claim": k, "ref": ref, "eid": eid,
                             "type": _get(ev, "type") or "changed", "at": _get(ev, "at"), "url": _get(ev, "url") or ""}
    return sorted(hits.values(), key=lambda h: (_idnum(h["id"]), _idnum(h["claim"]), h["claim"], h["ref"]))


def _idnum(i):
    m = re.search(r"(\d+)$", i or "")
    return int(m.group(1)) if m else 0


def _one_word(s):
    return re.sub(r"[\s()]+", "-", str(s or "changed")).strip("-") or "changed"


def stale_line(hit):
    return "STALE %s claim %s: ref %s changed (%s) %s" % (hit["id"], hit["claim"], hit["ref"], _one_word(hit["type"]),
                                                          hit["eid"])


def parse_stale_line(line):
    m = STALE_LINE.match(line or "")
    if not m:
        return None
    return {"id": m.group(1), "claim": m.group(2), "ref": m.group(3), "type": m.group(4), "eid": m.group(5)}


def recheck_due(calls, now=None):
    now = now or utcnow()
    out = []
    for call in calls:
        if not is_open(call) or not call.get("id"):
            continue
        t = parse_iso(call.get("recheckAfter"))
        if t and t <= now:
            out.append(call)
    return out


def recheck_line(call):
    return "RECHECK %s from %s: recheckAfter %s passed" % (call["id"], _one_word(call.get("from") or "unknown"),
                                                         call["recheckAfter"])


def parse_recheck_line(line):
    m = RECHECK_LINE.match(line or "")
    return {"id": m.group(1), "from": m.group(2), "recheckAfter": m.group(3)} if m else None


def ask_revise_text(hit):
    """What the desk sends the asker when a stale row is confirmed (refresh choose B + confirm)."""
    return ("[DECIDE] %s claim %s: ref %s changed (%s) since you asked. Check that the claim still holds; if it "
            "does not, call decision_revise on %s with the corrected text and why." % (
                hit["id"], hit["claim"], hit["ref"], hit["type"], hit["id"]))


def recheck_text(call):
    return ("[DECIDE] %s is still open and its recheck time (%s) has passed. Re-check what the call rests on; if "
            "anything moved, call decision_revise on %s." % (call["id"], call.get("recheckAfter"), call["id"]))


# ---------------------------------------------------------------- ledgers

def read_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def append_jsonl(path, recs):
    if not recs:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def stale_key(h):
    return "%s|%s|%s|%s" % (h.get("id"), h.get("claim"), h.get("ref"), h.get("eid"))


def recheck_key(c):
    return "%s|%s" % (c.get("id"), c.get("recheckAfter"))


def new_stale(ledger_path, hits, now=None):
    """Hits not yet in the ledger; appends them (with their STALE line) and returns them."""
    seen = {stale_key(r) for r in read_jsonl(ledger_path)}
    at = iso(now or utcnow())
    fresh, recs = [], []
    for h in hits:
        k = stale_key(h)
        if k in seen:
            continue
        seen.add(k)
        fresh.append(h)
        recs.append({"at": at, "id": h["id"], "from": h.get("from"), "claim": h["claim"], "ref": h["ref"],
                     "eid": h["eid"], "type": h["type"], "url": h.get("url") or "", "line": stale_line(h)})
    append_jsonl(ledger_path, recs)
    return fresh


def new_recheck(ledger_path, calls, now=None):
    """Due calls not yet flagged for this recheckAfter value; appends them and returns them."""
    seen = {recheck_key(r) for r in read_jsonl(ledger_path)}
    at = iso(now or utcnow())
    fresh, recs = [], []
    for c in calls:
        k = recheck_key(c)
        if k in seen:
            continue
        seen.add(k)
        fresh.append(c)
        recs.append({"at": at, "id": c["id"], "recheckAfter": c["recheckAfter"], "line": recheck_line(c)})
    append_jsonl(ledger_path, recs)
    return fresh


def stale_by_call(ledger_path, open_ids=None):
    """{id: [{claim, ref, type, at, eid, url}]} from the WATCH ledger, latest per (claim, ref)."""
    out = {}
    for r in read_jsonl(ledger_path):
        cid = r.get("id")
        if not cid or (open_ids is not None and cid not in open_ids):
            continue
        rows = out.setdefault(cid, [])
        rows[:] = [x for x in rows if (x["claim"], x["ref"]) != (r.get("claim"), r.get("ref"))]
        rows.append({k: r.get(k) for k in ("claim", "ref", "type", "at", "eid", "url")})
    for rows in out.values():
        rows.sort(key=lambda x: (str(x["claim"] or ""), x["ref"] or ""))
    return out


def candidates(ledger_path, open_ids):
    """Refresh candidates (fleet_decisions refresh, kind 'stale'): one per open call, its latest stale row. The quote
    is the ledger row's STALE line, which is verbatim in the ledger file, so the refresh grounding check holds."""
    latest = {}
    for r in read_jsonl(ledger_path):
        cid = r.get("id")
        if cid in open_ids and r.get("line"):
            latest[cid] = r
    out = []
    for cid in sorted(latest, key=_idnum):
        r = latest[cid]
        h = {"id": cid, "claim": r.get("claim"), "ref": r.get("ref"), "type": r.get("type")}
        out.append({"id": cid, "kind": "stale", "quote": r["line"], "path": ledger_path, "at": r.get("at"),
                    "text": ask_revise_text(h), "claim": r.get("claim"), "ref": r.get("ref")})
    return out


# ---------------------------------------------------------------- CLI (uses the mesh for paths and calls)

def _mesh():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import fleet_mesh
    return fleet_mesh


def ledger_paths(state_dir):
    return {"stale": os.path.join(state_dir, "watch", "stale.jsonl"),
            "recheck": os.path.join(state_dir, "watch", "recheck.jsonl"),
            "pinged": os.path.join(state_dir, "decide", "recheck-pinged.jsonl")}


def main(argv):
    mesh = _mesh()
    lp = ledger_paths(mesh.state_dir())
    cmd = argv[0] if argv else ""
    calls = [d for d in mesh.decisions() if is_open(d)]
    if cmd == "candidates":
        for c in candidates(lp["stale"], {d["id"] for d in calls}):
            print(json.dumps(c, sort_keys=True))
        return 0
    if cmd == "scan":
        events = read_jsonl(os.path.join(mesh.state_dir(), "watch", "events.jsonl"))
        for h in match(calls, events):
            print(stale_line(h))
        for c in recheck_due(calls):
            print(recheck_line(c))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
