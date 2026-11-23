#!/usr/bin/env python3
"""fleet learn: the LEARN desk's ledger, cards, terminal quiz and proof of learning. Contract: lib/fleet_learn_contract.md

Python 3.8+ stdlib only. Writes only under $FLEET_STATE/learn/ (export --to DIR by the human aside).

  fleet learn add --kind K --origin fleet|outside --source TYPE:REF --quote TEXT [--status confirmed|heard]
                  [--title T] [--what W] [--why Y] [--topic T] [--tag T ...]   add a ledger row; prints "OK added L<n>"
  fleet learn list [--origin O] [--status S] [--all] [--json]   latest row per lid, counts per origin
  fleet learn show <lid> [--json]                  one row, its history and its cards
  fleet learn confirm <lid> --source TYPE:REF --quote TEXT   heard -> confirmed (needs another source and its quote)
  fleet learn drop <lid> --reason TEXT             retire a row and its cards (e.g. answer not in its quote)
  fleet learn sync [--goal FILE --lane NAME] [--json]   decisions-log.jsonl, WATCH LEARN-ITEMs, achieved goal files,
                                                   then make cards; a missing log or events file is not an error
  fleet learn due [--json]                         cards due now
  fleet learn quiz [--max N]                       terminal quiz over due cards (reads answers on stdin; q quits)
  fleet learn review <cid> --answer TEXT [--rating again|hard|good|easy]   grade one answer (the quiz's verb)
  fleet learn proof [--rebuild] [--check]          per-row proof; --check exits 1 if proof.json != a rebuild
  fleet learn snapshot                             snapshot() as JSON
  fleet learn export [--to DIR]                    markdown, one file per topic (default $FLEET_STATE/learn/export)
  fleet learn serve [--port P] [--bind 127.0.0.1] [--open]   the phase 7 page (lib/fleet_learn_serve.py, when present)

Clock: FLEET_LEARN_NOW=<ISO-8601 UTC> pins "now" (tests). Failure prints "FAIL <reason>" to stderr and exits 1;
usage errors exit 2.
"""
import argparse
import datetime
import fcntl
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import fleet_mesh as mesh  # noqa: E402

ORIGINS = ("fleet", "outside")
KINDS = ("decision", "change", "closed", "doc", "lesson")
STATUSES = ("confirmed", "heard", "superseded", "dropped")
LIVE = ("confirmed", "heard")
ROUTED_KINDS = ("ask", "task-candidate", "fyi")  # lib/fleet_watch_contract.py KINDS
RATINGS = ("again", "hard", "good", "easy")
GENERATIVE = ("recall", "explain", "apply")
CHECKABLE = ("choice", "source")
INTERVALS_DAYS = (1, 3, 7, 21)
SPACING = datetime.timedelta(hours=24)
SOURCE_MIN = 8            # a source-check paste shorter than this (and shorter than the quote) proves nothing
LID_RE = re.compile(r"^L[0-9]+$")
CID_RE = re.compile(r"^C[0-9]+$")


class Refused(Exception):
    pass


# ---------------------------------------------------------------- clock

def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _now(now=None):
    if now is not None:
        return now if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)
    pinned = _parse_ts(os.environ.get("FLEET_LEARN_NOW"))
    return pinned or datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- paths and store

def learn_dir():
    return os.path.join(mesh.state_dir(), "learn")


def _p(name):
    return os.path.join(learn_dir(), name)


def _inside(path, root):
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep)


def _guard(path):
    """Every store write goes through here: the resolved path must sit inside learn/."""
    os.makedirs(learn_dir(), exist_ok=True)
    if not _inside(path, learn_dir()):
        raise Refused("write outside %s refused: %s" % (learn_dir(), os.path.realpath(path)))
    return path


def _append(name, row):
    mesh._append_line(_guard(_p(name)), json.dumps(row, sort_keys=True))
    return row


def _write(name, text):
    p = _guard(_p(name))
    tmp = _guard(_p(".%s.%d.tmp" % (name, os.getpid())))
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _read_json(name, default):
    try:
        with open(_p(name)) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


class _Lock(object):
    def __enter__(self):
        self.f = open(_guard(_p(".lock")), "a")
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        self.f.close()


# ---------------------------------------------------------------- scheduler (phase 5 swaps in FSRS)

def fixed_schedule(sched, rating, now):
    """Fixed intervals 1 d / 3 d / 7 d / 21 d. good/easy step up, hard repeats the step, again resets (a lapse).
    Writes only due, fixedStep, reps, lapses, lastReview; any other key (FSRS state, step, stability,
    difficulty) passes through untouched."""
    s = dict(sched or {})
    step = int(s.get("fixedStep") or 0)
    s["reps"] = int(s.get("reps") or 0) + 1
    if rating == "again":
        s["lapses"] = int(s.get("lapses") or 0) + 1
        step = 0
    elif rating in ("good", "easy"):
        step = min(step + (2 if rating == "easy" else 1), len(INTERVALS_DAYS))
    days = INTERVALS_DAYS[max(step - 1, 0)] if step else INTERVALS_DAYS[0]
    s["fixedStep"] = step
    s["lastReview"] = _iso(now)
    s["due"] = _iso(now + datetime.timedelta(days=days))
    return s


SCHEDULERS = ("fsrs", "fixed")  # learn.json "scheduler"; fsrs is the default (4.3), fixed the opt-out
_SCHEDULER = [None]  # set by use_scheduler(); None = resolve from learn.json on each review


def use_scheduler(fn):
    """Pin a scheduler for this process (tests, phase 5). None goes back to learn.json resolution."""
    _SCHEDULER[0] = fn


def scheduler():
    """The scheduler review() will call. learn.json {"scheduler": "fixed"} opts out of FSRS. FSRS that is not
    importable, or a bad "fsrs" section, raises Refused: never a silent fallback, and never after a review row."""
    if _SCHEDULER[0] is not None:
        return _SCHEDULER[0]
    cfg = _read_json("learn.json", {})
    name = (cfg.get("scheduler") if isinstance(cfg, dict) else None) or "fsrs"
    if name not in SCHEDULERS:
        raise Refused("learn.json scheduler must be fsrs or fixed, got %r" % name)
    if name == "fixed":
        return fixed_schedule
    try:
        import fleet_fsrs  # phase 5
        fn = fleet_fsrs.schedule
    except (ImportError, AttributeError) as e:
        raise Refused("scheduler fsrs (the default) needs lib/fleet_fsrs.py with schedule() (%s); "
                      "set learn.json {\"scheduler\": \"fixed\"} to use fixed intervals" % e)
    check = getattr(fleet_fsrs, "configured", None)
    if check is not None:
        try:
            check()  # a bad fsrs section fails here, before any review row is appended
        except Exception as e:  # noqa: BLE001  (fleet_fsrs's own error type)
            raise Refused("learn.json fsrs: %s" % e)
    return fn


# ---------------------------------------------------------------- ledger

def _rows():
    return mesh._parse_lines(_p("ledger.jsonl"))


def items(origin=None, status=None, include_retired=False):
    order, latest = [], {}
    for r in _rows():
        lid = r.get("lid")
        if not isinstance(lid, str):
            continue
        if lid not in latest:
            order.append(lid)
        latest[lid] = r
    out = [latest[k] for k in order]
    if not include_retired and status not in ("superseded", "dropped"):
        out = [r for r in out if r.get("status") in LIVE]
    if origin:
        out = [r for r in out if r.get("origin") == origin]
    if status:
        out = [r for r in out if r.get("status") == status]
    return out


def item(lid):
    if not LID_RE.match(lid or ""):
        raise Refused("bad row id %r (want L<n>)" % lid)
    for r in items(include_retired=True):
        if r["lid"] == lid:
            return r
    raise Refused("no row %s" % lid)


def history(lid):
    return [r for r in _rows() if r.get("lid") == lid]


def _need(value, what):
    if not isinstance(value, str) or not value.strip():
        raise Refused("%s is required and may not be empty" % what)
    return value


def _check_source(source):
    if not isinstance(source, dict) or not str(source.get("type") or "").strip() \
            or not str(source.get("ref") or "").strip():
        raise Refused("a row needs a source with a type and a ref")
    return source


def _next_id(prefix, rows, key):
    n = 0
    for r in rows:
        v = str(r.get(key) or "")
        if v.startswith(prefix) and v[1:].isdigit():
            n = max(n, int(v[1:]))
    return "%s%d" % (prefix, n + 1)


def add_item(kind, origin, quote, source, title="", what="", why="", status="confirmed", links=None, tags=None,
             topic=None, by="manual", supersedes=None, now=None):
    """Append a new ledger row and return it. Refuses a row with no quote, no source, or a bad origin/kind/status."""
    _need(quote, "quote (verbatim from the source)")
    _check_source(source)
    if origin not in ORIGINS:
        raise Refused("origin must be fleet or outside, got %r" % (origin,))
    if kind not in KINDS:
        raise Refused("kind must be one of %s, got %r" % ("|".join(KINDS), kind))
    if status not in LIVE:
        raise Refused("a new row is confirmed or heard, got %r" % (status,))
    tags = [str(t) for t in (tags or [])]
    with _Lock():
        row = {"lid": _next_id("L", _rows(), "lid"), "at": _iso(_now(now)), "kind": kind, "origin": origin,
               "status": status, "title": title or "", "what": what or "", "why": why or "", "source": source,
               "quote": quote, "links": list(links or []), "tags": tags,
               "topic": topic or (tags[0] if tags else str(source.get("ref"))), "supersedes": supersedes,
               "by": by, "confirmedBy": None}
        return _append("ledger.jsonl", row)


def confirm(lid, source, quote, by="manual", now=None):
    r = item(lid)
    if r.get("status") != "heard":
        raise Refused("%s is %s; only a heard row can be confirmed" % (lid, r.get("status")))
    _check_source(source)
    _need(quote, "quote (verbatim from the confirming source)")
    own = (r.get("source") or {})
    if (str(source.get("type")), str(source.get("ref"))) == (str(own.get("type")), str(own.get("ref"))):
        raise Refused("the confirming source must differ from the row's own heard source")
    return _append("ledger.jsonl", dict(r, at=_iso(_now(now)), status="confirmed", by=by,
                                        confirmedBy={"source": source, "quote": quote}))


def _retire_cards(lid):
    cs = _cards()
    hit = False
    for c in cs:
        if c.get("lid") == lid and not c.get("retired"):
            c["retired"] = True
            hit = True
    if hit:
        _save_cards(cs)


def supersede(lid, quote, source, by="sync", now=None):
    old = item(lid)
    if old.get("status") not in LIVE:
        raise Refused("%s is %s and cannot be superseded" % (lid, old.get("status")))
    new = add_item(old["kind"], old["origin"], quote, source, title=old.get("title"), what=old.get("what"),
                   why=old.get("why"), status="confirmed" if old.get("status") == "confirmed" else "heard",
                   links=old.get("links"), tags=old.get("tags"), topic=old.get("topic"), by=by, supersedes=lid,
                   now=now)
    _append("ledger.jsonl", dict(old, at=_iso(_now(now)), status="superseded", supersededBy=new["lid"], by=by))
    _retire_cards(lid)
    return new


def drop(lid, reason, now=None):
    r = item(lid)
    _need(reason, "reason")
    if r.get("status") not in LIVE:
        raise Refused("%s is already %s" % (lid, r.get("status")))
    row = _append("ledger.jsonl", dict(r, at=_iso(_now(now)), status="dropped", dropReason=reason, by="manual"))
    _retire_cards(lid)
    return row


def parse_source(text):
    """CLI form TYPE:REF (a bare REF becomes manual:REF)."""
    _need(text, "source")
    t, sep, ref = text.partition(":")
    if not sep or not t or not ref or not re.match(r"^[a-z][a-z0-9-]*$", t):
        return {"type": "manual", "ref": text}
    return {"type": t, "ref": ref}


# ---------------------------------------------------------------- sync

def _norm(text):
    return " ".join((text or "").split()).lower()


def _by_source():
    out = {}
    for r in items(include_retired=True):
        s = r.get("source") or {}
        if r.get("status") in LIVE:
            out[(s.get("type"), str(s.get("ref")))] = r
        else:
            out.setdefault((s.get("type"), str(s.get("ref"))), r)
    return out


def _questions():
    out = {}
    for d in mesh._parse_lines(os.path.join(mesh.state_dir(), "decisions.jsonl")):
        if d.get("id") and d.get("question"):
            out[d["id"]] = d["question"]
    return out


def _sync_decisions(res, now):
    path = os.path.join(mesh.state_dir(), "decisions-log.jsonl")
    res["decisionsLog"] = {"path": path, "present": os.path.exists(path), "read": 0, "skipped": 0}
    st = res["decisionsLog"]
    if not st["present"]:
        return []
    answers, heard = {}, []
    for n, ln in enumerate(mesh._read_lines(path), 1):
        st["read"] += 1
        try:
            r = json.loads(ln)
        except ValueError:
            st["skipped"] += 1
            continue
        if not isinstance(r, dict) or r.get("retry") is True or not str(r.get("verbatim") or "").strip():
            st["skipped"] += 1
            continue
        if r.get("id"):
            answers[str(r["id"])] = (n, r)
        elif r.get("kind") == "decided-on-call":
            heard.append((n, r))
        else:
            st["skipped"] += 1
    qs, have = _questions(), _by_source()
    for did, (n, r) in sorted(answers.items(), key=lambda kv: kv[1][0]):
        src = {"type": "decisions-log", "ref": did, "path": "$FLEET_STATE/decisions-log.jsonl", "line": n}
        old = have.get(("decisions-log", did))
        if old is None:
            row = add_item("decision", "fleet", r["verbatim"], src, title=qs.get(did, ""), topic=r.get("asker") or did,
                           by="sync", now=now)
            res["added"].append(row["lid"])
        elif old.get("status") in LIVE and old.get("quote") != r["verbatim"]:
            row = supersede(old["lid"], r["verbatim"], src, now=now)
            res["superseded"].append([old["lid"], row["lid"]])
    for n, r in heard:
        ref = "line:%d" % n
        if ("decisions-log", ref) in have:
            continue
        src = {"type": "decisions-log", "ref": ref, "path": "$FLEET_STATE/decisions-log.jsonl", "line": n,
               "kind": "decided-on-call", "speaker": r.get("asker"), "clock": r.get("clock")}
        row = add_item("decision", "fleet", r["verbatim"], src, status="heard", topic=r.get("from") or "call",
                       by="sync", now=now)
        res["added"].append(row["lid"])
    return [(did, n, r) for did, (n, r) in sorted(answers.items(), key=lambda kv: kv[1][0])]


def _min_score():
    for f in (os.path.join(mesh.state_dir(), "watch.json"), _p("learn.json")):
        try:
            with open(f) as fh:
                v = (json.load(fh).get("learn") or {}).get("minScore")
            if isinstance(v, (int, float)):
                return v
        except (FileNotFoundError, ValueError, AttributeError):
            continue
    return 1


def add_learn_item(event, now=None):
    """One WATCH event (a watch/events.jsonl row) -> an outside ledger row. None for a dupe or an empty quote."""
    if not isinstance(event, dict) or not event.get("eid"):
        raise Refused("a LEARN-ITEM needs an event with an eid")
    if not str(event.get("quote") or "").strip():
        return None
    if ("watch", str(event["eid"])) in _by_source():
        return None
    t = str(event.get("type") or "")
    kind = ("change" if any(w in t for w in ("merged", "release", "transition")) else
            "closed" if "closed" in t else "doc" if t.startswith("page") else "change")
    src = {"type": "watch", "ref": str(event["eid"]), "source": event.get("source"),
           "sourceKind": event.get("sourceKind"), "url": event.get("url"), "itemRef": event.get("ref")}
    links = [{"type": event.get("sourceKind") or "watch", "ref": event.get("ref"), "eid": event["eid"],
              "url": event.get("url")}]
    return add_item(kind, "outside", event["quote"], src, title=event.get("title") or "", links=links,
                    tags=event.get("tags") or [], topic=str(event.get("ref") or event.get("source") or "watch"),
                    by="watch", now=now)


def _sync_watch(res, now):
    path = os.path.join(mesh.state_dir(), "watch", "events.jsonl")
    cur = _read_json("cursors.json", {})
    start = int(cur.get("watch-events") or 0)
    lines = mesh._read_lines(path)
    if start > len(lines):
        start = 0
    res["watchEvents"] = {"path": path, "present": os.path.exists(path), "read": len(lines) - start}
    ms = _min_score()
    for ln in lines[start:]:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("origin", "outside") != "outside":
            continue  # call events (origin call) reach LEARN through decisions-log.jsonl as decided-on-call rows
        # Event.is_learn_item(minScore) in lib/fleet_watch_contract.py: a routed kind and score >= minScore
        if ev.get("kind") not in ROUTED_KINDS or not isinstance(ev.get("score"), (int, float)) or ev["score"] < ms:
            continue
        row = add_learn_item(ev, now=now)
        if row:
            res["added"].append(row["lid"])
    cur["watch-events"] = len(lines)
    _write("cursors.json", json.dumps(cur, indent=1, sort_keys=True) + "\n")


def _goal_text(d):
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        for k in ("summary", "what", "evidence", "text", "title", "decision"):
            if isinstance(d.get(k), str) and d[k].strip():
                return d[k]
        return json.dumps(d, sort_keys=True)
    return ""


def sync_goal(path, lane, now=None):
    """done[] and decisions[] of one finished lane's goal file -> fleet rows (lesson / decision). Returns new lids."""
    try:
        with open(path) as f:
            g = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused("cannot read goal file %s: %s" % (path, e))
    have, out = _by_source(), []
    for field, kind in (("done", "lesson"), ("decisions", "decision")):
        for i, d in enumerate(g.get(field) or []):
            text = _goal_text(d)
            ref = "%s:%s[%d]" % (lane, field, i)
            if not text.strip() or ("goal", ref) in have:
                continue
            title = d.get("decision") if isinstance(d, dict) and field == "decisions" else ""
            row = add_item(kind, "fleet", text, {"type": "goal", "ref": ref, "path": path, "lane": lane},
                           title=(g.get("goal") or "")[:120] if not title else title, topic=lane, by="sync", now=now)
            out.append(row["lid"])
    return out


def _sync_goals(res, now):
    roster = mesh.load_roster()
    cur = _read_json("cursors.json", {})
    done = cur.get("goals") or {}
    for name, e in sorted(roster.items()):
        if not isinstance(e, dict) or e.get("goalState") != "achieved" or done.get(name) == "achieved":
            continue
        txt = os.path.join(mesh.state_dir(), "launch", "%s.txt" % name)
        gfile = None
        try:
            with open(txt) as f:
                m = re.search(r"(~|/)[^ \"']*\.json", f.read())
            if m:
                gfile = os.path.expanduser(m.group(0))
        except OSError:
            pass
        if gfile and os.path.exists(gfile):
            res["added"] += sync_goal(gfile, name, now=now)
            done[name] = "achieved"
    cur = _read_json("cursors.json", {})
    cur["goals"] = done
    _write("cursors.json", json.dumps(cur, indent=1, sort_keys=True) + "\n")


def sync(now=None):
    """decisions-log.jsonl (desk answers confirmed, decided-on-call heard; latest answer per id supersedes),
    WATCH LEARN-ITEMs from watch/events.jsonl, goal files of achieved lanes; then the join rule (a heard row
    becomes confirmed when a desk answer quotes it) and make_cards(). Idempotent."""
    now = _now(now)
    res = {"added": [], "superseded": [], "confirmed": [], "cards": []}
    answers = _sync_decisions(res, now)
    _sync_watch(res, now)
    _sync_goals(res, now)
    for r in items(status="heard"):
        q = _norm(r.get("quote"))
        for did, n, a in answers:
            if q and q in _norm(a["verbatim"]):
                confirm(r["lid"], {"type": "decisions-log", "ref": did, "line": n}, a["verbatim"], by="sync", now=now)
                res["confirmed"].append(r["lid"])
                break
    res["cards"] = [c["cid"] for c in make_cards(now)]
    return res


# ---------------------------------------------------------------- cards

def _cards():
    return mesh._parse_lines(_p("cards.jsonl"))


def _save_cards(cs):
    _write("cards.jsonl", "".join(json.dumps(c, sort_keys=True) + "\n" for c in cs))


def _label(r):
    return r.get("title") or r.get("what") or "%s %s" % ((r.get("source") or {}).get("type"),
                                                         (r.get("source") or {}).get("ref"))


PHASE3_FORMATS = ("recall", "source", "choice")
ALL_FORMATS = PHASE3_FORMATS + ("explain", "apply")


def _formats(formats):
    if formats is None:
        cfg = _read_json("learn.json", {})
        formats = cfg.get("cardFormats") if isinstance(cfg, dict) else None
    formats = tuple(formats or PHASE3_FORMATS)
    bad = [f for f in formats if f not in ALL_FORMATS]
    if bad:
        raise Refused("unknown card format(s) %s (want %s)" % (",".join(bad), "|".join(ALL_FORMATS)))
    return formats


def make_cards(now=None, formats=None):
    """Cards for confirmed rows that lack them, in `formats` (default learn.json cardFormats, else recall,
    source, choice). choice needs 3 or more other rows; explain needs the row's `what`, apply its `why`.
    Heard rows get no cards: they are never taught as decided. Distractors are other ledger rows' quotes."""
    now = _now(now)
    formats = _formats(formats)
    with _Lock():
        cs = _cards()
        have = {(c.get("lid"), c.get("format")) for c in cs}
        rows = items()
        quotes = {}
        for r in items(include_retired=True):
            quotes.setdefault(r.get("quote"), r["lid"])
        new = []
        for r in rows:
            if r.get("status") != "confirmed":
                continue
            src = (r.get("source") or {}).get("ref")
            grounding = {"quote": r["quote"], "source": src}
            sched = {"due": _iso(now), "fixedStep": 0, "reps": 0, "lapses": 0, "lastReview": None}
            others = sorted({q for q, lid in quotes.items() if q and lid != r["lid"] and _norm(q) != _norm(r["quote"])})
            specs = [("recall", "What did %s say about: %s?" % (src, _label(r)), None, r["quote"]),
                     ("source", "Paste the line from %s that proves: %s" % (src, _label(r)), None, r["quote"])]
            if r.get("what"):
                specs.append(("explain", "Explain in your own words: %s" % _label(r), None, r["what"]))
            if r.get("why"):
                specs.append(("apply", "A lane wants to go against this: %s. What do you say, and why?" % _label(r),
                              None, r["why"]))
            if len(others) >= 3:
                rnd = random.Random(r["lid"])
                opts = rnd.sample(others, 3) + [r["quote"]]
                rnd.shuffle(opts)
                specs.append(("choice", "Which line is the source (%s) for: %s?" % (src, _label(r)), opts,
                              r["quote"]))
            for fmt, prompt, opts, answer in specs:
                if fmt not in formats or (r["lid"], fmt) in have:
                    continue
                c = {"cid": _next_id("C", cs + new, "cid"), "lid": r["lid"], "format": fmt, "origin": r["origin"],
                     "prompt": prompt, "answer": answer, "grounding": grounding, "sched": dict(sched)}
                if opts:
                    c["options"] = opts
                new.append(c)
        if new:
            _save_cards(cs + new)
        return new


def card(cid):
    if not CID_RE.match(cid or ""):
        raise Refused("bad card id %r (want C<n>)" % cid)
    for c in _cards():
        if c.get("cid") == cid:
            return c
    raise Refused("no card %s" % cid)


def _idnum(i):
    """C10 sorts after C2: the numeric part of an L<n>/C<n> id."""
    return int(i[1:]) if isinstance(i, str) and i[1:].isdigit() else -1


def due(now=None):
    now = _now(now)
    live = {r["lid"] for r in items(status="confirmed")}
    out = [c for c in _cards() if not c.get("retired") and c.get("lid") in live
           and (_parse_ts((c.get("sched") or {}).get("due")) or now) <= now]
    out.sort(key=lambda c: ((c.get("sched") or {}).get("due") or "", _idnum(c["cid"])))
    return out


# ---------------------------------------------------------------- review and proof

def check_source(answer, quote):
    """Source check: the pasted line must be a verbatim substring of the quote (same test as Listener._ground),
    and long enough to prove something."""
    a = (answer or "").strip()
    if not a or a not in (quote or ""):
        return False
    return len(a) >= min(SOURCE_MIN, len((quote or "").strip()))


def _choice_pick(c, answer):
    a = (answer or "").strip()
    opts = c.get("options") or []
    if len(a) == 1 and a.lower() in "abcd"[:len(opts)]:
        return opts["abcd".index(a.lower())]
    if a.isdigit() and 1 <= int(a) <= len(opts):
        return opts[int(a) - 1]
    return a


def review(cid, answer, rating=None, now=None, grade=None, gradedBy=None, detail=None, latencyMs=None):
    """Grade one answer, append reviews.jsonl, reschedule the card, refresh proof.json. Returns the review row."""
    now = _now(now)
    c = card(cid)
    if c.get("retired"):
        raise Refused("%s is retired" % cid)
    r = item(c["lid"])
    if r.get("status") != "confirmed":
        raise Refused("%s belongs to %s, which is %s" % (cid, r["lid"], r.get("status")))
    fmt, quote = c.get("format"), (c.get("grounding") or {}).get("quote")
    checked = False
    if fmt == "recall":
        if rating not in RATINGS:
            raise Refused("recall needs --rating again|hard|good|easy")
        g, by = ("pass" if rating in ("good", "easy") else "fail"), "self"
    elif fmt == "choice":
        g, by = ("pass" if _choice_pick(c, answer) == c["answer"] else "fail"), "exact"
    elif fmt == "source":
        checked = True
        g, by = ("pass" if check_source(answer, quote) else "fail"), "verbatim"
    else:
        if grade not in ("pass", "fail"):
            raise Refused("format %s needs an external grade pass|fail" % fmt)
        g, by = grade, gradedBy or "command"
    if rating is None:
        rating = "good" if g == "pass" else "again"
    elif rating not in RATINGS:
        raise Refused("rating must be again|hard|good|easy")
    row = {"cid": cid, "lid": c["lid"], "at": _iso(now), "format": fmt, "answer": answer or "", "grade": g,
           "rating": rating, "gradedBy": by, "latencyMs": latencyMs, "quoteChecked": checked, "origin": c.get("origin")}
    if detail is not None:
        row["detail"] = detail
    sched_fn = scheduler()  # resolved before the append: a scheduler error writes nothing
    with _Lock():
        _append("reviews.jsonl", row)
        cs = _cards()
        for x in cs:
            if x.get("cid") == cid:
                x["sched"] = sched_fn(x.get("sched") or {}, rating, now)
        _save_cards(cs)
    write_proof()
    return row


def reviews(lid=None):
    """reviews.jsonl rows (the record), oldest first; for score history per topic join on lid -> items()."""
    out = mesh._parse_lines(_p("reviews.jsonl"))
    return [r for r in out if lid is None or r.get("lid") == lid]


def rebuild_proof():
    """Per lid from reviews.jsonl alone (due comes from cards.jsonl): learned = a generative pass and a checkable
    pass, the latest pass at least 24 h after first exposure, and no failed review since the last pass."""
    by = {}
    for rv in mesh._parse_lines(_p("reviews.jsonl")):
        lid, at = rv.get("lid"), _parse_ts(rv.get("at"))
        if not lid or not at:
            continue
        by.setdefault(lid, []).append((at, rv))
    dues = {}
    for c in _cards():
        if c.get("retired"):
            continue
        d = (c.get("sched") or {}).get("due")
        if d and (c["lid"] not in dues or d < dues[c["lid"]]):
            dues[c["lid"]] = d
    out = {}
    for lid in sorted(by, key=lambda k: int(k[1:]) if k[1:].isdigit() else 0):
        revs = sorted(by[lid], key=lambda t: t[0])
        first = revs[0][0]
        passes = [{"at": _iso(at), "format": rv.get("format"),
                   "kind": "generative" if rv.get("format") in GENERATIVE else "checkable"}
                  for at, rv in revs if rv.get("grade") == "pass"]
        fails = [at for at, rv in revs if rv.get("grade") != "pass"]
        last_pass = _parse_ts(passes[-1]["at"]) if passes else None
        lapsed = [f for f in fails if last_pass is None or f > last_pass]
        learned = bool(passes) and any(p["kind"] == "generative" for p in passes) \
            and any(p["kind"] == "checkable" for p in passes) \
            and last_pass - first >= SPACING and not lapsed
        out[lid] = {"learned": learned, "firstSeen": _iso(first), "passes": passes,
                    "lapsedAt": _iso(lapsed[-1]) if lapsed else None, "due": dues.get(lid)}
    return out


def write_proof():
    p = rebuild_proof()
    _write("proof.json", json.dumps(p, indent=1, sort_keys=True) + "\n")
    return p


def proof():
    return _read_json("proof.json", {})


# ---------------------------------------------------------------- snapshot and export

def snapshot(now=None):
    """Read-only view for status lines and the phase 7 page. Origins are never blended."""
    now = _now(now)
    pf = rebuild_proof()
    rows = items()
    ds = due(now)
    by_origin = {}
    for o in ORIGINS:
        mine = [r for r in rows if r.get("origin") == o]
        by_origin[o] = {"items": len(mine), "confirmed": sum(1 for r in mine if r["status"] == "confirmed"),
                        "heard": sum(1 for r in mine if r["status"] == "heard"),
                        "learned": sum(1 for r in mine if (pf.get(r["lid"]) or {}).get("learned")),
                        "due": sum(1 for c in ds if c.get("origin") == o)}
    topics = {}
    for r in rows:
        k = (r.get("topic") or "", r.get("origin"))
        t = topics.setdefault(k, {"topic": k[0], "origin": k[1], "items": 0, "learned": 0})
        t["items"] += 1
        t["learned"] += 1 if (pf.get(r["lid"]) or {}).get("learned") else 0
    tl = sorted(topics.values(), key=lambda t: (t["origin"], t["topic"]))
    for t in tl:
        t["pct"] = int(round(100.0 * t["learned"] / t["items"])) if t["items"] else 0
    recent = [{"cid": rv.get("cid"), "lid": rv.get("lid"), "at": rv.get("at"), "grade": rv.get("grade"),
               "format": rv.get("format"), "origin": rv.get("origin")}
              for rv in mesh._parse_lines(_p("reviews.jsonl"))[-20:]]
    return {"at": _iso(now), "dir": learn_dir(), "byOrigin": by_origin, "topics": tl,
            "due": [{"cid": c["cid"], "lid": c["lid"], "format": c["format"], "prompt": c["prompt"],
                     "origin": c.get("origin")} for c in ds],
            "heard": [{"lid": r["lid"], "quote": r["quote"], "source": r.get("source")} for r in rows
                      if r["status"] == "heard"],
            "recent": recent}


def _slug(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-.")
    return (s or "topic")[:60]


def _md_row(r, pf):
    s = r.get("source") or {}
    p = pf.get(r["lid"]) or {}
    lines = ["## %s: %s" % (r["lid"], _label(r)), "",
             "- origin: %s (%s)" % (r.get("origin"), "we decided or shipped it" if r.get("origin") == "fleet"
                                    else "someone else changed it"),
             "- status: %s" % r.get("status"), "- source: %s %s" % (s.get("type"), s.get("ref")),
             "- proof: %s" % ("learned" if p.get("learned") else "%d pass(es), not learned yet" % len(p.get("passes")
                                                                                                    or []))]
    if r.get("what"):
        lines.append("- what: %s" % r["what"])
    if r.get("why"):
        lines.append("- why: %s" % r["why"])
    if r.get("confirmedBy"):
        lines.append("- confirmed by: %s" % ((r["confirmedBy"].get("source") or {}).get("ref")))
    lines += ["", "> " + "\n> ".join(str(r.get("quote", "")).split("\n")), ""]
    return "\n".join(lines)


def export(to=None):
    d = os.path.abspath(os.path.expanduser(to or os.path.join(learn_dir(), "export")))
    if os.environ.get("FLEET_ROLE") == "learn" and not _inside(d, learn_dir()):
        raise Refused("role learn may export only under %s, not %s" % (learn_dir(), d))
    os.makedirs(d, exist_ok=True)
    pf = rebuild_proof()
    groups = {}
    for r in items():
        groups.setdefault(r.get("topic") or "topic", []).append(r)
    written = []
    for topic, rs in sorted(groups.items()):
        head = "# %s\n\n%s\n\n" % (topic, "; ".join(
            "%s: %d of %d learned" % (o, sum(1 for r in rs if r["origin"] == o and (pf.get(r["lid"]) or {})
                                             .get("learned")), sum(1 for r in rs if r["origin"] == o))
            for o in ORIGINS))
        p = os.path.join(d, "%s.md" % _slug(topic))
        mesh._atomic_write(p, head + "\n".join(_md_row(r, pf) for r in rs))
        written.append(p)
    return written


# ---------------------------------------------------------------- quiz TUI

def quiz(inp=None, out=None, max_cards=30, now=None):
    """Deterministic terminal quiz over due cards; reads one line per answer. Returns the review rows."""
    inp, out = inp or sys.stdin, out or sys.stdout
    done = []

    def ask(prompt):
        out.write(prompt)
        out.flush()
        ln = inp.readline()
        if not ln:
            raise EOFError
        return ln.rstrip("\n")

    cards_due = due(now)[:max_cards]
    if not cards_due:
        out.write("nothing due. %s\n" % _status_line(now))
        return done
    out.write("%d card(s) due. Answer, or q to stop.\n" % len(cards_due))
    try:
        for c in cards_due:
            t0 = time.time()
            out.write("\n[%s] %s  (%s, %s)\n%s\n" % (c["cid"], c["lid"], c["format"], c.get("origin"), c["prompt"]))
            if c["format"] == "recall":
                a = ask("answer from memory (Enter to reveal): ")
                if a.strip() == "q":
                    break
                out.write("source quote: %s\n" % c["answer"])
                rt = ask("rate: a=again h=hard g=good e=easy: ").strip().lower()
                if rt == "q":
                    break
                rating = {"a": "again", "h": "hard", "g": "good", "e": "easy"}.get(rt[:1], "again")
                rv = review(c["cid"], a, rating=rating, now=now, latencyMs=int((time.time() - t0) * 1000))
            elif c["format"] == "choice":
                for i, o in enumerate(c.get("options") or []):
                    out.write("  %s) %s\n" % ("abcd"[i], o))
                a = ask("pick a-d: ")
                if a.strip() == "q":
                    break
                rv = review(c["cid"], a, now=now, latencyMs=int((time.time() - t0) * 1000))
            elif c["format"] in ("explain", "apply"):
                try:
                    import fleet_learn_explain as X  # phase 5
                except ImportError:
                    out.write("(skipped: %s cards need lib/fleet_learn_explain.py, phase 5)\n" % c["format"])
                    continue
                rv = X.quiz_card(sys.modules[__name__], c, cfg=X.load_config(), inp=inp, out=out, now=now,
                                 show_prompt=False)
            else:
                a = ask("paste the line: ")
                if a.strip() == "q":
                    break
                rv = review(c["cid"], a, now=now, latencyMs=int((time.time() - t0) * 1000))
            out.write("%s. source: \"%s\"\n" % ("PASS" if rv["grade"] == "pass" else "FAIL",
                                                 (c.get("grounding") or {}).get("quote") or c["answer"]))
            done.append(rv)
    except EOFError:
        pass
    out.write("\n%s\n" % _status_line(now))
    return done


def _status_line(now=None):
    s = snapshot(now)
    return "; ".join("%s: %d of %d learned, %d due, %d heard only" % (
        o, s["byOrigin"][o]["learned"], s["byOrigin"][o]["items"], s["byOrigin"][o]["due"], s["byOrigin"][o]["heard"])
        for o in ORIGINS)


# ---------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("FAIL usage: %s\n" % message)
        sys.exit(2)


def _help():
    return __doc__.split("\n\n", 1)[1]


def _one_line(r):
    s = r.get("source") or {}
    text = (r.get("title") or r.get("quote") or "").replace("\n", " ")
    return "%-5s %-7s %-10s %-22s %s" % (r["lid"], r.get("origin"), r.get("status"),
                                          ("%s:%s" % (s.get("type"), s.get("ref")))[:22],
                                          text[:60] + ("..." if len(text) > 60 else ""))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(_help())
        return 0 if argv else 2
    if argv[0] == "serve":
        try:
            import fleet_learn_serve
        except ImportError:
            sys.stderr.write("FAIL fleet learn serve is not built yet (lib/fleet_learn_serve.py, phase 7)\n")
            return 1
        return fleet_learn_serve.main(argv[1:])
    p = _Parser(prog="fleet learn")
    sp = p.add_subparsers(dest="cmd")
    a = sp.add_parser("add")
    a.add_argument("--kind", required=True)
    a.add_argument("--origin", required=True)
    a.add_argument("--source", required=True)
    a.add_argument("--quote", required=True)
    a.add_argument("--status", default="confirmed")
    for f in ("title", "what", "why", "topic"):
        a.add_argument("--" + f, default="")
    a.add_argument("--tag", action="append", default=[])
    ls = sp.add_parser("list")
    ls.add_argument("--origin")
    ls.add_argument("--status")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")
    sh = sp.add_parser("show")
    sh.add_argument("lid")
    sh.add_argument("--json", action="store_true")
    cf = sp.add_parser("confirm")
    cf.add_argument("lid")
    cf.add_argument("--source", required=True)
    cf.add_argument("--quote", required=True)
    dr = sp.add_parser("drop")
    dr.add_argument("lid")
    dr.add_argument("--reason", required=True)
    sy = sp.add_parser("sync")
    sy.add_argument("--goal")
    sy.add_argument("--lane")
    sy.add_argument("--json", action="store_true")
    du = sp.add_parser("due")
    du.add_argument("--json", action="store_true")
    qz = sp.add_parser("quiz")
    qz.add_argument("--max", type=int, default=30)
    rv = sp.add_parser("review")
    rv.add_argument("cid")
    rv.add_argument("--answer", default="")
    rv.add_argument("--rating")
    pr = sp.add_parser("proof")
    pr.add_argument("--rebuild", action="store_true")
    pr.add_argument("--check", action="store_true")
    sp.add_parser("snapshot")
    ex = sp.add_parser("export")
    ex.add_argument("--to")
    args = p.parse_args(argv)
    try:
        if args.cmd == "add":
            if args.status not in LIVE:
                raise Refused("status must be confirmed or heard, got %r" % args.status)
            r = add_item(args.kind, args.origin, args.quote, parse_source(args.source), title=args.title,
                         what=args.what, why=args.why, status=args.status, tags=args.tag, topic=args.topic or None)
            make_cards()
            print("OK added %s (%s, %s)" % (r["lid"], r["origin"], r["status"]))
        elif args.cmd == "list":
            if args.origin and args.origin not in ORIGINS:
                raise Refused("origin must be fleet or outside")
            rs = items(origin=args.origin, status=args.status, include_retired=args.all)
            if args.json:
                print(json.dumps(rs, indent=1, sort_keys=True))
            else:
                for r in rs:
                    print(_one_line(r))
                print(_status_line())
        elif args.cmd == "show":
            r = item(args.lid)
            cs = [c for c in _cards() if c.get("lid") == args.lid]
            if args.json:
                print(json.dumps(dict(r, history=history(args.lid), cards=cs), indent=1, sort_keys=True))
            else:
                print(_md_row(r, rebuild_proof()).rstrip())
                print("history: %d row(s); cards: %s" % (len(history(args.lid)),
                                                         ", ".join("%s %s" % (c["cid"], c["format"]) for c in cs)
                                                         or "none"))
        elif args.cmd == "confirm":
            confirm(args.lid, parse_source(args.source), args.quote)
            make_cards()
            print("OK confirmed %s (heard -> confirmed, source %s)" % (args.lid, args.source))
        elif args.cmd == "drop":
            drop(args.lid, args.reason)
            print("OK dropped %s" % args.lid)
        elif args.cmd == "sync":
            if args.goal or args.lane:
                if not (args.goal and args.lane):
                    raise Refused("--goal and --lane go together")
                res = {"added": sync_goal(args.goal, args.lane)}
                res["cards"] = [c["cid"] for c in make_cards()]
            else:
                res = sync()
            if args.json:
                print(json.dumps(res, sort_keys=True))
            else:
                dl = res.get("decisionsLog")
                if dl and not dl["present"]:
                    print("decisions log: none yet at %s (it appears with the first answered call)" % dl["path"])
                elif dl:
                    print("decisions log: read %d rows, skipped %d" % (dl["read"], dl["skipped"]))
                print("OK sync: added %d, superseded %d, confirmed %d, new cards %d" % (
                    len(res["added"]), len(res.get("superseded", [])), len(res.get("confirmed", [])),
                    len(res.get("cards", []))))
        elif args.cmd == "due":
            ds = due()
            if args.json:
                print(json.dumps(ds, indent=1, sort_keys=True))
            else:
                for c in ds:
                    print("%-5s %-5s %-7s %-7s %s" % (c["cid"], c["lid"], c["format"], c.get("origin"), c["prompt"]))
                print("%d due" % len(ds))
        elif args.cmd == "quiz":
            quiz(max_cards=args.max)
        elif args.cmd == "review":
            r = review(args.cid, args.answer, rating=args.rating)
            print("OK %s %s (%s)" % (args.cid, r["grade"], r["gradedBy"]))
        elif args.cmd == "proof":
            if args.check:
                same = proof() == rebuild_proof()
                print("OK proof.json matches a rebuild from reviews.jsonl" if same
                      else "FAIL proof.json differs from a rebuild")
                return 0 if same else 1
            pf = write_proof() if args.rebuild else proof()
            print(json.dumps(pf, indent=1, sort_keys=True))
        elif args.cmd == "snapshot":
            print(json.dumps(snapshot(), indent=1, sort_keys=True))
        elif args.cmd == "export":
            for w in export(args.to):
                print("OK wrote %s" % w)
        else:
            sys.stdout.write(_help())
            return 2
    except Refused as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
