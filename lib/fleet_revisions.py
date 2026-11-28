#!/usr/bin/env python3
"""Versioned revisions of a decision call (issue #60).

A revision never overwrites: it appends {rev, at, by, why, source, old, new, changed} to the call's `revisions[]` and
bumps `rev` (a call with no `rev` is rev 1). The same functions serve the mesh row in decisions.jsonl and the desk
goal-file item, so `decision_revise` (MCP), `desk.sh revise` and the DECISIONS UI end in the same state.

Guards:
  - a revision needs a reason (`why`) and must change something;
  - only an open call can be revised (an answered call gets a new call);
  - the question may be reworded, not replaced: low word overlap with the rev 1 question means a new topic, which
    is a new call (TopicChanged). Options-only revisions skip this check;
  - amend (adding to an answer) refuses an open call: see amend_allowed().

An answer records the rev it was given against (`answeredRev`); stale_note() words the "answered rev 1, now rev 2"
warning the relay carries. count_by_lane_day() is LEARN's per-asker-lane signal (a signal, never a block).

Python 3.8+ stdlib only.

  python3 lib/fleet_revisions.py count [--days N] [--json]   revisions per asker lane per day, from decisions.jsonl
                                                             and the desk goal file
"""
import collections
import datetime
import difflib
import json
import os
import re
import sys

REVISABLE = ("question", "options", "claims")  # fields a revision may change; claims arrive with #61
TOPIC_MIN = 0.34  # overlap coefficient of content words with the rev 1 question; below it is a new topic

_STOP = set("""a an the and or but if then else of to in on at by for from with without into onto over under is are
was were be been being do does did done have has had it its this that these those we you they i he she them us our
your their my me not no yes so as than too very can could should would will shall may might must just also only
still new what which who whom whose why how when where there here all any each some such own same other
more most less least one two""".split())
_WORD = re.compile(r"[a-z0-9]+")


class Refused(ValueError):
    """A revision the guards refuse (no reason, nothing changed, not open, unknown field)."""


class TopicChanged(Refused):
    """The new question is a different topic from rev 1: open a new call instead."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- topic guard

def content_words(text):
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 1 and w not in _STOP}


def topic_overlap(a, b):
    """Overlap coefficient |A & B| / min(|A|, |B|) over content words; 1.0 when either side has none."""
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 1.0
    return len(wa & wb) / float(min(len(wa), len(wb)))


def same_topic(original, new, threshold=TOPIC_MIN):
    return topic_overlap(original, new) >= threshold


# ---------------------------------------------------------------- reading a row

def current_rev(row):
    try:
        return max(1, int(row.get("rev") or 1))
    except (TypeError, ValueError):
        return 1


def original(row, field="question"):
    """The rev 1 value of `field`: the first revision's old value that holds it, else the current value."""
    for r in row.get("revisions") or []:
        old = r.get("old") or {}
        if field in old:
            return old[field]
    return row.get(field)


def _opt_letter(o, i):
    if isinstance(o, dict) and o.get("letter"):
        return str(o["letter"])
    return chr(ord("a") + i)


def _opt_text(o):
    if isinstance(o, dict):
        return str(o.get("text") or "")
    return str(o if o is not None else "")


def _claim_text(c):
    if isinstance(c, dict):
        return str(c.get("text") or "")
    return str(c if c is not None else "")


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def changed_fields(old, new):
    """['question', 'option a', 'claim 1', ...]: what one revision changed, in display words."""
    out = []
    for f in sorted(set(old) | set(new), key=lambda k: (REVISABLE + (k,)).index(k)):
        o, n = old.get(f), new.get(f)
        if _same(o, n):
            continue
        if f == "options":
            o, n = list(o or []), list(n or [])
            for i in range(max(len(o), len(n))):
                a = o[i] if i < len(o) else None
                b = n[i] if i < len(n) else None
                if not _same(a, b):
                    out.append("option %s" % _opt_letter(b if b is not None else a, i))
        elif f == "claims":
            o, n = list(o or []), list(n or [])
            for i in range(max(len(o), len(n))):
                a = o[i] if i < len(o) else None
                b = n[i] if i < len(n) else None
                if not _same(a, b):
                    out.append("claim %d" % (i + 1))
        else:
            out.append(f)
    return out


def word_diff(a, b):
    """[[op, text], ...] with op '=' kept, '-' removed, '+' added; runs of one op are merged."""
    wa, wb = (a or "").split(), (b or "").split()
    out = []

    def put(op, words):
        if not words:
            return
        if out and out[-1][0] == op:
            out[-1][1] += " " + " ".join(words)
        else:
            out.append([op, " ".join(words)])

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag == "equal":
            put("=", wa[i1:i2])
        else:
            put("-", wa[i1:i2])
            put("+", wb[j1:j2])
    return out


# ---------------------------------------------------------------- applying a revision

def apply_revision(row, changes, why, by, source=None, at=None):
    """Revise `row` in place. Returns the appended revisions[] entry. Raises Refused / TopicChanged."""
    why = (why or "").strip()
    if not why:
        raise Refused("a revision needs a reason (why)")
    status = row.get("status") or "open"
    if status != "open":
        raise Refused("%s is %s: revise only an open call; open a new call instead" % (row.get("id"), status))
    bad = [k for k in changes if k not in REVISABLE]
    if bad:
        raise Refused("can't revise %s (revisable: %s)" % (", ".join(sorted(bad)), ", ".join(REVISABLE)))
    new = {}
    for k, v in changes.items():
        if v is None:
            continue
        if k == "question":
            v = str(v).strip()
            if not v:
                raise Refused("the question can't be empty")
        elif not isinstance(v, list):
            raise Refused("`%s` must be a list" % k)
        if not _same(row.get(k), v):
            new[k] = v
    if not new:
        raise Refused("%s: nothing changed" % row.get("id"))
    if "question" in new:
        first = original(row, "question") or ""
        if not same_topic(first, new["question"]):
            raise TopicChanged("%s: the new question shares too few words with rev 1 (%.2f < %.2f): that is a new "
                               "topic, open a new call" % (row.get("id"), topic_overlap(first, new["question"]),
                                                            TOPIC_MIN))
    old = {k: row.get(k) for k in new}
    rev = current_rev(row) + 1
    entry = {"rev": rev, "at": at or now(), "by": by or "?", "why": why, "old": old, "new": new,
             "changed": changed_fields(old, new)}
    if source:
        entry["source"] = str(source)
    row.update(new)
    row["rev"] = rev
    row["revisions"] = list(row.get("revisions") or []) + [entry]
    return entry


def amend_allowed(item):
    """Amend adds to an answer already given, so it refuses an open call (that is a revise, or an answer)."""
    status = (item or {}).get("status") or "open"
    if status == "open":
        raise Refused("%s is open: amend adds to an answer already given; use revise or answer" % item.get("id"))
    return True


# ---------------------------------------------------------------- views

def summary(row):
    """'rev 2, changed: question, option a' for the latest revision; '' at rev 1."""
    revs = row.get("revisions") or []
    if current_rev(row) <= 1 or not revs:
        return ""
    last = revs[-1]
    ch = last.get("changed") or changed_fields(last.get("old") or {}, last.get("new") or {})
    return "rev %d, changed: %s" % (current_rev(row), ", ".join(ch) or "nothing")


def card_view(row):
    """The fields a card needs: rev, revSummary, why/by/at/source of the latest revision and its word diffs."""
    out = {"rev": current_rev(row), "revSummary": summary(row), "changed": [], "diff": {}, "revision": None}
    revs = row.get("revisions") or []
    if not revs:
        return out
    last = revs[-1]
    old, new = last.get("old") or {}, last.get("new") or {}
    diff = {}
    if "question" in new:
        diff["question"] = word_diff(old.get("question"), new.get("question"))
    if "options" in new:
        o, n = list(old.get("options") or []), list(new.get("options") or [])
        rows = []
        for i in range(max(len(o), len(n))):
            a = o[i] if i < len(o) else None
            b = n[i] if i < len(n) else None
            if not _same(a, b):
                rows.append({"letter": _opt_letter(b if b is not None else a, i),
                             "diff": word_diff(_opt_text(a) if a is not None else "", _opt_text(b) if b is not None
                                               else "")})
        diff["options"] = rows
    if "claims" in new:
        o, n = list(old.get("claims") or []), list(new.get("claims") or [])
        diff["claims"] = [{"n": i + 1, "diff": word_diff(_claim_text(o[i]) if i < len(o) else "",
                                                          _claim_text(n[i]) if i < len(n) else "")}
                          for i in range(max(len(o), len(n)))
                          if not _same(o[i] if i < len(o) else None, n[i] if i < len(n) else None)]
    out["changed"] = list(last.get("changed") or [])
    out["diff"] = diff
    out["revision"] = {"rev": last.get("rev"), "at": last.get("at"), "by": last.get("by"), "why": last.get("why"),
                       "source": last.get("source"), "changed": last.get("changed") or [], "diff": diff}
    return out


def stale_note(answered_rev, row):
    """'answered rev 1, now rev 2' when the answer was given against an older rev, else ''."""
    cur = current_rev(row)
    try:
        a = int(answered_rev)
    except (TypeError, ValueError):
        return ""
    if a < cur:
        return "answered rev %d, now rev %d" % (a, cur)
    return ""


def answered_rev(given, row):
    """The rev an answer was given against: the one the front end showed, else the current rev."""
    try:
        g = int(given)
        if g >= 1:
            return g
    except (TypeError, ValueError):
        pass
    return current_rev(row)


# ---------------------------------------------------------------- LEARN: revisions per asker lane per day

def count_by_lane_day(rows, since=None):
    """{(asker, 'YYYY-MM-DD'): n} over every revisions[] entry, keyed by the call's asker (`from`), not the
    reviser. Rows with the same id count once (pass mesh rows first: they win)."""
    seen, out = set(), collections.Counter()
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if rid in seen:
            continue
        if rid:
            seen.add(rid)
        asker = r.get("from") or "?"
        for e in r.get("revisions") or []:
            day = str(e.get("at") or "")[:10]
            if not day or (since and day < since):
                continue
            out[(asker, day)] += 1
    return dict(out)


def lane_signal(counts):
    """Per lane: days, total, usual (median per active day) and the days above usual (n > 2 x usual and n >= 3).
    A coaching signal, never a block."""
    by = collections.defaultdict(dict)
    for (lane, day), n in counts.items():
        by[lane][day] = n
    out = []
    for lane in sorted(by):
        days = by[lane]
        ns = sorted(days.values())
        mid = len(ns) // 2
        usual = ns[mid] if len(ns) % 2 else (ns[mid - 1] + ns[mid]) / 2.0
        high = [{"day": d, "n": n} for d, n in sorted(days.items()) if n >= 3 and n > 2 * usual]
        if len(ns) == 1 and ns[0] >= 3:  # one active day: nothing to compare with, the floor alone flags it
            high = [{"day": d, "n": n} for d, n in days.items()]
        out.append({"lane": lane, "days": [{"day": d, "n": n} for d, n in sorted(days.items())],
                    "total": sum(ns), "usual": usual, "above": high})
    return out


def _state_dir():
    return os.environ.get("FLEET_STATE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), "fleet")


def _jsonl(path):
    out = []
    try:
        with open(path) as f:
            for ln in f:
                if ln.strip():
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _goal_items(path):
    try:
        with open(path) as f:
            g = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(g, dict):
        return []
    for k in ("openCalls", "openForHuman"):
        if k in g:
            return list(g.get(k) or [])
    return []


def all_rows(state=None, goal=None):
    state = state or _state_dir()
    goal = goal or os.environ.get("FLEET_DESK_GOAL") or os.path.join(state, "desk.goal.json")
    return _jsonl(os.path.join(state, "decisions.jsonl")) + _goal_items(goal)


def report(days=7, state=None, goal=None, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    since = (today - datetime.timedelta(days=max(0, int(days)) - 1)).isoformat() if days else None
    return {"since": since, "lanes": lane_signal(count_by_lane_day(all_rows(state, goal), since=since))}


def format_report(rep):
    lanes = rep.get("lanes") or []
    if not lanes:
        return "revisions: none%s" % (" since %s" % rep["since"] if rep.get("since") else "")
    lines = ["revisions per asker lane per day%s (a signal, not a block):" % (
        " since %s" % rep["since"] if rep.get("since") else "")]
    for ln in lanes:
        days = ", ".join("%s %d" % (d["day"], d["n"]) for d in ln["days"])
        flag = ""
        if ln["above"]:
            flag = "  ABOVE USUAL on " + ", ".join("%s (%d)" % (d["day"], d["n"]) for d in ln["above"])
        lines.append("  %s: %d total, usual %g/day [%s]%s" % (ln["lane"], ln["total"], ln["usual"], days, flag))
    return "\n".join(lines)


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip().split("\n\n")[-1])
        return 0
    if argv[0] == "count":
        days, as_json, i = 7, False, 1
        while i < len(argv):
            if argv[i] == "--days" and i + 1 < len(argv):
                days = int(argv[i + 1])
                i += 2
            elif argv[i] == "--json":
                as_json = True
                i += 1
            else:
                print("usage: count [--days N] [--json]", file=sys.stderr)
                return 2
        rep = report(days)
        print(json.dumps(rep, indent=1, sort_keys=True) if as_json else format_report(rep))
        return 0
    print("unknown command %s" % argv[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
