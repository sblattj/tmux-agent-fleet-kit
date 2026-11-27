"""fleet learn lesson / lessons / session / brief / digest / entity: LEARN teaches before it tests.

A lesson follows the human's model: terms first (each work subject with a definition), then each row as an example
of the term in context. Every line carries a verbatim quote and its source (Q696); a what/why line that is not
backed by its row's quote is dropped and counted. Heard rows show "not decided yet" and get no cards.

Showing a lesson records it seen (seen.jsonl, the human only, Q695): the 24 h proof clock starts there and only then
do that row's cards come due. --preview shows the same lesson and records nothing.

Python 3.8+ stdlib only. Reads and writes only through lib/fleet_learn.py.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fleet_learn as fl  # noqa: E402

MAX_ROWS = 8
DIGEST_DEFAULT = datetime.timedelta(days=7)
BRIEF_MAX = 10
BRIEF_TIMEOUT = 10


def _src(s):
    s = s or {}
    return "%s:%s" % (s.get("type"), s.get("ref"))


def _q(text):
    return '"%s"' % " ".join(str(text or "").split())


# ---------------------------------------------------------------- model

def _topics_of(rows):
    out = []
    for r in rows:
        t = fl.topic_of(r)
        if t and t not in out:
            out.append(t)
    return out


def terms_for(rows):
    """[term dict]: each topic the rows are on, then its part-of ancestors, each with a verbatim definition."""
    es = fl.entities()
    out, seen = [], set()
    for t in _topics_of(rows):
        for eid in [t] + fl.ancestors(t):
            if eid in seen:
                continue
            seen.add(eid)
            e = es.get(eid)
            if not e or not str(e.get("quote") or "").strip():
                continue
            g = fl.ground(e.get("definition") or e.get("quote"), e.get("quote"))
            if g is None:
                continue
            out.append({"id": eid, "type": e.get("type"), "definition": g[0], "quote": g[1],
                        "source": _src(e.get("source")),
                        "edges": {k: v for k, v in (e.get("edges") or {}).items() if v}})
    return out


def context_for(rows):
    """[context dict], dropped count. what/why lines appear only when backed by the row's quote (Q696)."""
    out, dropped = [], 0
    for r in rows:
        lines = []
        for f in ("what", "why"):
            v = str(r.get(f) or "").strip()
            if not v:
                continue
            g = fl.ground(v, r.get("quote"))
            if g is None:
                dropped += 1
                continue
            lines.append({"field": f, "text": g[0], "quote": g[1]})
        out.append({"lid": r["lid"], "kind": r.get("kind"), "status": r.get("status"), "topic": fl.topic_of(r),
                    "quote": r.get("quote") or "", "source": _src(r.get("source")),
                    "heard": r.get("status") == "heard", "lines": lines})
    return out, dropped


def lesson_model(rows):
    ctx, dropped = context_for(rows)
    return {"terms": terms_for(rows), "context": ctx, "dropped": dropped, "lids": [r["lid"] for r in rows]}


def unseen_rows(topic=None):
    """Rows whose lesson is not seen yet, in serve order: the manual stack first (newest request first, then row
    order), then auto rows near a manual topic, then the rest, oldest first."""
    seen = fl.seen_lids()
    every = fl.items()
    rows = [r for r in every if r["lid"] not in seen and (topic is None or fl.on_topic(r, topic))]
    sk = fl.serve_order(every)
    return sorted(rows, key=lambda r: sk(r) + (r.get("at") or "", fl._idnum(r["lid"])))


def lessons(topic=None):
    """Topics with unseen rows: [{topic, rows, heard, stack}] in serve order (manual topics first, the newest request
    on top; then auto topics in the order their oldest row arrived). A topic with rows on both stacks is listed once
    per stack, so the manual batch is taught on its own."""
    by = {}
    for r in unseen_rows(topic):
        t = fl.topic_of(r) or "(no topic)"
        d = by.setdefault((t, fl.stack_of(r)), {"topic": t, "rows": 0, "heard": 0, "lids": [], "stack": fl.stack_of(r)})
        d["rows"] += 1
        d["heard"] += r.get("status") == "heard"
        d["lids"].append(r["lid"])
    return list(by.values())


# ---------------------------------------------------------------- render

def render(model, title):
    out = ["Lesson: %s (%d row%s)" % (title, len(model["context"]), "" if len(model["context"]) == 1 else "s")]
    out.append("Terms")
    if not model["terms"]:
        out.append("  (no term with a source quote yet)")
    for t in model["terms"]:
        out.append("  %s (%s): %s [%s]" % (t["id"], t["type"], _q(t["definition"]), t["source"]))
        for et, vs in sorted(t["edges"].items()):
            out.append("    %s: %s" % (et, ", ".join(vs)))
    out.append("In context")
    for c in model["context"]:
        tag = "heard, not decided yet" if c["heard"] else c["kind"]
        out.append("  %s %s: %s [%s]" % (c["lid"], tag, _q(c["quote"]), c["source"]))
        for ln in c["lines"]:
            out.append("     %s: %s %s [%s]" % (ln["field"], ln["text"], _q(ln["quote"]), c["source"]))
    if model["dropped"]:
        out.append("%d line%s dropped (not backed by %s quote)" % (
            model["dropped"], "" if model["dropped"] == 1 else "s", "its" if model["dropped"] == 1 else "their"))
    return "\n".join(out)


def tree_lines(root=None):
    """Pane text tree of the entity graph (part-of), roots first, children indented two spaces."""
    es = fl.entities()
    if root and root not in es:
        raise fl.Refused("no entity %s (fleet learn entity list shows them)" % root)
    roots = [root] if root else sorted(k for k, e in es.items() if not (e.get("edges") or {}).get("part-of"))
    out, seen = [], set()

    def walk(eid, depth):
        if eid in seen:
            return
        seen.add(eid)
        e = es.get(eid) or {}
        n = len([r for r in fl.items() if fl.topic_of(r) == eid])
        out.append("%s%s (%s)%s" % ("  " * depth, eid, e.get("type") or "topic", ", %d row%s" % (
            n, "" if n == 1 else "s") if n else ""))
        for c in fl.children(eid):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    return out


# ---------------------------------------------------------------- verbs

def lesson(topic=None, max_rows=MAX_ROWS, preview=False, out=None, via="lesson", now=None):
    """Teach one topic's unseen rows (a bare lesson takes the first topic waiting). Returns the model with
    "marked" (lids recorded seen) and "waiting" (rows left on other topics)."""
    out = out or sys.stdout
    if not preview:
        fl.human_only("mark lessons seen")
    if topic is None:
        ls = lessons()
        if not ls:
            out.write("no new lessons\n")
            return {"marked": [], "waiting": 0, "context": []}
        topic = ls[0]["topic"]
        rows = [fl.item(l) for l in ls[0]["lids"]]
    else:
        rows = unseen_rows(topic)
    if not rows:
        out.write("no new lessons on %s\n" % topic)
        return {"marked": [], "waiting": len(unseen_rows()), "context": []}
    rows = rows[:max_rows]
    model = lesson_model(rows)
    out.write(render(model, topic) + "\n")
    marked = []
    if preview:
        out.write("preview: nothing recorded\n")
    else:
        marked = [r["lid"] for r in fl.mark_seen(model["lids"], via=via, now=now)]
        out.write("OK marked seen: %s\n" % ", ".join(marked))
    waiting = len(unseen_rows()) - (len(rows) if preview else 0)
    if waiting > 0:
        out.write("%d more row%s waiting (fleet learn lessons lists them)\n" % (waiting, "" if waiting == 1 else "s"))
    model.update(marked=marked, waiting=waiting, topic=topic)
    return model


def session(topic=None, inp=None, out=None, max_rows=MAX_ROWS, now=None):
    """The lesson for one topic, then a quiz on that topic only, then one result line. With no topic: the first
    topic waiting for a lesson (the manual stack first), else the topic of the first due card."""
    out = out or sys.stdout
    fl.human_only("take a session")
    if not topic:
        ls = lessons()
        ds = [] if ls else fl.due(now)
        if not ls and not ds:
            out.write("no new lessons and nothing due\n")
            return {"lesson": {}, "reviews": [], "learned": 0, "line": "session: nothing waiting"}
        if ls:  # the first waiting lesson, the manual batch on its own when the manual stack leads
            m = lesson(None, max_rows=max_rows, out=out, via="session", now=now)
            topic = m.get("topic")
        else:
            topic = fl.topic_of(fl.item(ds[0]["lid"]))
            m = lesson(topic, max_rows=max_rows, out=out, via="session", now=now)
    else:
        m = lesson(topic, max_rows=max_rows, out=out, via="session", now=now)
    revs = fl.quiz(inp=inp, out=out, topic=topic, now=now)
    pf = fl.rebuild_proof()
    lids = {r["lid"] for r in fl.items() if fl.on_topic(r, topic)}
    learned = len([l for l in lids if (pf.get(l) or {}).get("learned")])
    p = len([r for r in revs if r.get("grade") == "pass"])
    line = "session %s: %d lesson(s), %d card(s), %d pass, %d fail, %d learned" % (
        topic, len(m.get("marked") or []), len(revs), p, len(revs) - p, learned)
    out.write(line + "\n")
    return {"lesson": m, "reviews": revs, "learned": learned, "line": line}


def parse_since(text, now=None):
    now = fl._now(now)
    t = (text or "").strip()
    if not t:
        raise fl.Refused("--since needs an ISO time, Nd or Nh")
    if t[-1:] in ("d", "h") and t[:-1].isdigit():
        n = int(t[:-1])
        return now - (datetime.timedelta(days=n) if t[-1] == "d" else datetime.timedelta(hours=n))
    ts = fl._parse_ts(t)
    if ts is None:
        raise fl.Refused("--since %r is not an ISO time, Nd or Nh" % t)
    return ts


def digest(since=None, preview=False, out=None, now=None, max_rows=50):
    """Lessons for the rows newer than the last digest (or --since, else 7 days), grouped by topic; records them seen
    and moves the digest cursor (the latest digest-cursor row in seen.jsonl)."""
    out = out or sys.stdout
    if not preview:
        fl.human_only("mark lessons seen")
    now = fl._now(now)
    if since:
        start = parse_since(since, now)
    else:
        cur = fl.digest_cursor()
        start = fl._parse_ts(cur) if cur else now - DIGEST_DEFAULT
    seen = fl.seen_lids()
    rows = [r for r in fl.items() if r["lid"] not in seen and (fl._parse_ts(r.get("at")) or now) > start]
    rows = sorted(rows, key=lambda r: (r.get("at") or "", fl._idnum(r["lid"])))[:max_rows]
    if not rows:
        out.write("nothing new since %s\n" % fl._iso(start))
        if not preview:
            fl.mark_seen([], via="digest", now=now)
        return {"marked": [], "topics": []}
    topics = _topics_of(rows) or ["(no topic)"]
    for t in topics:
        sub = [r for r in rows if (fl.topic_of(r) or "(no topic)") == t]
        out.write(render(lesson_model(sub), t) + "\n\n")
    marked = []
    if preview:
        out.write("preview: nothing recorded\n")
    else:
        marked = [r["lid"] for r in fl.mark_seen([r["lid"] for r in rows], via="digest", now=now)]
        out.write("OK digest since %s: %d row(s) on %d topic(s) marked seen\n" % (fl._iso(start), len(marked),
                                                                                 len(topics)))
    return {"marked": marked, "topics": topics}


def _brief_cfg():
    cfg = fl._read_json("learn.json", {})
    b = cfg.get("brief") if isinstance(cfg, dict) else None
    return b if isinstance(b, dict) else {}


def brief(for_, max_rows=BRIEF_MAX, as_json=False, out=None):
    """A read-only brief on X for any role: X's ancestor chain, then the rows a selector picks (learn.json
    brief.selector: a command reading {for, max, rows} JSON on stdin, printing one lid per line), else the rows on X
    or its subtree, else the most recent rows. Unknown lids from a selector are dropped."""
    out = out or sys.stdout
    rows = fl.items()
    on = [r for r in rows if fl.on_topic(r, for_)]
    cfg = _brief_cfg()
    note, picked = "", None
    if cfg.get("selector"):
        import fleet_learn_explain as fx
        payload = {"for": for_, "max": max_rows,
                   "rows": [{k: r.get(k) for k in ("lid", "kind", "status", "title", "quote", "topic", "source", "at")}
                            for r in rows]}
        ok, lines, err, _ms = fx.run_bounded(cfg["selector"], payload, float(cfg.get("timeoutSec") or BRIEF_TIMEOUT))
        if ok:
            byid = {r["lid"]: r for r in rows}
            picked = [byid[l.strip()] for l in lines if l.strip() in byid][:max_rows]
        else:
            note = "selector failed (%s), fallback used" % err
    if picked is None:
        if on:
            picked = sorted(on, key=lambda r: r.get("at") or "", reverse=True)[:max_rows]
        else:
            picked = sorted(rows, key=lambda r: r.get("at") or "", reverse=True)[:max_rows]
            note = (note + "; " if note else "") + "no rows on %s, most recent shown" % for_
    chain = fl.ancestors(for_)
    model = lesson_model(picked)
    res = {"for": for_, "chain": chain, "note": note, "terms": model["terms"], "context": model["context"],
           "dropped": model["dropped"]}
    if as_json:
        out.write(json.dumps(res, indent=1, sort_keys=True) + "\n")
    else:
        out.write("Brief: %s%s\n" % (for_, (" (part of %s)" % " > ".join(chain)) if chain else ""))
        if note:
            out.write(note + "\n")
        out.write(render(model, for_) + "\n")
    return res
