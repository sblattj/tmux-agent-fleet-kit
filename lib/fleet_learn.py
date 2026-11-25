#!/usr/bin/env python3
"""fleet learn: the LEARN desk's ledger, cards, terminal quiz and proof of learning. Contract: lib/fleet_learn_contract.md

Python 3.8+ stdlib only. Writes only under $FLEET_STATE/learn/ (export --to DIR by the human aside).

  fleet learn add --kind K --origin fleet|outside --source TYPE:REF --quote TEXT [--status confirmed|heard]
                  [--title T] [--what W] [--why Y] [--topic T] [--tag T ...] [--teach]
                                                   add a ledger row; prints "OK added L<n>". A route-done, goal or
                                                   decisions-log row with no question --title that is fleet plumbing or holds
                                                   no answer prints "SKIP ..." and adds nothing (--teach keeps it)
  fleet learn list [--origin O] [--status S] [--all] [--json]   latest row per lid, counts per origin
  fleet learn show <lid> [--json]                  one row, its history and its cards
  fleet learn confirm <lid> --source TYPE:REF --quote TEXT   heard -> confirmed (needs another source and its quote)
  fleet learn drop <lid> --reason TEXT             retire a row and its cards (e.g. answer not in its quote)
  fleet learn sync [--goal FILE --lane NAME] [--json]   decisions-log.jsonl, WATCH LEARN-ITEMs, achieved goal files,
                                                   then make cards; a missing log or events file is not an error
  fleet learn due [--topic T] [--json]             cards due now (--topic: a ticket, PR, release or topic name)
  fleet learn quiz [--topic T] [--max N]           terminal quiz over due cards (reads answers on stdin; q quits)
  fleet learn review <cid> --answer TEXT [--rating again|hard|good|easy]   grade one answer (the quiz's verb)
  fleet learn proof [--rebuild] [--check]          per-row proof; --check exits 1 if proof.json != a rebuild
  fleet learn snapshot                             snapshot() as JSON
  fleet learn export [--to DIR]                    markdown, one file per topic (default $FLEET_STATE/learn/export)
  fleet learn serve [--port P] [--bind 127.0.0.1] [--open]   the phase 7 page (lib/fleet_learn_serve.py, when present)
  fleet learn lesson [--topic T | --lid L] [--yes] [--json]   the lesson before the quiz: terms, then use in context,
                                                   then why; every line has its quote and source; heard rows show as
                                                   "not decided yet". Enter (or --yes) marks it seen: its cards come
                                                   due and the 24 h proof clock starts (lib/fleet_learn_lesson.py)
  fleet learn session --topic T [--max N] [--yes]  lesson on T, a quiz on only T's cards, then one result line
  fleet learn brief --for TEXT [--max N] [--json]  meeting prep: rows for TEXT, picked by learn.json briefSelector
                                                   (a command) or the built-in fallback; marks nothing seen
  fleet learn digest [--since ISO|24h|7d|last] [--all] [--json]   lessons for rows new to you since then
  fleet learn rebuild [--json]                     re-derive ledger and cards from sources only; old files
                                                   go to learn/archive/<stamp>/; reviews and seen are kept, re-keyed

Teach first: a new row is due as a lesson; its cards come due only after its lesson is seen (learn.json
{"teachFirst": false} turns that off; FLEET_LEARN_TEACH_FIRST=0|1 overrides it, for tests). lesson/session marking, quiz and review refuse a FLEET_ROLE (exit 4).
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
KINDS = ("decision", "change", "closed", "doc", "shipped")  # shipped: a goal done line (was "lesson", Q694)
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


class RoleRefused(Refused):
    """A verb only the human may run was called under a FLEET_ROLE (the CLI exits 4, as `serve` does)."""


def human_only(verb):
    role = os.environ.get("FLEET_ROLE") or ""
    if role:
        raise RoleRefused("role %s can't %s: only the human marks lessons seen and answers cards" % (role, verb))


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


# ---------------------------------------------------------------- row identity across rebuilds (Q694, Q703)
# Every row is derived from a source outside learn/ (decisions-log.jsonl, watch/events.jsonl, goal files, the
# configured sources), so a rebuild re-derives it. Only the human's own record (reviews.jsonl, seen.jsonl) is kept,
# re-keyed onto the new lids by this key (never by lid, which a rebuild re-allocates). There is no inputs journal.


def source_key(r):
    s = r.get("source") or {}
    return "%s|%s|%s" % (s.get("type"), s.get("ref"), _norm(r.get("quote")))


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
        _append("ledger.jsonl", row)
    return row


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


# ---------------------------------------------------------------- relevance: teach work, skip fleet plumbing (#23 #24)
# LEARN teaches what the human needs in meetings: which PR changes what, what was decided and why, which ticket
# tracks it. Rows about the fleet's own operation (lane lifecycle, desk or kit setup, routing, launch or park,
# tooling installs) and rows whose quote answers nothing on its own are skipped by default. Override per line with
# "learn: teach" / "learn: skip" in the quote or done line, or per lane with {"learn": "teach"|"skip"} in its goal.

DESKS = ("ROUTE", "DECIDE", "WATCH", "LEARN", "DECISIONS", "ORCHESTRATOR")
FILTERED_SOURCES = ("decisions-log", "goal", "route-done")  # manual, decide-learn-this and watch rows are not filtered
_NOT_TICKETS = {"UTF", "ISO", "SHA", "RFC", "HTTP", "TLS", "AES", "IPV", "GPT", "PEP", "CVE"}
_TICKET_RE = re.compile(r"\bAB#\d+\b|\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_PR_RE = re.compile(r"\b[\w.-]+/[\w.-]+#\d+\b|(?<![\w&#/])#\d{1,6}\b|(?<![\w!])![0-9]{3,}\b")
_RELEASE_RE = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+(?:-[a-z]+\.?\d*)?(?![.\d])")
PLUMBING_LANE_TOKENS = ("kit", "fleet", "desk", "desks", "tmux", "mesh", "mcp", "launchd", "dotfiles", "zerotier")
_STRONG_RE = re.compile(r"tmux-agent-fleet-kit|\bfleet[ -]kit\b|\bfleet_mesh\b|\blane-env\b|\bdesk[AB]\b"
                        r"|\bdesks? (?:rename|slots?|layout)\b|\bMCP server|\blaunchd\b|\bzerotier\b|\bmosh\b", re.I)
_WEAK_RE = re.compile(r"\b(?:launch|launched|park|parked|reopen|relaunch|retire|cleanup|clean up|refresh|relay|relayed"
                      r"|routing|lanes?|desk|inbox|mesh|install|installed|upgrade|update to latest|ssh|tmux|handoff)\b"
                      r"|/refresh", re.I)
_PROCESS_RE = re.compile(r"^\s*(?:wrote|git|gh|fleet|fleet-goal|bun|npm|pnpm|python3?|bash|ls|jq|cat|pass|fail|ok)\b",
                         re.I)
_BOILER_RE = re.compile(r"^route-done(?: addendum)?[:,]?\s*\S+?[:,]?\s*(?:parked\s*\([^)]*\))?[.:,]?\s*", re.I)
_OVERRIDE_RE = re.compile(r"\blearn:\s*(skip|teach)\b", re.I)
_QID_RE = re.compile(r"^Q\d+$", re.I)
_OPTION_RE = re.compile(r"^\d*-?[a-d]$|^\(?[a-d]\)?$", re.I)
_META_WORDS = {"yours", "your", "recs", "rec", "recommended", "on", "all", "ok", "okay", "yes", "no", "go", "option",
               "answer", "it", "the", "typed", "desk"}
ANSWER_MIN_WORDS = 3


def subjects(text):
    """Work subjects named in text, tickets first, then PRs, then releases: ["PROJ-12", "acme/widget#7", "v1.2.0"].
    Azure Boards AB#<n> ids count as tickets."""
    out = []
    for rx in (_TICKET_RE, _PR_RE, _RELEASE_RE):
        for m in rx.findall(text or ""):
            if rx is _TICKET_RE and m.split("-")[0] in _NOT_TICKETS:
                continue
            if m not in out:
                out.append(m)
    return out


def answer_bearing(quote):
    """False for a quote that answers nothing on its own: bare option letters ("a", "9a", "13-a", "12c"),
    "yours recs on all = a", one- or two-word step names ("issues filed", "stage it")."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'._/#-]*", quote or "")
    kept = [w for w in words if not _QID_RE.match(w) and not _OPTION_RE.match(w) and w.lower() not in _META_WORDS]
    return len(kept) >= ANSWER_MIN_WORDS


def _plumbing_lane(name):
    if not name:
        return False
    if name in DESKS:
        return True
    toks = re.split(r"[^a-z0-9]+", name.lower())
    return any(t in PLUMBING_LANE_TOKENS for t in toks)


def relevance(text, lane=None, asker=None, field=None, title="", lane_override=None):
    """("teach"|"skip", reason) for one candidate row. A line's own "learn: teach|skip" wins, then the lane's goal
    file {"learn": ...}, then the default rules: plumbing lane, desk instruction with no work subject, kit or
    tooling text, lifecycle words with no work subject, a done[] step that is process evidence."""
    m = _OVERRIDE_RE.search(text or "")
    if m:
        return m.group(1).lower(), "learn: %s in the line" % m.group(1).lower()
    if lane_override in ("skip", "teach"):
        return lane_override, "learn: %s in the lane's goal file" % lane_override
    body = _BOILER_RE.sub("", text or "", count=1)
    subj = subjects(body + " " + (title or ""))
    if lane not in DESKS and _plumbing_lane(lane):
        return "skip", "plumbing lane"
    if (asker in DESKS or lane in DESKS) and not subj:
        return "skip", "desk instruction with no work subject"
    if _STRONG_RE.search(body):
        return "skip", "fleet kit or tooling setup"
    if _WEAK_RE.search(body) and not subj:
        return "skip", "fleet lifecycle or routing with no work subject"
    if field == "done" and (not subj or _PROCESS_RE.match(body)):
        return "skip", "process step, not a work fact"
    return "teach", "work"


def question_title(question, lane=None):
    """A desk question as a card prompt, only when it is question-shaped: the question itself if short, else its
    first sentence that ends in "?" with leading "label:" segments stripped. "" when none fits."""
    q = " ".join((question or "").split())
    cands = [q] if len(q) <= 160 else []
    cands += [s for s in re.split(r"(?<=[.?!])\s+", q) if s.endswith("?")][:1]
    for s in cands:
        if not s.endswith("?"):
            continue
        prev = None
        while prev != s:
            prev, s = s, re.sub(r"^[^:?]{1,60}:\s+", "", s)
        s = s[:1].upper() + s[1:]
        if 12 <= len(s) <= 160 and not (lane and lane in s):
            return s
    return ""


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


def _lane_goal_file(name):
    """The goal file a lane was launched with ($FLEET_STATE/launch/<name>.txt names it), or None."""
    if not name or "/" in name or name.startswith("."):
        return None
    try:
        with open(os.path.join(mesh.state_dir(), "launch", "%s.txt" % name)) as f:
            m = re.search(r"(~|/)[^ \"']*\.json", f.read())
    except OSError:
        return None
    p = os.path.expanduser(m.group(0)) if m else None
    return p if p and os.path.exists(p) else None


def _goal_override(g):
    """{"learn": "skip"|"teach"} in a goal file, or "learn: skip|teach" in its goal text."""
    if not isinstance(g, dict):
        return None
    v = g.get("learn")
    if isinstance(v, str) and v.strip().lower() in ("skip", "teach"):
        return v.strip().lower()
    m = _OVERRIDE_RE.search(g.get("goal") or "") if isinstance(g.get("goal"), str) else None
    return m.group(1).lower() if m else None


def _lane_override(name):
    p = _lane_goal_file(name)
    if not p:
        return None
    try:
        with open(p) as f:
            return _goal_override(json.load(f))
    except (OSError, ValueError):
        return None


def _skip(res, why, ref, reason):
    res.setdefault("skipped", {}).setdefault(why, []).append({"ref": ref, "reason": reason})


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
    ordered = sorted(answers.items(), key=lambda kv: kv[1][0])
    # fold: a bare answer ("a", "13-a") goes into the row that holds its full option text (a row that names its
    # id, e.g. 'the human typed "13-a" (desk Q12): (a) ...'); a row that repeats an earlier answer folds into it.
    folded, overrides = {}, {}
    for did, (n, r) in ordered:
        v = r["verbatim"]
        if not answer_bearing(v):
            rx = re.compile(r"(?<![A-Za-z0-9])%s(?![0-9])" % re.escape(did))
            into = next((d2 for d2, (n2, r2) in ordered if d2 != did and rx.search(r2["verbatim"])
                         and answer_bearing(r2["verbatim"])), None)
            if into:
                folded[did] = into
            continue
        nv = _norm(v)
        for d2, (n2, r2) in ordered:
            if d2 == did or n2 >= n:
                continue
            n2v = _norm(r2["verbatim"])
            if len(n2v) >= 20 and nv.startswith(n2v) and answer_bearing(r2["verbatim"]):
                folded[did] = d2
                break
    for did, into in folded.items():
        res.setdefault("folded", []).append([did, into])
    for did, (n, r) in ordered:
        src = {"type": "decisions-log", "ref": did, "path": "$FLEET_STATE/decisions-log.jsonl", "line": n}
        asker = r.get("asker")
        if asker:
            src["asker"] = asker
        v, q = r["verbatim"], qs.get(did, "")
        if did in folded:
            continue
        if not answer_bearing(v):
            _skip(res, "answerless", did, "bare answer with no row holding its option text")
            continue
        if asker not in overrides:
            overrides[asker] = _lane_override(asker)
        verdict, why = relevance(v, lane=asker, asker=asker, title=q, lane_override=overrides[asker])
        if verdict == "skip":
            _skip(res, "plumbing", did, why)
            continue
        links = [{"rel": "decided-by", "type": "desk", "ref": "DECIDE"}]
        if asker:
            links.append({"rel": "part-of", "type": "lane", "ref": asker})
        links += [{"type": "decisions-log", "ref": d} for d, into in sorted(folded.items()) if into == did]
        subj = subjects(q + " " + v)
        old = have.get(("decisions-log", did))
        if old is None:
            row = add_item("decision", "fleet", v, src, title=question_title(q, lane=asker), links=links,
                           topic=subj[0] if subj else (asker or did), by="sync", now=now)
            res["added"].append(row["lid"])
        elif old.get("status") in LIVE and old.get("quote") != v:
            row = supersede(old["lid"], v, src, now=now)
            res["superseded"].append([old["lid"], row["lid"]])
    for n, r in heard:
        ref = "line:%d" % n
        if ("decisions-log", ref) in have:
            continue
        if not answer_bearing(r["verbatim"]):
            _skip(res, "answerless", ref, "bare heard line")
            continue
        if relevance(r["verbatim"])[0] == "skip":
            _skip(res, "plumbing", ref, relevance(r["verbatim"])[1])
            continue
        src = {"type": "decisions-log", "ref": ref, "path": "$FLEET_STATE/decisions-log.jsonl", "line": n,
               "kind": "decided-on-call", "speaker": r.get("asker"), "clock": r.get("clock")}
        subj = subjects(r["verbatim"])
        row = add_item("decision", "fleet", r["verbatim"], src, status="heard",
                       topic=subj[0] if subj else (r.get("from") or "call"), by="sync", now=now)
        res["added"].append(row["lid"])
    return [(did, n, r) for did, (n, r) in ordered]


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


def sync_goal(path, lane, now=None, res=None):
    """done[] and decisions[] of one finished lane's goal file -> fleet rows (shipped / decision). Returns new lids.
    Plumbing lanes, process-evidence steps and answerless lines are skipped (into res["skipped"] when given);
    {"learn": "teach"|"skip"} in the goal file, or "learn: teach|skip" in a line, overrides."""
    try:
        with open(path) as f:
            g = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused("cannot read goal file %s: %s" % (path, e))
    res = res if res is not None else {}
    have, out = _by_source(), []
    lane_ov = _goal_override(g)
    gsubj = subjects(g.get("goal") if isinstance(g.get("goal"), str) else "")
    for field, kind in (("done", "shipped"), ("decisions", "decision")):
        for i, d in enumerate(g.get(field) or []):
            text = _goal_text(d)
            ref = "%s:%s[%d]" % (lane, field, i)
            if not text.strip() or ("goal", ref) in have:
                continue
            verdict, why = relevance(text, lane=lane, field=field, lane_override=lane_ov)
            if verdict == "skip":
                _skip(res, "plumbing", "goal:" + ref, why)
                continue
            if not answer_bearing(text) and not _OVERRIDE_RE.search(text):
                _skip(res, "answerless", "goal:" + ref, "step name with no answer in it")
                continue
            subj = subjects(text) or gsubj
            row = add_item(kind, "fleet", text, {"type": "goal", "ref": ref, "path": path, "lane": lane},
                           title="", topic=subj[0] if subj else lane, by="sync", now=now,
                           links=[{"rel": "ships-in" if kind == "shipped" else "part-of", "type": "lane",
                                   "ref": lane}])
            out.append(row["lid"])
    return out


def _sync_goals(res, now):
    roster = mesh.load_roster()
    cur = _read_json("cursors.json", {})
    done = cur.get("goals") or {}
    for name, e in sorted(roster.items()):
        if not isinstance(e, dict) or e.get("goalState") != "achieved" or done.get(name) == "achieved":
            continue
        gfile = _lane_goal_file(name)
        if gfile:
            res["added"] += sync_goal(gfile, name, now=now, res=res)
            done[name] = "achieved"
    cur = _read_json("cursors.json", {})
    cur["goals"] = done
    _write("cursors.json", json.dumps(cur, indent=1, sort_keys=True) + "\n")


SOURCE_ROUNDS = 50  # bound on drain rounds per sync


def _sync_sources(res, now):
    """The configured sources (sources.json, else watch.json; Q702) through WATCH's adapter registry. A row per
    item ref: a new quote for the same ref supersedes the old row, so the row holds the item's latest state."""
    import fleet_learn_sources as S
    cur = _read_json("cursors.json", {})
    cursors = dict(cur.get("sources") or {})
    rep, eids = None, {}
    for _round in range(SOURCE_ROUNDS):  # drain: a source caps each poll, so poll again until nothing is new
        pairs, r = S.poll_all(cursors)
        if rep is None:
            rep = r
        else:
            rep["failed"].update(r["failed"])
            rep["errors"] = sorted(set(rep["errors"]) | set(r["errors"]))
        for src, evs in pairs:  # polled = distinct events per source over all rounds
            eids.setdefault(src["name"], set()).update(ev.eid for ev in evs)
        rep["polled"] = {n: len(v) for n, v in eids.items()}
        before = len(res["added"]) + len(res["superseded"])
        _sync_pairs(pairs, res, now)
        if len(res["added"]) + len(res["superseded"]) == before or r["failed"] or r["errors"]:
            break
    res["sources"] = rep
    cur = _read_json("cursors.json", {})
    cur["sources"] = cursors
    _write("cursors.json", json.dumps(cur, indent=1, sort_keys=True) + "\n")


def _sync_pairs(pairs, res, now):
    import fleet_learn_sources as S
    for src, evs in pairs:
        for ev in evs:
            kw = S.event_row(src, ev)
            if kw is None:
                continue
            old = _by_source().get((kw["source"]["type"], str(kw["source"]["ref"])))
            if old is not None and old.get("status") in LIVE:
                if _norm(old.get("quote")) != _norm(kw["quote"]):
                    row = supersede(old["lid"], kw["quote"], kw["source"], now=now)
                    _relink(row["lid"], kw["links"], kw["title"])
                    res["superseded"].append([old["lid"], row["lid"]])
                continue
            if old is not None:
                continue  # dropped or superseded by hand-free sync history: not re-added
            row = add_item(kw["kind"], kw["origin"], kw["quote"], kw["source"], title=kw["title"],
                           status=kw["status"], links=kw["links"], tags=kw["tags"], topic=kw["topic"], by="sync",
                           now=now)
            res["added"].append(row["lid"])


def _relink(lid, links, title):
    r = item(lid)
    _append("ledger.jsonl", dict(r, links=list(links or []), title=title or r.get("title") or ""))


# ---------------------------------------------------------------- entities (Q697): derived from row links

def entities():
    """Every link target of the live rows, one entity per (type, ref): {key, type, ref, rows: [{lid, rel}]}.
    Rebuilt from the ledger on every sync, never kept state (Q703); entities.jsonl is its file form."""
    out = {}
    for r in items():
        for ln in r.get("links") or []:
            if not isinstance(ln, dict) or not ln.get("type") or not ln.get("ref"):
                continue
            key = "%s:%s" % (ln["type"], ln["ref"])
            e = out.setdefault(key, {"key": key, "type": str(ln["type"]), "ref": str(ln["ref"]), "rows": []})
            e["rows"].append({"lid": r["lid"], "rel": ln.get("rel") or ""})
    return [out[k] for k in sorted(out)]


def write_entities():
    es = entities()
    _write("entities.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in es))
    return es


def sync(now=None):
    """decisions-log.jsonl (desk answers confirmed, decided-on-call heard; latest answer per id supersedes),
    WATCH LEARN-ITEMs from watch/events.jsonl, goal files of achieved lanes, the configured sources (Q702); then
    the join rule (a heard row becomes confirmed when a desk answer quotes it), make_cards() and entities.jsonl.
    Idempotent."""
    now = _now(now)
    res = {"added": [], "superseded": [], "confirmed": [], "cards": []}
    answers = _sync_decisions(res, now)
    _sync_watch(res, now)
    _sync_goals(res, now)
    _sync_sources(res, now)
    for r in items(status="heard"):
        q = _norm(r.get("quote"))
        for did, n, a in answers:
            if q and q in _norm(a["verbatim"]):
                confirm(r["lid"], {"type": "decisions-log", "ref": did, "line": n}, a["verbatim"], by="sync", now=now)
                res["confirmed"].append(r["lid"])
                break
    res["cards"] = [c["cid"] for c in make_cards(now)]
    res["entities"] = len(write_entities())
    return res


# ---------------------------------------------------------------- cards

def _cards():
    return mesh._parse_lines(_p("cards.jsonl"))


def _save_cards(cs):
    _write("cards.jsonl", "".join(json.dumps(c, sort_keys=True) + "\n" for c in cs))


def _label(r):
    return r.get("title") or r.get("what") or "%s %s" % ((r.get("source") or {}).get("type"),
                                                         (r.get("source") or {}).get("ref"))


_ASK = {"decision": "What was decided about %s?", "change": "What changed in %s?", "closed": "Why was %s closed?",
        "doc": "What changed in the %s doc?", "shipped": "What shipped in %s?"}
_ASK_PLAIN = {"decision": "What was decided here, and why?", "change": "What changed here?",
              "closed": "What was closed here, and why?", "doc": "What changed in this doc?",
              "shipped": "What shipped here?"}


def card_prompt(r):
    """The question a card asks. A row's title is the prompt, verbatim. With no title: a plain question about the
    row's work subject (ticket, PR, release). Never names the source or the lane; those show after grading."""
    t = (r.get("title") or "").strip()
    if t and (r.get("by") != "sync" or t.endswith("?")):
        return t  # a human's --title, verbatim (an old sync-made title that is not a question is not used)
    subj = subjects(r.get("quote") or "") or subjects(r.get("topic") or "")
    kind = r.get("kind") if r.get("kind") in _ASK else "decision"
    return _ASK[kind] % subj[0] if subj else _ASK_PLAIN[kind]


def row_lane(r):
    s = r.get("source") or {}
    if s.get("type") == "route-done":
        return str(s.get("ref") or "")
    if s.get("type") == "goal":
        return s.get("lane") or str(s.get("ref") or "").split(":")[0]
    if s.get("type") == "decisions-log":
        return s.get("asker") or (r.get("topic") if r.get("by") == "sync" and not s.get("kind") else None)
    return None


def _curated(r):
    """A curated card: a question-shaped --title (the human wrote the question to be asked)."""
    return (r.get("title") or "").strip().endswith("?")


def teachable(r):
    """("teach"|"skip", reason) for a ledger row, the same rules sync uses. A row whose title is a question is a
    curated card and is always taught; manual, decide-learn-this and watch rows are not filtered."""
    s = r.get("source") or {}
    if _curated(r):
        return "teach", "curated"
    if s.get("type") not in FILTERED_SOURCES:
        return "teach", "unfiltered source"
    if s.get("kind") == "decided-on-call":
        return ("teach", "heard") if answer_bearing(r.get("quote")) else ("skip", "answerless")
    lane = row_lane(r)
    field = "done" if s.get("type") == "goal" and ":done[" in str(s.get("ref")) else None
    v = relevance(r.get("quote") or "", lane=lane, asker=lane if s.get("type") == "decisions-log" else None,
                  field=field)
    if v[0] == "skip":
        return v
    if not answer_bearing(r.get("quote")) and not _OVERRIDE_RE.search(r.get("quote") or ""):
        return "skip", "answerless"
    return v


def topic_of(r):
    return (r.get("topic") or "").strip()


def _row_subjects(r):
    return {x.lower() for x in subjects(" ".join([r.get("title") or "", r.get("quote") or "", topic_of(r)]))}


def same_topic(a, b):
    """Two rows share a topic: the same topic (case-insensitive) or a shared work subject."""
    ta, tb = topic_of(a).lower(), topic_of(b).lower()
    return bool(ta and ta == tb) or bool(_row_subjects(a) & _row_subjects(b))


def on_topic(r, topic):
    """`fleet learn quiz --topic T`: the row's topic is or contains T, or the row names T as a work subject."""
    t = (topic or "").strip().lower()
    return bool(t) and (t in topic_of(r).lower() or t in _row_subjects(r))


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


def _choice_opts(r, pool):
    rnd = random.Random(r["lid"])
    opts = rnd.sample(pool, 3) + [r["quote"]]
    rnd.shuffle(opts)
    return opts


def make_cards(now=None, formats=None):
    """Cards for confirmed rows that lack them, in `formats` (default learn.json cardFormats, else recall,
    source, choice). choice needs 3 or more other rows on the same topic; explain needs the row's `what`, apply
    its `why`. Heard rows get no cards: they are never taught as decided. Distractors are other live rows' quotes
    on the same topic. Live cards are refreshed in place (cid and schedule kept): the prompt follows card_prompt(),
    a choice card whose distractors left the topic is redrawn or retired, and a row that is not teachable
    (plumbing, answerless) has its cards retired."""
    now = _now(now)
    formats = _formats(formats)
    with _Lock():
        cs = _cards()
        rows = items()
        by_lid = {r["lid"]: r for r in rows}
        teach = {r["lid"]: teachable(r)[0] == "teach" for r in rows}
        pools = {}
        for r in rows:
            if r.get("status") != "confirmed":
                continue
            pools[r["lid"]] = sorted({o["quote"] for o in rows if o["lid"] != r["lid"] and o.get("quote")
                                      and _norm(o["quote"]) != _norm(r["quote"]) and same_topic(r, o)})
        changed = False
        for c in cs:
            r = by_lid.get(c.get("lid"))
            if c.get("retired") or r is None or r.get("status") != "confirmed":
                continue
            if not teach.get(r["lid"]):
                c["retired"] = True
                changed = True
                continue
            want = _card_prompt_for(c.get("format"), r)
            if want and c.get("prompt") != want:
                c["prompt"] = want
                changed = True
            if c.get("format") == "choice":
                pool = set(pools.get(r["lid"]) or [])
                ds = [o for o in c.get("options") or [] if o != c.get("answer")]
                if not all(o in pool for o in ds):
                    if len(pool) >= 3:
                        c["options"] = _choice_opts(r, sorted(pool))
                    else:
                        c["retired"] = True
                    changed = True
        have = {(c.get("lid"), c.get("format")) for c in cs}
        new = []
        for r in rows:
            if r.get("status") != "confirmed" or not teach.get(r["lid"]):
                continue
            src = (r.get("source") or {}).get("ref")
            grounding = {"quote": r["quote"], "source": src}
            sched = {"due": _iso(now), "fixedStep": 0, "reps": 0, "lapses": 0, "lastReview": None}
            others = pools.get(r["lid"]) or []
            specs = [("recall", None, r["quote"]), ("source", None, r["quote"])]
            if r.get("what"):
                specs.append(("explain", None, r["what"]))
            if r.get("why"):
                specs.append(("apply", None, r["why"]))
            if len(others) >= 3:
                specs.append(("choice", _choice_opts(r, others), r["quote"]))
            for fmt, opts, answer in specs:
                if fmt not in formats or (r["lid"], fmt) in have:
                    continue
                c = {"cid": _next_id("C", cs + new, "cid"), "lid": r["lid"], "format": fmt, "origin": r["origin"],
                     "prompt": _card_prompt_for(fmt, r), "answer": answer, "grounding": grounding,
                     "sched": dict(sched)}
                if opts:
                    c["options"] = opts
                new.append(c)
        if new or changed:
            _save_cards(cs + new)
        return new


def _card_prompt_for(fmt, r):
    q = card_prompt(r)
    if fmt == "explain":
        return "In your own words: %s" % q
    if fmt == "apply":
        return "Someone wants to go against this (%s). What do you say, and why?" % q.rstrip("?")
    return q


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


# ---------------------------------------------------------------- lessons before cards (seen.jsonl)
# A new row is due as a lesson first; its cards come due only once the human has seen that lesson while the row was
# confirmed. seen.jsonl is append-only and, like reviews.jsonl, is the human's own record: rebuild() keeps it.
# learn.json {"teachFirst": false} turns the gate off (cards due at creation, the phase 3 behaviour).

def teach_first():
    env = os.environ.get("FLEET_LEARN_TEACH_FIRST")
    if env in ("0", "1"):
        return env == "1"
    cfg = _read_json("learn.json", {})
    v = cfg.get("teachFirst") if isinstance(cfg, dict) else None
    return v is not False


def seen_rows(lid=None):
    return [r for r in mesh._parse_lines(_p("seen.jsonl")) if lid is None or r.get("lid") == lid]


def exposed():
    """lids whose lesson the human saw while the row was confirmed: their cards may come due."""
    return {r.get("lid") for r in seen_rows() if r.get("status") == "confirmed"}


def _seen_as():
    out = {}
    for r in seen_rows():
        out.setdefault(r.get("lid"), set()).add(r.get("status"))
    return out


def lessons_due(now=None, topic=None):
    """Live, teachable rows whose lesson is due: never seen as their current status (a heard row seen as "not
    decided yet" and later confirmed is due again, as decided). Oldest first."""
    seen = _seen_as()
    out = [r for r in items() if r.get("status") not in seen.get(r["lid"], set())
           and teachable(r)[0] == "teach" and (topic is None or on_topic(r, topic))]
    out.sort(key=lambda r: _idnum(r["lid"]))
    return out


def mark_seen(lids, via="lesson", topic=None, now=None):
    """Record that the human saw these rows' lessons (human only: pane, page and digest all call this). A row
    already seen as its current status is not recorded again. Returns the lids newly recorded."""
    human_only("mark a lesson seen")
    if via not in ("lesson", "session", "digest", "page"):
        raise Refused("via must be lesson, session, digest or page")
    now = _now(now)
    have = _seen_as()
    out = []
    with _Lock():
        for lid in lids:
            r = item(lid)
            if r.get("status") not in LIVE:
                raise Refused("%s is %s; only a live row's lesson can be seen" % (lid, r.get("status")))
            if r.get("status") in have.get(lid, set()) or lid in out:
                continue
            _append("seen.jsonl", {"lid": lid, "at": _iso(now), "status": r["status"], "via": via,
                                   "topic": topic or topic_of(r), "key": source_key(r)})
            out.append(lid)
    if out:
        write_proof()
    return out


def due(now=None, topic=None):
    """Cards due now, oldest due first; topic narrows to rows on_topic() (a ticket, PR, release or topic name).
    With teachFirst (the default) only cards whose row's lesson was seen come due."""
    now = _now(now)
    live = {r["lid"] for r in items(status="confirmed") if topic is None or on_topic(r, topic)}
    if teach_first():
        live &= exposed()
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
    """Grade one answer, append reviews.jsonl, reschedule the card, refresh proof.json. Returns the review row.
    Human only: a lane answering cards would fake proof of learning."""
    human_only("answer a LEARN card")
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
    row = {"cid": cid, "lid": c["lid"], "key": source_key(r), "at": _iso(now), "format": fmt, "answer": answer or "", "grade": g,
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
    """Per lid from reviews.jsonl (due comes from cards.jsonl): learned = a generative pass and a checkable pass,
    the latest pass at least 24 h after first exposure, and no failed review since the last pass. First exposure
    is the earlier of the first review and the first time the lesson was seen as confirmed (seen.jsonl)."""
    by = {}
    for rv in mesh._parse_lines(_p("reviews.jsonl")):
        lid, at = rv.get("lid"), _parse_ts(rv.get("at"))
        if not lid or not at:
            continue
        by.setdefault(lid, []).append((at, rv))
    seen_at = {}
    for sr in seen_rows():
        if sr.get("status") == "confirmed":
            seen_at.setdefault(sr.get("lid"), []).append(_parse_ts(sr.get("at")))
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
        first = min([revs[0][0]] + [t for t in seen_at.get(lid, []) if t])
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
            "lessonsDue": [{"lid": r["lid"], "topic": topic_of(r), "origin": r.get("origin"),
                            "status": r.get("status")} for r in (lessons_due(now) if teach_first() else [])],
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


# ---------------------------------------------------------------- rebuild from sources (Q694)

DERIVED_FILES = ("ledger.jsonl", "cards.jsonl", "proof.json", "cursors.json", "entities.jsonl")
HUMAN_FILES = ("reviews.jsonl", "seen.jsonl")


def rebuild(now=None):
    """Re-derive everything LEARN holds from its sources: decisions-log.jsonl, WATCH events, achieved lanes' goal
    files and the configured sources (Q703: nothing else; there is no inputs journal, so a hand-added row such as a
    route-done line does not survive). The old files are copied to archive/<stamp>/ first, never deleted. The
    human's own record (reviews.jsonl, seen.jsonl) is kept and re-keyed onto the new ids by each row's source and
    quote; a review or seen row whose row no longer derives is left out of the live file (it stays
    in the archive) and counted. Card schedules are replayed from the kept reviews. Returns a report."""
    now = _now(now)
    import shutil
    stamp = _iso(now).replace(":", "").replace("-", "")
    arch = os.path.join(learn_dir(), "archive", stamp)
    n = 1
    while os.path.exists(arch):
        n += 1
        arch = os.path.join(learn_dir(), "archive", "%s-%d" % (stamp, n))
    _guard(os.path.join(arch, "x"))
    os.makedirs(arch)
    old_rows = _rows()
    old_cards = _cards()
    old_reviews = mesh._parse_lines(_p("reviews.jsonl"))
    old_seen = seen_rows()
    for name in DERIVED_FILES + HUMAN_FILES + ("inputs.jsonl",):
        if os.path.exists(_p(name)):
            shutil.copy2(_p(name), os.path.join(arch, name))
    keep = _read_json("cursors.json", {})
    old_key = {}
    for r in old_rows:
        if isinstance(r.get("lid"), str):
            old_key[r["lid"]] = source_key(r)
    old_card = {c.get("cid"): (c.get("lid"), c.get("format")) for c in old_cards}
    for name in DERIVED_FILES + HUMAN_FILES:
        if os.path.exists(_p(name)):
            os.remove(_guard(_p(name)))
    if isinstance(keep, dict) and keep.get("digest"):
        _write("cursors.json", json.dumps({"digest": keep["digest"]}, indent=1, sort_keys=True) + "\n")
    if os.path.exists(_p("inputs.jsonl")):  # an older branch's journal: archived above, never read (Q703)
        os.remove(_guard(_p("inputs.jsonl")))
    res = sync(now)
    src = res.get("sources") or {}
    if src.get("errors") or src.get("failed"):
        # fail closed: a source that did not answer would leave the human's reviews and seen rows unmatched
        for name in DERIVED_FILES + HUMAN_FILES + ("inputs.jsonl",):
            a = os.path.join(arch, name)
            if os.path.exists(a):
                shutil.copy2(a, _guard(_p(name)))
            elif os.path.exists(_p(name)):
                os.remove(_guard(_p(name)))
        bad = sorted(src.get("failed") or {}) or ["config"]
        raise Refused("rebuild stopped, nothing changed (archive %s): source(s) failed: %s"
                      % (arch, ", ".join(bad)))
    make_cards(now)
    rows = items()
    new_lid = {source_key(r): r["lid"] for r in rows}
    new_card = {(c.get("lid"), c.get("format")): c["cid"] for c in _cards() if not c.get("retired")}
    kept_rv, lost_rv = [], 0
    for rv in old_reviews:
        key = rv.get("key") or old_key.get(rv.get("lid"))
        lid = new_lid.get(key)
        fmt = old_card.get(rv.get("cid"), (None, rv.get("format")))[1] or rv.get("format")
        cid = new_card.get((lid, fmt))
        if not lid or not cid:
            lost_rv += 1
            continue
        kept_rv.append(dict(rv, lid=lid, cid=cid, key=key))
    kept_seen, lost_seen = [], 0
    for sr in old_seen:
        key = sr.get("key") or old_key.get(sr.get("lid"))
        lid = new_lid.get(key)
        if not lid:
            lost_seen += 1
            continue
        kept_seen.append(dict(sr, lid=lid, key=key))
    _write("reviews.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept_rv))
    _write("seen.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept_seen))
    if kept_rv:
        sched_fn = scheduler()
        with _Lock():
            cs = _cards()
            by_cid = {}
            for rv in sorted(kept_rv, key=lambda r: r.get("at") or ""):
                by_cid.setdefault(rv["cid"], []).append(rv)
            for c in cs:
                for rv in by_cid.get(c["cid"], []):
                    c["sched"] = sched_fn(c.get("sched") or {}, rv.get("rating") or "again",
                                          _parse_ts(rv.get("at")) or now)
            _save_cards(cs)
    write_proof()
    return {"archive": arch, "rows": len(rows), "cards": len([c for c in _cards() if not c.get("retired")]),
            "synced": len(res.get("added") or []),
            "reviewsKept": len(kept_rv), "reviewsLeftOut": lost_rv, "seenKept": len(kept_seen),
            "seenLeftOut": lost_seen}


# ---------------------------------------------------------------- quiz TUI

def _where(c):
    """Where a card's quote came from, shown only after grading: source type:ref (the lane for route-done/goal)."""
    try:
        s = item(c["lid"]).get("source") or {}
    except Refused:
        return ""
    return "%s:%s" % (s.get("type"), s.get("ref"))


def quiz(inp=None, out=None, max_cards=30, now=None, topic=None):
    """Deterministic terminal quiz over due cards; reads one line per answer. Returns the review rows.
    The prompt never names the source; the source quote and where it came from show after grading."""
    human_only("take a LEARN quiz")
    inp, out = inp or sys.stdin, out or sys.stdout
    done = []

    def ask(prompt):
        out.write(prompt)
        out.flush()
        ln = inp.readline()
        if not ln:
            raise EOFError
        return ln.rstrip("\n")

    cards_due = due(now, topic=topic)[:max_cards]
    if not cards_due:
        out.write("nothing due%s. %s\n" % (" on topic %s" % topic if topic else "", _status_line(now)))
        ld = lessons_due(now, topic=topic) if teach_first() else []
        if ld:
            out.write("%d lesson(s) to read first: fleet learn lesson%s\n" % (
                len(ld), " --topic %s" % topic if topic else ""))
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
                out.write("source quote: %s  [from %s]\n" % (c["answer"], _where(c)))
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
            out.write("%s. source: \"%s\"  [from %s]\n" % ("PASS" if rv["grade"] == "pass" else "FAIL",
                                                           (c.get("grounding") or {}).get("quote") or c["answer"],
                                                           _where(c)))
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
    if argv[0] in ("lesson", "session", "brief", "digest"):
        import fleet_learn_lesson
        return fleet_learn_lesson.main(argv)
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
    a.add_argument("--teach", action="store_true")
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
    du.add_argument("--topic")
    qz = sp.add_parser("quiz")
    qz.add_argument("--max", type=int, default=30)
    qz.add_argument("--topic")
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
    rb = sp.add_parser("rebuild")
    rb.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.cmd == "add":
            if args.status not in LIVE:
                raise Refused("status must be confirmed or heard, got %r" % args.status)
            src = parse_source(args.source)
            cand = {"kind": args.kind, "quote": args.quote, "title": args.title, "source": src,
                    "topic": args.topic, "by": "manual"}
            verdict, why = teachable(cand) if args.quote.strip() else ("teach", "")
            if verdict == "skip" and not args.teach:
                print("SKIP %s (%s): not added; pass --teach, a question --title, or 'learn: teach' in the quote"
                      % (args.source, why))
                return 0
            subj = subjects(args.quote)
            r = add_item(args.kind, args.origin, args.quote, src, title=args.title, what=args.what, why=args.why,
                         status=args.status, tags=args.tag,
                         topic=args.topic or (subj[0] if subj and src.get("type") in FILTERED_SOURCES else None))
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
                res = {"added": []}
                res["added"] = sync_goal(args.goal, args.lane, res=res)
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
                sk = res.get("skipped") or {}
                print("OK sync: added %d, superseded %d, confirmed %d, new cards %d" % (
                    len(res["added"]), len(res.get("superseded", [])), len(res.get("confirmed", [])),
                    len(res.get("cards", []))))
                if sk or res.get("folded"):
                    print("skipped: plumbing %d, answerless %d; folded %d bare answer(s) into their full rows" % (
                        len(sk.get("plumbing") or []), len(sk.get("answerless") or []), len(res.get("folded") or [])))
        elif args.cmd == "due":
            ds = due(topic=args.topic)
            if args.json:
                print(json.dumps(ds, indent=1, sort_keys=True))
            else:
                for c in ds:
                    print("%-5s %-5s %-7s %-7s %s" % (c["cid"], c["lid"], c["format"], c.get("origin"), c["prompt"]))
                print("%d due" % len(ds))
        elif args.cmd == "quiz":
            quiz(max_cards=args.max, topic=args.topic)
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
        elif args.cmd == "rebuild":
            res = rebuild()
            if args.json:
                print(json.dumps(res, indent=1, sort_keys=True))
            else:
                print("OK rebuild: %d row(s), %d card(s) from sources; reviews %d kept, "
                      "%d left out; seen %d kept, %d left out; old files in %s" % (
                          res["rows"], res["cards"],
                          res["reviewsKept"], res["reviewsLeftOut"], res["seenKept"], res["seenLeftOut"],
                          res["archive"]))
        elif args.cmd == "export":
            for w in export(args.to):
                print("OK wrote %s" % w)
        else:
            sys.stdout.write(_help())
            return 2
    except RoleRefused as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 4
    except Refused as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
