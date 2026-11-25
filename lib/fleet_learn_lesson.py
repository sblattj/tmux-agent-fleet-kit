#!/usr/bin/env python3
"""fleet learn lesson|session|brief|digest: LEARN teaches before it tests (DESIGN-NOTES.md, lib/fleet_learn_contract.md).

  fleet learn lesson [--topic T | --lid L] [--yes] [--json]   the lesson: new ones by default, or every live row on T;
                                                     Enter marks it seen (the key that starts the 24 h proof clock)
  fleet learn session --topic T [--max N]            lesson on T, then a quiz on only T's cards, then one result line
  fleet learn brief --for TEXT [--max N] [--json]    meeting prep: rows that matter for TEXT, as lesson lines (read only)
  fleet learn digest [--since ISO|24h|7d|last] [--json]   lessons for rows added or changed since then

A lesson is a view of ledger rows, in the order terms, then use in context, then why, then "not decided yet" for
heard rows. Every line carries the row's verbatim quote and its source. A row's `what` or `why` is shown only when
grounded in its quote (grounded()); otherwise it is dropped and counted. Nothing here writes the ledger: the only
write is a seen record through fleet_learn.mark_seen() (human only) and the digest cursor.
Under a FLEET_ROLE, lesson and digest print a preview and record nothing; session is refused (exit 4). The guard is
a norm, not a security boundary (Q695).
Python 3.8+ stdlib only.
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import fleet_learn as fl  # noqa: E402

STOP = set("""the and for that this with from have has had was were are been will would should could not but into
onto over under then than they them their there here what when where which while who whom whose why how all any
each per via its it's our out off too very just also only about after before again because does did done doing
yes now new use used using make made one two three""".split())
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9#._/-]*")
GROUND_SHARE = 0.5
DEFAULT_BRIEF_MAX = 8
DEFAULT_TIMEOUT = 20.0
LID_LINE = re.compile(r"^\s*(L[0-9]+)\s*$")


# ---------------------------------------------------------------- lesson lines

def _words(text):
    return [w.lower().strip("._/-") for w in WORD_RE.findall(text or "")]


def content_words(text):
    return [w for w in _words(text) if len(w) >= 3 and w not in STOP]


def grounded(line, r):
    """A what/why line is backed by the row when at least half of its content words appear in its quote or title
    (a deterministic form of the explain grader's rule). A line with no content words is not grounded."""
    ws = content_words(line)
    if not ws:
        return False
    have = set(_words((r.get("quote") or "") + " " + (r.get("title") or "")))
    hit = sum(1 for w in ws if w in have)
    return hit >= GROUND_SHARE * len(ws)


def _src(r):
    s = r.get("source") or {}
    return "%s:%s" % (s.get("type"), s.get("ref"))


def term_of(r):
    """The lesson's term: the row's first work subject (ticket, PR, release), else its topic, else its id."""
    subj = fl.subjects(" ".join([r.get("title") or "", r.get("quote") or ""])) or fl.subjects(fl.topic_of(r))
    return subj[0] if subj else (fl.topic_of(r) or r["lid"])


def lesson_row(r, exposed):
    """One row as lesson data: term, definition (grounded what, else the title), quote, source, why when grounded."""
    what, why = (r.get("what") or "").strip(), (r.get("why") or "").strip()
    dropped = []
    definition, dfrom = "", ""
    if what and grounded(what, r):
        definition, dfrom = what, "what"
    elif what:
        dropped.append("what")
    title = (r.get("title") or "").strip()
    if not definition and title and not title.endswith("?"):
        definition, dfrom = title, "title"
    if why and not grounded(why, r):
        dropped.append("why")
        why = ""
    heard = r.get("status") == "heard"
    return {"lid": r["lid"], "term": term_of(r), "definition": definition, "definitionFrom": dfrom,
            "question": title if title.endswith("?") else "", "why": why, "quote": r.get("quote") or "",
            "source": _src(r), "origin": r.get("origin"), "status": r.get("status"), "kind": r.get("kind"),
            "topic": fl.topic_of(r) or "topic", "new": r["lid"] not in exposed,
            "decided": not heard, "label": "not decided yet" if heard else "decided",
            "links": [{"rel": str(x.get("rel") or ""), "type": str(x.get("type")), "ref": str(x.get("ref"))}
                      for x in (r.get("links") or [])
                      if isinstance(x, dict) and x.get("ref")],
            "dropped": dropped}


def build(rows, title=None):
    """rows -> lessons grouped by topic ([{topic, rows, heard, new, tree}]); a title folds them into one lesson."""
    exposed = fl.exposed()
    groups, order = {}, []
    for r in rows:
        key = title or fl.topic_of(r) or "topic"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(lesson_row(r, exposed))
    out = []
    for k in order:
        ls = groups[k]
        dec = [x for x in ls if x["decided"]]
        hrd = [x for x in ls if not x["decided"]]
        out.append({"topic": k, "rows": dec, "heard": hrd, "new": sum(1 for x in ls if x["new"]),
                    "tree": tree(k, dec + hrd)})
    return out


def tree(topic, ls):
    """The lesson's shape as nested nodes: topic -> rows -> their typed links (the entity-layer seam, Q697)."""
    return {"label": topic, "children": [
        {"label": "%s %s" % (x["lid"], x["term"]), "lid": x["lid"], "status": x["status"], "new": x["new"],
         "children": [{"label": ("%s %s %s" % (ln["rel"], ln["type"], ln["ref"])).strip()}
                      for ln in x["links"]]} for x in ls]}


def _tree_lines(node, prefix="", last=True, root=True):
    out = [node["label"] + ("  [new]" if node.get("new") else "") + (
        "  (not decided yet)" if node.get("status") == "heard" else "")] if root else []
    kids = node.get("children") or []
    for i, k in enumerate(kids):
        end = i == len(kids) - 1
        lab = k["label"] + ("  [new]" if k.get("new") else "") + (
            "  (not decided yet)" if k.get("status") == "heard" else "")
        out.append(prefix + ("`-- " if end else "|-- ") + lab)
        out += _tree_lines(k, prefix + ("    " if end else "|   "), end, root=False)
    return out


def _q(x):
    return '| quote: "%s" | source: %s' % (" ".join(x["quote"].split()), x["source"])


def render(lessons, out):
    origin_word = {"fleet": "fleet: we decided or shipped it", "outside": "outside: someone else changed it"}
    for ls in lessons:
        n = len(ls["rows"]) + len(ls["heard"])
        out.write("\nLESSON %s: %d row(s), %d new\n" % (ls["topic"], n, ls["new"]))
        for ln in _tree_lines(ls["tree"]):
            out.write("  %s\n" % ln)
        if ls["rows"]:
            out.write("1. Terms\n")
            for x in ls["rows"]:
                d = x["definition"] or "(no definition yet; the quote below is the lesson)"
                out.write("  %s %s means: %s  %s\n" % (x["lid"], x["term"], d, _q(x)))
            out.write("2. In context\n")
            for x in ls["rows"]:
                out.write("  %s %s, %s  %s\n" % (x["lid"], x["kind"], origin_word.get(x["origin"], x["origin"]),
                                                  _q(x)))
            whys = [x for x in ls["rows"] if x["why"]]
            if whys:
                out.write("3. Why\n")
                for x in whys:
                    out.write("  %s because: %s  %s\n" % (x["lid"], x["why"], _q(x)))
        if ls["heard"]:
            out.write("Not decided yet (heard, never quizzed as decided)\n")
            for x in ls["heard"]:
                out.write("  %s not decided yet  %s\n" % (x["lid"], _q(x)))
        drops = sum(len(x["dropped"]) for x in ls["rows"] + ls["heard"])
        if drops:
            out.write("  (%d what/why line(s) left out: not backed by their quote)\n" % drops)


def _lids(lessons):
    return [x["lid"] for ls in lessons for x in ls["rows"] + ls["heard"]]


# ---------------------------------------------------------------- selections

def _live_teachable():
    return [r for r in fl.items() if fl.teachable(r)[0] == "teach"]


def lesson(topic=None, lid=None, now=None):
    """Lesson data for the pane and the page. topic: every live row on T as one lesson. lid: that row. Neither:
    the lessons due (rows not yet seen), by topic."""
    if lid:
        r = fl.item(lid)
        if r.get("status") not in fl.LIVE:
            raise fl.Refused("%s is %s; only live rows are taught" % (lid, r.get("status")))
        return build([r])
    if topic:
        return build([r for r in _live_teachable() if fl.on_topic(r, topic)], title=topic)
    return build(fl.lessons_due(now=now))


def _role():
    return os.environ.get("FLEET_ROLE") or ""


def _confirm_seen(inp, out):
    """The pane key: Enter marks the lesson seen, n leaves it unseen. End of input marks nothing."""
    out.write("\nEnter when you have read it (marks it seen, cards come due); n = not now: ")
    out.flush()
    ln = inp.readline()
    if not ln:
        out.write("\n")
        return False
    return ln.strip().lower() not in ("n", "no", "q")


def teach(topic=None, lid=None, out=None, now=None, via="lesson", as_json=False, inp=None, yes=False):
    """Print the lesson; the human marks it seen with Enter (or --yes). Under a role it is a preview. Returns the
    lessons."""
    out = out or sys.stdout
    inp = inp or sys.stdin
    ls = lesson(topic=topic, lid=lid, now=now)
    lids = _lids(ls)
    role = _role()
    marked = []
    if as_json:
        if lids and not role and yes:
            marked = fl.mark_seen(lids, via=via, topic=topic, now=now)
        out.write(json.dumps({"lessons": ls, "marked": marked, "preview": bool(role)}, indent=1, sort_keys=True)
                  + "\n")
        return ls
    if not lids:
        out.write("no lesson%s to teach. %s\n" % (" on topic %s" % topic if topic else " due", fl._status_line(now)))
        return ls
    render(ls, out)
    if role:
        out.write("\npreview only (role %s): nothing marked seen; only the human marks a lesson seen\n" % role)
    elif yes or _confirm_seen(inp, out):
        marked = fl.mark_seen(lids, via=via, topic=topic, now=now)
        out.write("OK lesson: %d row(s); %d newly seen, their cards are due now\n" % (len(lids), len(marked)))
    else:
        out.write("not marked seen: its cards stay out of the quiz until it is\n")
    return ls


def session(topic, inp=None, out=None, now=None, max_cards=30, yes=False):
    """Lesson on topic, then a quiz on only that topic's cards, then one result line. The first exposure is never
    graded as learned: proof needs a pass 24 h after the first look, so a same-session pass does not count."""
    fl.human_only("take a LEARN session")
    out = out or sys.stdout
    if not (topic or "").strip():
        raise fl.Refused("session needs --topic")
    ls = teach(topic=topic, out=out, now=now, via="session", inp=inp, yes=yes)
    lids = _lids(ls)
    new = sum(x["new"] for l2 in ls for x in l2["rows"] + l2["heard"])
    out.write("\nQUIZ %s\n" % topic)
    revs = fl.quiz(inp=inp or sys.stdin, out=out, max_cards=max_cards, now=now, topic=topic) if lids else []
    pf = fl.rebuild_proof()
    learned = sum(1 for lid in lids if (pf.get(lid) or {}).get("learned"))
    p = sum(1 for r in revs if r.get("grade") == "pass")
    line = ("SESSION %s: lesson %d row(s), %d new; quiz %d card(s), %d pass, %d fail; learned %d of %d"
            % (topic, len(lids), new, len(revs), p, len(revs) - p, learned, len(lids)))
    if new:
        line += " (first look today: a pass counts toward learned only 24 h after it)"
    out.write(line + "\n")
    return {"topic": topic, "lids": lids, "new": new, "reviews": revs, "learned": learned, "line": line}


# ---------------------------------------------------------------- brief --for

def _selector_cfg():
    cfg = fl._read_json("learn.json", {})
    s = cfg.get("briefSelector") if isinstance(cfg, dict) else None
    if not isinstance(s, dict) or s.get("kind") != "command" or not s.get("run"):
        return None
    return {"run": str(s["run"]), "timeoutSec": float(s.get("timeoutSec", DEFAULT_TIMEOUT))}


def _run_selector(sel, for_text, rows, max_n):
    """The plugin: JSON on stdin, lids on stdout, killed after timeoutSec. Only lids of the given rows are kept;
    any other line is ignored and counted, and its text is never shown. -> (lids, ignored, error)."""
    import fleet_learn_explain as X
    payload = {"for": for_text, "max": max_n,
               "rows": [{k: r.get(k) for k in ("lid", "title", "topic", "quote", "status", "origin", "kind")}
                        for r in rows]}
    ok, lines, err, _ms = X.run_bounded(sel["run"], payload, sel["timeoutSec"])
    if not ok:
        return [], 0, err or "failed"
    known = {r["lid"] for r in rows}
    got, ignored = [], 0
    for ln in lines:
        m = LID_LINE.match(ln)
        if m and m.group(1) in known:
            if m.group(1) not in got:
                got.append(m.group(1))
        elif ln.strip():
            ignored += 1
    return got[:max_n], ignored, ""


def select_fallback(for_text, rows, max_n):
    """No plugin: rows on the topic, else rows ranked by shared words, else the most recent. -> (rows, how)."""
    hit = [r for r in rows if fl.on_topic(r, for_text)]
    if hit:
        return hit[-max_n:], "topic"
    want = set(content_words(for_text))
    scored = []
    for i, r in enumerate(rows):
        have = set(content_words(" ".join([r.get("title") or "", r.get("quote") or "", fl.topic_of(r),
                                           r.get("what") or "", r.get("why") or ""])))
        n = len(want & have)
        if n:
            scored.append((-n, -i, r))
    if scored:
        scored.sort(key=lambda t: (t[0], t[1]))
        return [t[2] for t in scored[:max_n]], "words"
    return rows[-max_n:], "recent"


def brief(for_text, max_n=DEFAULT_BRIEF_MAX):
    """-> {"for", "how", "lessons", "ignored", "selectorError"}. Read only: a brief marks nothing seen."""
    if not (for_text or "").strip():
        raise fl.Refused("brief needs --for TEXT (a meeting, person, ticket or topic)")
    rows = _live_teachable()
    sel = _selector_cfg()
    how, ignored, err, chosen = None, 0, "", []
    if sel:
        lids, ignored, err = _run_selector(sel, for_text, rows, max_n)
        by = {r["lid"]: r for r in rows}
        chosen = [by[x] for x in lids]
        how = "command" if chosen else None
        if not chosen and not err:
            err = "selector returned no known row"
    if not chosen:
        chosen, how = select_fallback(for_text, rows, max_n)
    return {"for": for_text, "how": how, "lessons": build(chosen, title=None), "ignored": ignored,
            "selectorError": err}


def _print_brief(b, out):
    n = len(_lids(b["lessons"]))
    out.write("BRIEF for %s: %d row(s), picked by %s%s\n" % (
        b["for"], n, b["how"], " (selector: %s; fell back)" % b["selectorError"] if b["selectorError"] else ""))
    if b["ignored"]:
        out.write("  (selector: %d line(s) ignored, not row ids of live rows)\n" % b["ignored"])
    render(b["lessons"], out)


# ---------------------------------------------------------------- digest --since

_REL = re.compile(r"^(\d+)([mhd])$")


def parse_since(text, now=None):
    """ISO-8601 UTC, a relative 30m / 24h / 7d, or last (the digest cursor; 7 d back when there is none)."""
    now = fl._now(now)
    t = (text or "last").strip()
    if t == "last":
        cur = fl._read_json("cursors.json", {})
        at = fl._parse_ts(cur.get("digest") if isinstance(cur, dict) else None)
        return at or now - datetime.timedelta(days=7), "last" if at else "7d (no digest yet)"
    m = _REL.match(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": datetime.timedelta(minutes=n), "h": datetime.timedelta(hours=n),
                 "d": datetime.timedelta(days=n)}[unit]
        return now - delta, t
    at = fl._parse_ts(t)
    if not at:
        raise fl.Refused("--since wants ISO-8601 UTC (2026-11-20T09:00:00Z), 30m, 24h, 7d or last; got %r" % t)
    return at, t


def digest(since="last", out=None, now=None, as_json=False, include_seen=False):
    """Teach the live rows added or changed since then that are new to the human (not yet seen as their current
    status; include_seen keeps those too). The human's run marks them seen and moves the cursor; a role preview
    moves nothing."""
    out = out or sys.stdout
    now = fl._now(now)
    at, label = parse_since(since, now)
    seen = fl._seen_as()
    rows = [r for r in _live_teachable() if (fl._parse_ts(r.get("at")) or now) >= at
            and (include_seen or r.get("status") not in seen.get(r["lid"], set()))]
    ls = build(rows)
    lids = _lids(ls)
    role = _role()
    marked = []
    if not role:
        if lids:
            marked = fl.mark_seen(lids, via="digest", now=now)
        with fl._Lock():
            cur = fl._read_json("cursors.json", {})
            cur["digest"] = fl._iso(now)
            fl._write("cursors.json", json.dumps(cur, indent=1, sort_keys=True) + "\n")
    if as_json:
        out.write(json.dumps({"since": fl._iso(at), "lessons": ls, "marked": marked, "preview": bool(role)},
                             indent=1, sort_keys=True) + "\n")
        return ls
    out.write("DIGEST since %s (%s): %d row(s)\n" % (fl._iso(at), label, len(lids)))
    render(ls, out)
    if role:
        out.write("\npreview only (role %s): nothing marked seen, cursor not moved\n" % role)
    elif lids:
        out.write("\nOK digest: %d row(s), %d newly seen; next digest starts at %s\n" % (
            len(lids), len(marked), fl._iso(now)))
    return ls


# ---------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("FAIL usage: %s\n" % message)
        sys.exit(2)


def main(argv):
    cmd, rest = argv[0], argv[1:]
    p = _Parser(prog="fleet learn %s" % cmd)
    if cmd == "lesson":
        g = p.add_mutually_exclusive_group()
        g.add_argument("--topic")
        g.add_argument("--lid")
        p.add_argument("--yes", action="store_true")
        p.add_argument("--json", action="store_true")
    elif cmd == "session":
        p.add_argument("--topic", required=True)
        p.add_argument("--max", type=int, default=30)
        p.add_argument("--yes", action="store_true")
    elif cmd == "brief":
        p.add_argument("--for", dest="for_text", required=True)
        p.add_argument("--max", type=int, default=DEFAULT_BRIEF_MAX)
        p.add_argument("--json", action="store_true")
    elif cmd == "digest":
        p.add_argument("--since", default="last")
        p.add_argument("--all", action="store_true")
        p.add_argument("--json", action="store_true")
    a = p.parse_args(rest)
    try:
        if cmd == "lesson":
            teach(topic=a.topic, lid=a.lid, as_json=a.json, yes=a.yes)
        elif cmd == "session":
            session(a.topic, max_cards=a.max, yes=a.yes)
        elif cmd == "brief":
            if a.max < 1:
                raise fl.Refused("--max must be 1 or more")
            b = brief(a.for_text, max_n=a.max)
            if a.json:
                print(json.dumps(b, indent=1, sort_keys=True))
            else:
                _print_brief(b, sys.stdout)
        elif cmd == "digest":
            digest(a.since, as_json=a.json, include_seen=a.all)
    except fl.RoleRefused as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 4
    except fl.Refused as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    return 0
