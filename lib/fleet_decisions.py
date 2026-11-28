#!/usr/bin/env python3
"""fleet decisions: the DECISIONS UI. A second front end on the desk's state, never a new source of truth.

Python 3.8+ stdlib only. Reads decisions.jsonl, the desk goal file (openCalls[], or legacy openForHuman[]), decisions-log.jsonl,
decisions-reports.jsonl and the roster; writes only through the desk's own paths (fleet_mesh mark/deliver,
desk.sh answer/revise), so the desk pane and the UI end in the same state.

  fleet decisions serve [--port P] [--bind 127.0.0.1] [--open]   local web page (loopback + per-launch token)
  fleet decisions tui [--one] [--plain]                           terminal / tmux fallback (also a display-popup)
  fleet decisions pick <id> <letter|--text "..."|--edit "..."|--refuse ["why"]>
  fleet decisions retry <id>                                      re-wake the targets whose relay failed or queued
  fleet decisions snooze <id> [minutes]                           hide a call for N minutes (default 30)
  fleet decisions state [--json]                                  open cards and the Called list
  fleet decisions ask "<q>" [--options a,b] [--allow pick,own] [--wait] [--timeout 30m] [--new]
                                                                  exit 0 picked, 1 refused / own answer, 2 no answer yet
  fleet decisions find "<text>"                                   past answered calls matching the text
  fleet decisions learn [<id>]                                    learn this: one LEARN row with the human's words
  fleet decisions intake ["<WATCH lines>"|-]                      DECIDE: FYI lines to ROUTE alone; TASK-CANDIDATE
                                                                  lines become calls (ROUTE hears only after a yes)
  fleet decisions forward                                         settle task calls answered in the desk pane
  fleet decisions report <id> ok|fail [note]                      the asker says whether it acted on the answer
  fleet decisions refresh [--json] | refresh choose <id> A|B | refresh confirm <id>
                                                                  ask every lane to re-sync, look for answers given
                                                                  elsewhere; B rows relay only after choose B + confirm
Config: $FLEET_STATE/decisions-ui.json  {port, notify, notifyCommand, copyOrchestrator, refreshSources}
"""
import datetime
import fcntl
import hmac
import http.server
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleet_mesh as mesh  # noqa: E402

ROOT = os.path.dirname(HERE)
DESK_SH = os.path.join(ROOT, "skills", "running-the-decisions-desk", "desk.sh")
HTML = os.path.join(HERE, "decisions-ui.html")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
EXIT_ALREADY = 3


class NotFound(ValueError):
    pass


AlreadyAnswered = mesh.AlreadyAnswered


# ---------------------------------------------------------------- config / paths

def state_path(name):
    return os.path.join(mesh.state_dir(), name)


def goal_path():
    return os.environ.get("FLEET_DESK_GOAL") or state_path("desk.goal.json")


def load_config():
    cfg = {"port": 7340, "notify": True, "notifyCommand": None, "copyOrchestrator": True,
           "refreshSources": [{"kind": "jsonl", "path": "$FLEET_STATE/decisions-log.jsonl", "label": "decisions log"}]}
    try:
        with open(state_path("decisions-ui.json")) as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update(user)
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def expand(p):
    return os.path.expanduser(str(p).replace("$FLEET_STATE", mesh.state_dir()).replace("${FLEET_STATE}",
                                                                                       mesh.state_dir()))


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def sender():
    return os.environ.get("FLEET_NAME") or mesh.desk_name()


def _parse_at(at):
    try:
        return datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def ago(at):
    t = _parse_at(at)
    if not t:
        return ""
    s = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


def hhmm(at):
    t = _parse_at(at)
    return t.astimezone().strftime("%H:%M") if t else "?"


def _idnum(i):
    m = re.search(r"(\d+)$", i or "")
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- options

_INLINE = re.compile(r"\(([a-z])(\s*,\s*rec(?:ommended)?)?\)\s*", re.I)


def parse_option(o):
    """{text, rec, downside, letter}; letter is the asker's own, or None (parsing lives in fleet_mesh, issue #59)."""
    p = mesh.parse_option(o)
    p.setdefault("downside", None)
    return p


def split_inline(question):
    """'Ship now? (a, rec) yes (b) no' -> ('Ship now?', ['(a, rec) yes', '(b) no']); no markers -> (q, [])."""
    marks = list(_INLINE.finditer(question or ""))
    if len(marks) < 2 or marks[0].group(1).lower() != "a":
        return question, []
    opts = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(question)
        opts.append(question[m.start():end].strip())
    return question[:marks[0].start()].strip(), opts


def build_options(raw):
    """Card options: the stored letters in the asker's order; rec is a flag and a badge, never a position (#59)."""
    opts = mesh.option_list({"options": list(raw or [])})
    for o in opts:
        o.setdefault("downside", None)
    return opts


# ---------------------------------------------------------------- reading state

GOAL_OPEN_KEYS = ("openCalls", "openForHuman")  # written name first, then legacy names still read


def goal_items():
    g = _read_json(goal_path(), {})
    if not isinstance(g, dict):
        return []
    for k in GOAL_OPEN_KEYS:
        if k in g:
            return g.get(k) or []
    return []


def latest_log():
    out = {}
    for rec in mesh._parse_lines(mesh.decisions_log_path()):
        if rec.get("id") and not rec.get("action"):  # DECIDE's alone/nuke rows (probe contract) are not answers
            out[rec["id"]] = rec
    return out


def latest_reports():
    out = {}
    for rec in mesh._parse_lines(mesh.reports_path()):
        if rec.get("id"):
            out[rec["id"]] = rec
    return out


def ui_state():
    s = _read_json(state_path("decisions-ui-state.json"), {})
    return s if isinstance(s, dict) else {}


def _save_ui_state(s):
    mesh._atomic_write(state_path("decisions-ui-state.json"), json.dumps(s, indent=2, sort_keys=True) + "\n")


def _uniq(xs):
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


def all_calls():
    """Every call keyed by id, merged across decisions.jsonl and the desk goal file."""
    calls = {}
    for d in mesh.decisions():
        calls[d["id"]] = {"id": d["id"], "at": d.get("at"), "from": d.get("from"), "question": d.get("question") or "",
                          "rawOptions": d.get("options") or [], "allow": d.get("allow"), "relayTo": [],
                          "mesh": d.get("status"), "goal": None, "answer": d.get("answer"),
                          "watch": clean_watch(d.get("watch"))}
    for g in goal_items():
        gid = g.get("id")
        if not gid:
            continue
        c = calls.get(gid)
        if c is None:
            q, inline = split_inline(g.get("question") or "")
            c = calls[gid] = {"id": gid, "at": g.get("at"), "from": g.get("from"), "question": q,
                              "rawOptions": inline, "allow": None, "relayTo": [], "mesh": None, "goal": None,
                              "answer": g.get("answer"), "watch": None}
        elif c["mesh"] is not None:
            gq = mesh._norm(split_inline(g.get("question") or "")[0])
            mq = mesh._norm(c["question"])
            if gq and mq and not (gq == mq or gq in mq or mq in gq):
                c["conflict"] = "the desk goal file has a different call under %s: %s" % (gid, g.get("question"))
        c["goal"] = g.get("status")
        c["relayTo"] = list(g.get("relayTo") or [])
        if c["answer"] is None:
            c["answer"] = g.get("answer")
    closed = ui_state().get("closed") or {}
    for c in calls.values():
        stores = [s for s in (c["mesh"], c["goal"]) if s is not None]
        c["open"] = bool(stores) and all(s == "open" for s in stores) and c["id"] not in closed
    return calls


DEFAULT_ALLOW = ["pick", "edit", "own", "snooze", "refuse"]


def open_cards(include_snoozed=False):
    calls = all_calls()
    meta = calls_meta()
    opened = sorted((c for c in calls.values() if c["open"]), key=lambda c: (c.get("at") or "", _idnum(c["id"])))
    groups, order = {}, []
    for c in opened:  # oldest first: the oldest id of a duplicate group is the primary
        key = mesh._norm(c["question"])
        if key in groups:
            groups[key].append(c)
        else:
            groups[key] = [c]
            order.append(key)
    snoozed = {k: v for k, v in (ui_state().get("snoozed") or {}).items()
               if (_parse_at(v) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
               > datetime.datetime.now(datetime.timezone.utc)}
    cards, n_snoozed = [], 0
    for key in order:
        prim, dups = groups[key][0], groups[key][1:]
        askers = _uniq([prim["from"]] + prim["relayTo"] + [x for d in dups for x in [d["from"]] + d["relayTo"]])
        card = {"id": prim["id"], "at": prim.get("at"), "ago": ago(prim.get("at")), "from": prim["from"],
                "question": prim["question"], "options": build_options(prim["rawOptions"]),
                "allow": prim.get("allow") or DEFAULT_ALLOW, "askers": askers,
                "alsoAskedBy": [a for a in askers if a != prim["from"]], "dupIds": [d["id"] for d in dups],
                "conflict": prim.get("conflict"),
                "askerOf": {d["id"]: _uniq([d["from"]] + d["relayTo"]) for d in [prim] + dups},
                "watch": watch_of(prim["id"], prim, meta), "task": (meta.get(prim["id"]) or {}).get("kind")}
        if prim["id"] in snoozed and not include_snoozed:
            n_snoozed += 1
            continue
        card["snoozedUntil"] = snoozed.get(prim["id"])
        cards.append(card)
    cards.sort(key=lambda c: (c.get("at") or "", _idnum(c["id"])), reverse=True)
    return cards, n_snoozed


def classify(result):
    r = result or ""
    if "FAILED" in r:
        return "failed"
    if r.startswith("queued"):
        return "queued"
    if r.startswith("reopened"):
        return "reopened"
    if r.startswith("delivered"):
        return "relayed"
    return "failed"


_RANK = {"failed": 3, "queued": 2, "reopened": 1, "relayed": 0}


def badge_of(delivery):
    if not delivery:
        return "relayed"
    return max((classify(v) for v in delivery.values()), key=lambda b: _RANK[b])


def called_list():
    calls = all_calls()
    logs, reports = latest_log(), latest_reports()
    meta, learned = calls_meta(), learned_ids()
    out = []
    for cid, ln in logs.items():
        c = calls.get(cid) or {}
        if c.get("open"):
            continue
        delivery = ln.get("delivery") or {}
        b = badge_of(delivery)
        rep = reports.get(cid)
        out.append({"id": cid, "at": ln.get("at"), "time": hhmm(ln.get("at")), "question": c.get("question", ""),
                    "asker": ln.get("asker"), "verbatim": ln.get("verbatim"), "kind": ln.get("kind"),
                    "relayedTo": ln.get("relayedTo") or [], "copiedTo": ln.get("copiedTo") or [],
                    "via": ln.get("via") or "desk", "delivery": delivery, "badge": b,
                    "retry": b in ("failed", "queued") and bool(delivery),
                    "acted": (None if not rep else ("acted on" if rep.get("ok") else "act failed")),
                    "actedNote": rep.get("note") if rep else None, "source": ln.get("source"),
                    "watch": watch_of(cid, c, meta), "task": (meta.get(cid) or {}).get("kind"),
                    "forwarded": ln.get("forwarded"), "learned": learned.get(cid)})
    out.sort(key=lambda r: (r.get("at") or "", _idnum(r["id"])), reverse=True)
    return out


def snapshot():
    cards, n_snoozed = open_cards()
    called = called_list()
    return {"open": cards, "called": called, "snoozed": n_snoozed, "openCount": len(cards),
            "calledCount": len(called), "desk": mesh.desk_name(), "orchestrator": mesh.orch_name(),
            "notify": bool(load_config().get("notify", True))}


# ---------------------------------------------------------------- writes

def _lock():
    path = state_path(".decisions-pick.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def _desk_sh(args, extra=None):
    env = dict(os.environ)
    env["FLEET_DESK_GOAL"] = goal_path()
    if extra is not None:
        env["FLEET_DECISION_EXTRA"] = json.dumps(extra)
    try:
        p = subprocess.run(["bash", DESK_SH] + args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def resolve_answer(card, option=None, text=None, kind=None, refuse=False):
    if refuse:
        return (text or "refused"), "refuse"
    if option is not None:
        o = str(option).strip()
        for opt in card["options"]:
            if o.lower() == opt["letter"] or o == opt["text"]:
                return "%s: %s" % (opt["letter"], opt["text"]), "pick"
        raise ValueError("%s has no option %r (options: %s)" % (
            card["id"], o, ", ".join(x["letter"] for x in card["options"]) or "none, answer with --text"))
    if text is None or not str(text).strip():
        raise ValueError("an option letter or answer text is required")
    return str(text), (kind or "own")


def find_open(cid):
    cards, _ = open_cards(include_snoozed=True)
    for c in cards:
        if c["id"] == cid or cid in c["dupIds"]:
            return c
    if cid in all_calls():
        raise AlreadyAnswered("%s is already answered" % cid)
    raise NotFound("no such call: %s" % cid)


def pick(cid, option=None, text=None, kind=None, refuse=False, via="ui", extra=None, frontend="web"):
    """Answer one call: mark it, relay at once to every asker (+ the orchestrator copy), record one log line.

    Idempotent on the call id: a second pick (any front end) raises AlreadyAnswered and relays nothing."""
    cfg = load_config()
    lk = _lock()
    try:
        card = find_open(cid)
        if card.get("conflict"):
            raise ValueError("%s is ambiguous (%s); answer it in the desk pane" % (card["id"], card["conflict"]))
        verbatim, kind = resolve_answer(card, option, text, kind, refuse)
        need = {"pick": "pick", "own": "own", "edit": "edit", "refuse": "refuse"}.get(kind)
        if need and need not in card["allow"]:
            raise ValueError("%s does not allow %s (allow: %s)" % (card["id"], need, ", ".join(card["allow"])))
        ids = [card["id"]] + card["dupIds"]
        mesh_ids = {d["id"] for d in mesh.decisions()}
        goal_only = [i for i in ids if i not in mesh_ids]
        if goal_only:  # no decisions.jsonl row to mark: close them in the UI's own state first, so a failed
            st = ui_state()  # desk.sh answer below can never let a second click relay again
            st.setdefault("closed", {}).update({i: verbatim for i in goal_only})
            _save_ui_state(st)
        fields = {"answerKind": kind, "via": via}
        for i in ids:  # the commit point: a racing desk-pane decision_answer makes this raise for the primary
            if i in mesh_ids:
                try:
                    mesh.mark_decision(i, verbatim, extra=fields, only_open=True)
                except AlreadyAnswered:
                    if i == card["id"]:
                        raise
        frm = sender()
        delivery = {}
        for i in ids:
            for to in card["askerOf"].get(i, []):
                if to not in delivery:
                    delivery[to] = mesh.deliver(frm, to, "[answer %s] %s" % (i, verbatim))
        copied = []
        orch = mesh.orch_name()
        tmeta = calls_meta().get(card["id"]) or {}
        forwarded, notes = None, []
        if tmeta.get("kind") in TASK_KINDS:  # a task reaches ROUTE only as TASK, only after a yes (Q88, 3.7)
            forwarded, notes = _settle_task(card["id"], tmeta, verbatim, kind, card["options"])
            if forwarded:
                delivery[orch] = forwarded
        elif cfg.get("copyOrchestrator", True) and orch not in card["askers"]:
            delivery[orch] = mesh.deliver(frm, orch, "[DECISIONS UI copy] %s from %s -> %s: %s" % (
                card["id"], card["from"], ", ".join(card["askers"]), verbatim))
            copied.append(orch)
        rec_extra = {"via": via, "frontend": frontend, "kind": kind, "copiedTo": copied, "delivery": delivery}
        if forwarded:
            rec_extra["forwarded"] = orch
        rec_extra.update(extra or {})
        goal_open = {g.get("id") for g in goal_items() if g.get("status") == "open"}
        for i in ids:
            done = False
            if i in goal_open:
                ok, out = _desk_sh(["answer", i, verbatim] + card["askers"], rec_extra)
                done = ok
                if not ok:
                    notes.append("desk.sh answer %s failed: %s" % (i, out))
            if not done:
                c = all_calls().get(i) or {}
                line = {"id": i, "at": mesh.now(), "asker": c.get("from") or card["from"], "verbatim": verbatim,
                        "relayedTo": card["askers"]}
                line.update(rec_extra)
                mesh._append_line(mesh.decisions_log_path(), json.dumps(line))
        mesh.log("decisions-ui pick %s kind=%s via=%s badge=%s" % (card["id"], kind, via, badge_of(delivery)))
        return {"ok": True, "id": card["id"], "ids": ids, "verbatim": verbatim, "kind": kind, "delivery": delivery,
                "status": badge_of(delivery), "copiedTo": copied, "notes": notes,
                "forwarded": orch if forwarded else None}
    finally:
        lk.close()


def rewake(frm, to, text):
    """Wake `to` again without writing a second inbox line (the first relay already wrote it)."""
    entry = mesh.load_roster().get(to)
    if not entry:
        return "queued (no live lane): %s -> %s" % (frm, to)
    prefix = ""
    if entry.get("status") in ("parked", "done"):
        ok, detail = mesh.reopen(to)
        if not ok:
            return "delivered %s -> %s (inbox written; reopen FAILED: %s)" % (frm, to, detail)
        prefix = "reopened %s; " % to
        entry = mesh.load_roster().get(to) or entry
    how, ok, detail = mesh.wake(frm, to, text, entry)
    return mesh.wake_result(prefix, frm, to, how, ok, detail)


def retry(cid):
    lk = _lock()
    try:
        ln = latest_log().get(cid)
        if not ln or not ln.get("delivery"):
            raise NotFound("no UI relay recorded for %s" % cid)
        delivery = dict(ln["delivery"])
        frm = sender()
        for to, res in ln["delivery"].items():
            if classify(res) in ("failed", "queued"):
                text = ("[answer %s] %s" % (cid, ln.get("verbatim")) if to != mesh.orch_name() or
                        to in (ln.get("relayedTo") or []) else "[DECISIONS UI copy] %s: %s" % (cid, ln.get("verbatim")))
                delivery[to] = rewake(frm, to, text)
        new = dict(ln)
        new.update({"at": mesh.now(), "delivery": delivery, "retry": True})
        mesh._append_line(mesh.decisions_log_path(), json.dumps(new))
        return {"ok": True, "id": cid, "delivery": delivery, "status": badge_of(delivery)}
    finally:
        lk.close()


def snooze(cid, minutes=30):
    card = find_open(cid)
    if "snooze" not in card["allow"]:
        raise ValueError("%s does not allow snooze" % cid)
    s = ui_state()
    until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=float(minutes)))
    s.setdefault("snoozed", {})[cid] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_ui_state(s)
    return {"ok": True, "id": cid, "snoozedUntil": s["snoozed"][cid]}


def revise(cid, new_q):
    """Change a call's question in both stores (desk.sh revise for the goal file)."""
    path = mesh._decisions_path()
    if os.path.exists(path):
        with open(path, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                rows = [json.loads(x) for x in f.read().split("\n") if x.strip()]
                for r in rows:
                    if r.get("id") == cid:
                        r["question"] = new_q
                f.seek(0)
                f.truncate()
                f.write("".join(json.dumps(r) + "\n" for r in rows))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    if any(g.get("id") == cid for g in goal_items()):
        ok, out = _desk_sh(["revise", cid, new_q])
        if not ok:
            raise ValueError("desk.sh revise failed: %s" % out)
    return {"ok": True, "id": cid, "question": new_q}


# ---------------------------------------------------------------- find / ask / report

def find(text):
    hits = mesh.find_decisions(text)
    seen = {h["id"] for h in hits}
    want = mesh._norm(text)
    logs = latest_log()
    for g in goal_items():
        if g.get("id") in seen or g.get("status") not in ("answered", "answered-elsewhere"):
            continue
        q = mesh._norm(g.get("question"))
        if want and (q == want or want in q or q in want):
            lg = logs.get(g["id"]) or {}
            hits.append({"id": g["id"], "question": g.get("question"), "verbatim": lg.get("verbatim") or g.get("answer"),
                         "at": lg.get("at") or g.get("answeredAt"), "asker": g.get("from"), "exact": q == want})
    hits.sort(key=lambda h: not h["exact"])
    return hits


def answer_code(verbatim, kind, raw_options):
    """0 = a clean pick of one of the options, 1 = refused / own answer / changes asked."""
    if kind == "pick":
        return 0
    if kind in ("own", "edit", "refuse", "found"):
        opts = build_options(raw_options or [])
        v = (verbatim or "").strip()
        return 0 if any(v == "%s: %s" % (o["letter"], o["text"]) or v == o["text"] for o in opts) and \
            kind == "found" else 1
    opts = build_options(raw_options or [])
    v = (verbatim or "").strip().lower()
    for o in opts:
        if v in (o["letter"], o["text"].lower(), "%s: %s" % (o["letter"], o["text"].lower())):
            return 0
    return 1


def parse_duration(s):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", str(s))
    if not m:
        raise ValueError("bad duration %r (use 30s, 30m, 1h)" % s)
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def ask(question, options, allow, wait, timeout, force_new, out=sys.stdout):
    mesh.side_refuse("open")  # a side run asks through its parent lane, never the desk (issue #12)
    if not force_new:
        for h in find(question):
            if h["exact"]:
                ln = latest_log().get(h["id"]) or {}
                raw = (all_calls().get(h["id"]) or {}).get("rawOptions") or options
                out.write('answered before: %s at %s: "%s"\n' % (h["id"], h.get("at"), h.get("verbatim")))
                return answer_code(h.get("verbatim"), ln.get("kind"), raw)
    args = {"question": question, "options": options, "from": os.environ.get("FLEET_NAME")}
    if allow:
        args["allow"] = allow
    res = mesh.t_decision_open(args)
    out.write(res + "\n")
    out.flush()
    did = re.match(r"opened (Q\d+)", res).group(1)
    if not wait:
        return 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = next((d for d in mesh.decisions() if d.get("id") == did), None)
        if row and row.get("status") != "open":
            ln = latest_log().get(did) or {}
            kind = row.get("answerKind") or ln.get("kind")
            out.write("%s answered: %s\n" % (did, row.get("answer")))
            return answer_code(row.get("answer"), kind, row.get("options"))
        time.sleep(0.3)
    out.write("no answer yet for %s after %ds\n" % (did, timeout))
    return 2


# ---------------------------------------------------------------- refresh all

def _run_source(run):
    argv = run if isinstance(run, list) else shlex.split(expand(run))
    p = subprocess.run([expand(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=60)
    return p.stdout


def _docs_from_lines(text):
    docs = []
    for ln in text.split("\n"):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            docs.append({"text": ln.strip(), "at": None, "id": None})
            continue
        if isinstance(rec, dict):
            t = rec.get("text") or rec.get("verbatim") or rec.get("message") or rec.get("quote")
            if t:
                docs.append({"text": str(t), "at": rec.get("at"), "id": rec.get("id") or rec.get("refersTo")})
    return docs


def _in_source(quote, raw):
    return bool(quote) and (quote in raw or json.dumps(quote)[1:-1] in raw)


def refresh_all(cfg=None):
    cfg = cfg or load_config()
    cards, _ = open_cards(include_snoozed=True)
    frm = sender()
    ids = ", ".join(c["id"] for c in cards) or "none"
    request = mesh.deliver(frm, mesh.orch_name(), (
        "[DECISIONS UI] refresh all: please ask every live lane to re-sync (git, CI, tracker, whatever its goal "
        "file says) and report back. Open calls: %s." % ids))
    found, ungrounded, errors = {}, 0, []
    for src in cfg.get("refreshSources") or []:
        kind, label = src.get("kind"), src.get("label") or src.get("kind")
        try:
            if kind in ("jsonl", "file", "text"):
                with open(expand(src["path"])) as f:
                    raw = f.read()
                docs = _docs_from_lines(raw)
                label = src.get("label") or os.path.basename(src["path"])
            elif kind == "command":
                raw = _run_source(src["run"])
                docs = _docs_from_lines(raw)
            elif kind == "candidates":
                for ln in _run_source(src["run"]).split("\n"):
                    try:
                        cand = json.loads(ln)
                    except ValueError:
                        continue
                    if not isinstance(cand, dict) or not any(c["id"] == cand.get("id") for c in cards):
                        continue
                    try:
                        with open(expand(cand.get("path") or "")) as f:
                            ground = f.read()
                    except OSError:
                        ground = ""
                    if not _in_source(cand.get("quote"), ground):
                        ungrounded += 1
                        continue
                    found.setdefault(cand["id"], {"kind": cand.get("kind") or "answer", "quote": cand["quote"],
                                                  "text": cand.get("text") or cand["quote"], "at": cand.get("at"),
                                                  "source": label})
                continue
            else:
                errors.append("unknown source kind %r" % kind)
                continue
        except (OSError, KeyError, subprocess.TimeoutExpired, ValueError) as e:
            errors.append("%s: %s" % (label, e))
            continue
        for card in cards:
            if card["id"] in found:
                continue
            pat = re.compile(r"(?<![A-Za-z0-9])%s(?![0-9])" % re.escape(card["id"]))
            hits = [d for d in docs if d["id"] == card["id"] or pat.search(d["text"])]
            if not hits:
                continue
            d = sorted(hits, key=lambda x: x.get("at") or "")[-1]
            if not _in_source(d["text"], raw):
                ungrounded += 1
                continue
            found[card["id"]] = {"kind": "answer", "quote": d["text"], "text": d["text"], "at": d.get("at"),
                                 "source": label}
    # exact repeats of a past answered call, grounded in the decisions log
    try:
        with open(mesh.decisions_log_path()) as f:
            lograw = f.read()
    except OSError:
        lograw = ""
    for card in cards:
        if card["id"] in found:
            continue
        for h in find(card["question"]):
            if h["exact"] and h["id"] != card["id"] and h.get("verbatim"):
                if _in_source(h["verbatim"], lograw):
                    found[card["id"]] = {"kind": "answer", "quote": h["verbatim"], "text": h["verbatim"],
                                         "at": h.get("at"), "source": "decisions log (%s)" % h["id"]}
                else:
                    ungrounded += 1
                break
    rows = [{"id": c["id"], "from": c["from"], "question": c["question"], "b": found[c["id"]], "choice": None,
             "state": "pending"} for c in cards if c["id"] in found]
    res = {"at": mesh.now(), "request": request, "rows": rows, "ungrounded": ungrounded, "errors": errors}
    mesh._atomic_write(state_path("decisions-refresh.json"), json.dumps(res, indent=2) + "\n")
    return res


def _refresh_doc():
    d = _read_json(state_path("decisions-refresh.json"), None)
    if not d:
        raise NotFound("no refresh results: run refresh first")
    return d


def _row(doc, cid):
    for r in doc["rows"]:
        if r["id"] == cid:
            return r
    raise NotFound("no refresh row for %s" % cid)


def refresh_choose(cid, choice):
    if choice not in ("A", "B"):
        raise ValueError("choice must be A or B")
    lk = _lock()
    try:
        doc = _refresh_doc()
        r = _row(doc, cid)
        if r["state"] != "pending":
            raise AlreadyAnswered("%s row is already %s" % (cid, r["state"]))
        r["choice"] = choice
        mesh._atomic_write(state_path("decisions-refresh.json"), json.dumps(doc, indent=2) + "\n")
        return {"ok": True, "id": cid, "choice": choice}
    finally:
        lk.close()


def refresh_confirm(cid):
    lk = _lock()
    try:
        doc = _refresh_doc()
        r = _row(doc, cid)
        if r["state"] != "pending":
            raise AlreadyAnswered("%s row is already %s" % (cid, r["state"]))
        if r.get("choice") != "B":
            raise ValueError("confirm needs a prior choice of B for %s (choice is %s)" % (cid, r.get("choice")))
    finally:
        lk.close()
    b = r["b"]
    if b["kind"] == "revise":
        res = revise(cid, b["text"])
    else:
        res = pick(cid, text=b["quote"], kind="found", extra={"source": b["source"], "quoteAt": b.get("at")})
    lk = _lock()
    try:
        doc = _refresh_doc()
        _row(doc, cid)["state"] = "applied"
        mesh._atomic_write(state_path("decisions-refresh.json"), json.dumps(doc, indent=2) + "\n")
    finally:
        lk.close()
    return res


# ---------------------------------------------------------------- role guard

# ---------------------------------------------------------------- four desks: WATCH source, task gate, learn this
#
# Spec: docs/design/four-desks.md 2.3, 2.4, 2.5 and 3.7. Every WATCH item reaches DECIDE first (Q88). An fyi DECIDE
# passes to ROUTE alone (a mesh message, never a write) and logs a decidedBy decide:probe row. A task candidate
# becomes a call for the human, and ROUTE hears of it only as `TASK ... by human:<Qn>` after a yes on that call.

WATCH_KEYS = ("sourceKind", "source", "ref", "url", "eid")
TASK_KINDS = ("task-candidate", "probe")  # call kinds whose yes forwards a TASK to ROUTE
TASK_OPTIONS = ["yes: start a writing lane", "learn only", "no: drop"]
_URL_OK = re.compile(r"^https?://[^\s\"'<>\\]+$", re.I)
_EID_OK = re.compile(r"^[A-Za-z0-9][^\s\"]{0,279}$")
_TASK_LINE = re.compile(r'^TASK-CANDIDATE\s+(\S+)\s+(\S+)\s+"(.*)"\s+(\S+)\s*$')
_FYI_LINE = re.compile(r"^FYI\s+(\d+)\s+events?:\s*(.*?)\s*$")


def clean_watch(w):
    """The WATCH object a call may carry: WATCH_KEYS only, one-line strings, and a url only when it is http(s)."""
    if not isinstance(w, dict):
        return None
    out = {}
    for k in WATCH_KEYS:
        v = w.get(k)
        if isinstance(v, str) and v.strip() and len(v) <= 500 and "\n" not in v:
            out[k] = v.strip()
    if "url" in out and not _URL_OK.match(out["url"]):
        del out["url"]
    return out if (out.get("sourceKind") or out.get("ref")) else None


def decide_dir():
    return state_path("decide")


def _meta_path():
    return os.path.join(decide_dir(), "calls.json")


def calls_meta():
    d = _read_json(_meta_path(), {})
    return d if isinstance(d, dict) else {}


def _calls_lock():
    os.makedirs(decide_dir(), exist_ok=True)
    f = open(os.path.join(decide_dir(), ".calls.lock"), "a+")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def _update_meta(cid, fields):
    lk = _calls_lock()
    try:
        m = calls_meta()
        m.setdefault(cid, {}).update(fields)
        mesh._atomic_write(_meta_path(), json.dumps(m, indent=2, sort_keys=True) + "\n")
        return dict(m[cid])
    finally:
        lk.close()


def _clean_task(task):
    if not isinstance(task, dict) or not isinstance(task.get("eid"), str) or not _EID_OK.match(task["eid"]):
        raise ValueError("a task needs an eid (a WATCH event id, or probe:<id>)")
    t = {k: str(task[k]) for k in ("eid", "ref", "quote", "url") if task.get(k)}
    if t.get("url") and not _URL_OK.match(t["url"]):
        del t["url"]
    return t


def register_call(cid, kind, watch=None, task=None, **extra):
    """Record DECIDE's own metadata for call `cid` in $FLEET_STATE/decide/calls.json and return the stored row.

    kind: task-candidate (a WATCH TASK-CANDIDATE, opened by intake()), probe (a "found X, start a lane?" call opened by
    fleet_probe) or ask (a WATCH ask, for its badge only). watch: {sourceKind, source, ref, url, eid}. task (task kinds
    only): {eid, ref, quote, url}, what a yes forwards to ROUTE. extra keys (target, probeId, ...) are stored as-is."""
    if not cid or not re.match(r"^Q\d+$", str(cid)):
        raise ValueError("register_call needs a call id like Q12, got %r" % (cid,))
    if kind not in TASK_KINDS + ("ask",):
        raise ValueError("kind must be one of %s" % ", ".join(TASK_KINDS + ("ask",)))
    fields = dict(extra)
    fields["kind"] = kind
    w = clean_watch(watch)
    if w:
        fields["watch"] = w
    if task is not None:
        fields["task"] = _clean_task(task)
    elif kind in TASK_KINDS and not (calls_meta().get(cid) or {}).get("task"):
        raise ValueError("a %s call needs task {eid, ref, quote, url}" % kind)
    return _update_meta(cid, fields)


def watch_of(cid, call=None, meta=None):
    """The WATCH object for a call: the decisions.jsonl row's own `watch`, else DECIDE's sidecar entry."""
    w = (call or {}).get("watch")
    if w:
        return w
    m = (meta if meta is not None else calls_meta()).get(cid) or {}
    return clean_watch(m.get("watch"))


def watch_event(eid):
    """The latest watch/events.jsonl row with this eid, or None."""
    hit = None
    for rec in mesh._parse_lines(state_path(os.path.join("watch", "events.jsonl"))):
        if rec.get("eid") == eid:
            hit = rec
    return hit


def choice_of(answer, options, kind):
    """Which option an answer is: the option's first word ("yes", "no", "nuke", "learn") when it was picked or typed
    back exactly; "own" / "refuse" otherwise. An own answer that merely contains yes is never a yes."""
    if kind == "refuse":
        return "refuse"
    if kind not in ("pick", "desk"):
        return "own"
    a = (answer or "").strip()
    m = re.match(r"^\(?([a-z])\)?\s*:\s*(.+)$", a, re.I)
    body = mesh._norm(m.group(2) if m else a)
    for o in options:
        t = mesh._norm(o["text"])
        if body and (body == t or body == t.split(" ")[0]):
            return t.split(" ")[0]
    return "own"


def _task_text(cid, t):
    return 'TASK %s %s "%s" %s by human:%s' % (t["eid"], t.get("ref") or "-", (t.get("quote") or "").replace('"', "'"),
                                               t.get("url") or "-", cid)


def _probe_hook(cid, choice, meta):
    try:
        import fleet_probe
        hook = getattr(fleet_probe, "on_pick", None)
    except ImportError:
        return "fleet_probe not installed"
    if hook is None:
        return "fleet_probe has no on_pick"
    try:
        hook(cid, choice, meta)
        return None
    except Exception as e:  # the answer stands; the hook's failure is a note, never a second relay
        mesh.log("decisions-ui probe hook %s failed: %s" % (cid, e))
        return "fleet_probe.on_pick failed: %s" % e


def _settle_task(cid, tmeta, verbatim, kind, options):
    """Run once per task call, after it is marked answered, under the pick lock. A yes forwards the TASK to ROUTE;
    "learn only" adds it to LEARN; anything else sends nothing. Returns (deliver result or None, notes)."""
    if tmeta.get("settledAt"):
        return None, []
    choice = choice_of(verbatim, options, kind)
    notes, res, fields = [], None, {"settledAt": mesh.now(), "choice": choice}
    if choice == "yes":
        res = mesh.deliver(mesh.desk_name(), mesh.orch_name(), _task_text(cid, tmeta["task"]))
        fields.update({"forwardedAt": mesh.now(), "forwardResult": res})
    elif choice == "learn" and tmeta.get("kind") == "task-candidate":
        try:
            fields["lid"] = _learn_outside(cid, tmeta)["lid"]
        except ValueError as e:
            notes.append(str(e))
    if tmeta.get("kind") == "probe":
        n = _probe_hook(cid, choice, tmeta)
        if n:
            notes.append(n)
    _update_meta(cid, fields)
    mesh.log("decisions-ui task %s choice=%s forwarded=%s" % (cid, choice, bool(res)))
    return res, notes


def forward_answered():
    """Settle every task call answered outside pick() (the desk pane's decision_answer): idempotent.

    Fails closed: decision_answer has no role check, so an answer counts only when its log row says the desk gave it
    (`by` == $FLEET_DECIDE, written by the mesh patch in LANDER-NOTES-fd-decide-ui.md). Any other answer is reported
    as `unverified`, left unsettled and forwards nothing; answer task calls with `fleet decisions pick` instead."""
    lk = _lock()
    try:
        rows = {d["id"]: d for d in mesh.decisions()}
        logs = latest_log()
        out = []
        for cid, m in sorted(calls_meta().items(), key=lambda kv: _idnum(kv[0])):
            d = rows.get(cid)
            if m.get("kind") not in TASK_KINDS or m.get("settledAt") or not d or d.get("status") != "answered":
                continue
            by = (logs.get(cid) or {}).get("by")
            if by != mesh.desk_name():
                out.append({"id": cid, "forwarded": False, "result": None, "unverified": True,
                            "notes": ["answered outside a pick by %s, not the desk: not forwarded; ask again with "
                                      "`fleet decisions pick`" % (by or "an unrecorded sender")]})
                continue
            kind = d.get("answerKind") or "desk"
            res, notes = _settle_task(cid, m, d.get("answer"), kind, build_options(d.get("options") or []))
            out.append({"id": cid, "forwarded": bool(res), "result": res, "notes": notes})
        return out
    finally:
        lk.close()


def _forwarded_fyi():
    d = _read_json(os.path.join(decide_dir(), "fyi-forwarded.json"), {})
    return d if isinstance(d, dict) else {}


def intake(text):
    """DECIDE's handling of WATCH inbox lines (3.7).

    FYI <n> events: <eid>...      -> ROUTE at once, alone (mesh only), one decidedBy decide:probe log row
    TASK-CANDIDATE <eid> <ref> "<quote>" <url>  -> a call for the human; ROUTE gets nothing until a yes
    Each eid is handled once. Unknown lines are skipped and listed."""
    import fleet_probe_contract as probe_c
    out = {"fyi": [], "calls": [], "skipped": [], "settled": forward_answered()}
    desk, orch = mesh.desk_name(), mesh.orch_name()
    os.makedirs(decide_dir(), exist_ok=True)
    lk = open(os.path.join(decide_dir(), ".intake.lock"), "a+")  # one intake at a time: two wakes must not open
    fcntl.flock(lk.fileno(), fcntl.LOCK_EX)  # the same candidate twice
    try:
        _intake_lines(text, out, desk, orch, probe_c)
    finally:
        lk.close()
    return out


def _intake_lines(text, out, desk, orch, probe_c):
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _TASK_LINE.match(line)
        if m:
            eid, ref, quote, url = m.groups()
            if not _EID_OK.match(eid):
                out["skipped"].append(line[:120])
                continue
            dup = [c for c, mm in calls_meta().items() if (mm.get("task") or {}).get("eid") == eid]
            if dup:
                out["skipped"].append("%s (already %s)" % (eid, dup[0]))
                continue
            ev = watch_event(eid) or {}
            parts = eid.split(":")
            w = clean_watch({"sourceKind": ev.get("sourceKind") or parts[0],
                             "source": ev.get("source") or (parts[1] if len(parts) > 1 else ""),
                             "ref": ev.get("ref") or ref, "url": ev.get("url") or url, "eid": eid}) or {}
            q = '%s %s: start a writing lane? "%s"' % (w.get("source") or w.get("sourceKind") or "watch", ref, quote)
            res = mesh.t_decision_open({"from": desk, "question": q, "options": TASK_OPTIONS,
                                        "allow": ["pick", "snooze", "refuse"], "watch": w})
            cid = re.match(r"opened (Q\d+)", res).group(1)
            register_call(cid, "task-candidate", watch=w, task={"eid": eid, "ref": ref, "quote": quote,
                                                                 "url": w.get("url")})
            out["calls"].append(cid)
            continue
        m = _FYI_LINE.match(line)
        if m:
            seen = _forwarded_fyi()
            eids = [e for e in m.group(2).split() if _EID_OK.match(e)]
            fresh = [e for e in eids if e not in seen]
            if not fresh:
                out["skipped"].append("FYI (all %d already forwarded)" % len(eids))
                continue
            msg = "FYI %d events: %s decidedBy: decide:probe" % (len(fresh), " ".join(fresh))
            res = mesh.deliver(desk, orch, msg)
            row = probe_c.log_row("fyi-forward", mesh.now(), "watch", verbatim=msg, relayed_to=[orch])
            row.update({"eids": fresh, "delivery": {orch: res}})
            errs = probe_c.validate_log_row(row)
            if errs:
                raise ValueError("fyi-forward row invalid: %s" % "; ".join(errs))
            mesh._append_line(mesh.decisions_log_path(), json.dumps(row))
            seen.update({e: row["at"] for e in fresh})
            mesh._atomic_write(os.path.join(decide_dir(), "fyi-forwarded.json"),
                               json.dumps(seen, indent=2, sort_keys=True) + "\n")
            out["fyi"].append({"eids": fresh, "result": res})
            continue
        out["skipped"].append(line[:120])


# ---- learn this (2.4): one LEARN ledger row with the human's verbatim words, through fleet_learn's API

def _learn():
    try:
        import fleet_learn
    except ImportError:
        raise ValueError("LEARN is not installed here (lib/fleet_learn.py missing); learn this needs fleet-learn")
    return fleet_learn


def learned_ids():
    """{call id: lid} for calls already copied to LEARN by learn this; {} when LEARN is absent."""
    try:
        fl = _learn()
        rows = fl.items(include_retired=True)
    except Exception:
        return {}
    out = {}
    for r in rows:
        src = r.get("source") or {}
        if src.get("type") == "decide-learn-this" and src.get("ref"):
            out[src["ref"]] = r.get("lid")
    return out


def _links(w):
    if not w or not w.get("ref"):
        return []
    link = {"type": w.get("sourceKind") or "watch", "ref": w["ref"]}
    for k in ("eid", "url"):
        if w.get(k):
            link[k] = w[k]
    return [link]


def _add(fl, **kw):
    try:
        return fl.add_item(**kw)
    except Exception as e:  # fleet_learn.Refused and friends: one line, no traceback
        raise ValueError("LEARN refused: %s" % e)


def learn_this(cid=None):
    """Copy one answered call to LEARN: kind decision, origin fleet, quote = the human's verbatim answer from the
    decisions log. cid None = the newest called row. A second press on the same call adds nothing."""
    lk = _lock()
    try:
        rows = called_list()
        if cid:
            row = next((r for r in rows if r["id"] == cid), None)
            if row is None:
                c = all_calls().get(cid)
                if c and c.get("open"):
                    raise ValueError("%s is still open; answer it first" % cid)
                raise NotFound("no answered call %s" % cid)
        elif rows:
            row = rows[0]
        else:
            raise NotFound("no answered call to learn")
        if row.get("learned"):
            raise AlreadyAnswered("%s is already in LEARN as %s" % (row["id"], row["learned"]))
        verbatim = row.get("verbatim") or ""
        if not verbatim.strip():
            raise ValueError("%s has no answer words in the decisions log" % row["id"])
        fl = _learn()
        lr = _add(fl, kind="decision", origin="fleet", quote=verbatim, title=row.get("question") or "",
                  source={"type": "decide-learn-this", "ref": row["id"], "path": "$FLEET_STATE/decisions-log.jsonl"},
                  links=_links(row.get("watch")), by="decide")
        mesh.log("decisions-ui learn this %s -> %s" % (row["id"], lr.get("lid")))
        return {"ok": True, "id": row["id"], "lid": lr.get("lid"), "quote": verbatim}
    finally:
        lk.close()


def _learn_outside(cid, tmeta):
    """"learn only" on a task candidate: the WATCH item goes to LEARN as outside knowledge, nothing to ROUTE."""
    fl = _learn()
    t = tmeta.get("task") or {}
    ev = watch_event(t.get("eid")) if t.get("eid") else None
    if ev and hasattr(fl, "add_learn_item"):
        try:
            r = fl.add_learn_item(ev)
        except Exception as e:
            raise ValueError("LEARN refused: %s" % e)
        if r:
            return r
    return _add(fl, kind="change", origin="outside", quote=t.get("quote") or t.get("eid"),
                source={"type": "watch", "ref": t.get("eid")}, title=t.get("ref") or "",
                links=_links(tmeta.get("watch")), by="decide")


def human_only(verb):
    role = os.environ.get("FLEET_ROLE") or ""
    if role not in ("", "decide"):
        raise PermissionError("role %s can't %s: only the human (or DECIDE) answers calls" % (role, verb))


# ---------------------------------------------------------------- web server

class Hub:
    """Watches the state files; wakes SSE clients on change; notifies on new open calls."""

    def __init__(self, cfg, url_for):
        self.cfg, self.url_for = cfg, url_for
        self.cond = threading.Condition()
        self.version, self.sig = 0, None
        self.known = None
        self.payload = "{}"

    def files(self):
        return [mesh._decisions_path(), mesh.decisions_log_path(), mesh.reports_path(), goal_path(),
                state_path("decisions-ui-state.json"), state_path("decisions-refresh.json"),
                state_path("roster.json"), _meta_path(), state_path(os.path.join("learn", "ledger.jsonl"))]

    def signature(self):
        out = []
        for p in self.files():
            try:
                st = os.stat(p)
                out.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((p, 0, 0))
        return out

    def tick(self):
        sig = self.signature()
        if sig == self.sig:
            return
        self.sig = sig
        try:
            snap = snapshot()
        except Exception as e:  # a half-written file: try again next tick
            mesh.log("decisions-ui snapshot error: %s" % e)
            self.sig = None
            return
        ids = {c["id"] for c in snap["open"]}
        if self.known is not None:
            for c in snap["open"]:
                if c["id"] not in self.known:
                    self.notify(c)
        self.known = ids
        with self.cond:
            self.version += 1
            self.payload = json.dumps(snap)
            self.cond.notify_all()

    def notify(self, card):
        cmd = self.cfg.get("notifyCommand")
        if not cmd or not self.cfg.get("notify", True):
            return
        title = "DECISIONS: %s from %s" % (card["id"], card["from"])
        body = (card["question"] or "").split("\n")[0][:200]
        url = self.url_for(card["id"])
        argv = [str(a).replace("{title}", title).replace("{body}", body).replace("{url}", url).replace(
            "{id}", card["id"]) for a in (cmd if isinstance(cmd, list) else shlex.split(cmd))]
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            mesh.log("decisions-ui notify failed: %s" % e)

    def loop(self, stop):
        while not stop.is_set():
            self.tick()
            stop.wait(1.0)


def make_handler(token, hub, allowed_hosts):
    with open(HTML) as f:
        page = f.read().encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "fleet-decisions"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # never log the token (it rides in the query string)
            mesh.log("decisions-ui %s %s" % (self.command, urllib.parse.urlsplit(self.path).path))

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str)
                                                         else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype == "text/html":
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return (self.headers.get("Host") or "") in allowed_hosts

        def _token_ok(self, allow_query):
            got = self.headers.get("X-Fleet-Token") or ""
            if not got and allow_query:
                got = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t") or [""])[0]
            return bool(got) and hmac.compare_digest(got, token)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._host_ok() or not self._token_ok(allow_query=True):
                return self._send(403, {"error": "forbidden"})
            if path in ("/", "/refresh"):
                return self._send(200, page, "text/html")
            if path == "/api/state":
                return self._send(200, snapshot())
            if path == "/api/refresh":
                return self._send(200, _read_json(state_path("decisions-refresh.json"), {"rows": []}))
            if path == "/events":
                return self.events()
            return self._send(404, {"error": "not found"})

        def events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            seen = -1
            try:
                while True:
                    with hub.cond:
                        if hub.version == seen:
                            hub.cond.wait(15)
                        v, payload = hub.version, hub.payload
                    if v != seen:
                        self.wfile.write(("event: state\ndata: %s\n\n" % payload).encode("utf-8"))
                        seen = v
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self._host_ok() or not self._token_ok(allow_query=False) or (
                    origin and origin != "http://%s" % self.headers.get("Host")):
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/api/pick":
                    res = pick(body.get("id"), option=body.get("option"), text=body.get("text"),
                               kind=body.get("kind"), refuse=bool(body.get("refuse")), via="ui")
                elif path == "/api/retry":
                    res = retry(body.get("id"))
                elif path == "/api/learn":
                    res = learn_this(body.get("id"))
                elif path == "/api/snooze":
                    res = snooze(body.get("id"), body.get("minutes") or 30)
                elif path == "/api/refresh":
                    res = refresh_all()
                elif path == "/api/refresh/choose":
                    res = refresh_choose(body.get("id"), body.get("choice"))
                elif path == "/api/refresh/confirm":
                    res = refresh_confirm(body.get("id"))
                else:
                    return self._send(404, {"error": "not found"})
            except AlreadyAnswered as e:
                return self._send(409, {"error": str(e)})
            except NotFound as e:
                return self._send(404, {"error": str(e)})
            except (ValueError, PermissionError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                mesh.log("decisions-ui %s error: %s" % (path, e))
                return self._send(500, {"error": str(e)})
            hub.tick()
            return self._send(200, res)

    return H


def serve(port, bind, open_page):
    human_only("serve the DECISIONS UI")
    if bind not in LOOPBACK:
        raise ValueError("--bind %s refused: the DECISIONS UI binds loopback only (127.0.0.1 or ::1)" % bind)
    cfg = load_config()
    token = secrets.token_urlsafe(24)
    port = cfg.get("port", 7340) if port is None else port
    srv_cls = http.server.ThreadingHTTPServer
    if bind == "::1":
        import socket

        class V6(srv_cls):
            address_family = socket.AF_INET6
        srv_cls = V6
    holder = {}

    def url_for(cid=None):
        return "http://%s:%d/?t=%s%s" % ("[::1]" if bind == "::1" else "127.0.0.1", holder["port"], token,
                                         "#" + cid if cid else "")

    hub = Hub(cfg, url_for)
    # allowed Host headers are filled in once the port is known
    allowed = set()
    try:
        srv = srv_cls(("localhost" if bind == "localhost" else bind, port), make_handler(token, hub, allowed))
    except OSError as e:
        raise ValueError("can't listen on %s:%s (%s); pick another --port or set port in decisions-ui.json" % (
            bind, port, e.strerror or e))
    srv.daemon_threads = True
    holder["port"] = srv.server_address[1]
    for h in ("127.0.0.1", "localhost", "[::1]"):
        allowed.add("%s:%d" % (h, holder["port"]))
    url = url_for()
    urlfile = state_path("decisions-ui.url")
    os.makedirs(os.path.dirname(urlfile), exist_ok=True)
    fd = os.open(urlfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(url + "\n")
    sys.stdout.write("DECISIONS UI: %s\n" % url)
    sys.stdout.flush()
    stop = threading.Event()
    hub.tick()
    threading.Thread(target=hub.loop, args=(stop,), daemon=True).start()
    if open_page:
        opener = os.environ.get("FLEET_OPEN_CMD") or ("open -g" if sys.platform == "darwin" else "xdg-open")
        subprocess.Popen(shlex.split(opener) + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import signal

    def _term(signum, frame):  # SIGTERM (kill, tmux kill-pane) cleans up like Ctrl-C: the URL file goes away
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        try:
            os.remove(urlfile)
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------- terminal fallback

HELP = """keys: j/k or arrows move | a/b/c/d pick that option | o own answer | e edit the rec option
      x refuse | s snooze 30m | r refresh all | v called list | t retry failed relays
      l [id] learn this: copy an answered call (newest, or id) to LEARN in your own words | ? help | q quit"""


class Tui:
    def __init__(self, out):
        self.out, self.cursor = out, 0

    def cards(self):
        return open_cards()[0]

    def render_card(self, c, sel):
        lines = ["%s %s  from %s  %s%s" % (">" if sel else " ", c["id"], c["from"], c["ago"],
                                          ("   also asked by: " + ", ".join(c["alsoAskedBy"])) if c["alsoAskedBy"]
                                          else "")]
        lines.append("    " + c["question"])
        w = c.get("watch")
        if w:
            lines.append("    [%s] %s%s" % (w.get("sourceKind") or "watch", w.get("ref") or "",
                                          ("  " + w["url"]) if w.get("url") else ""))
        if c.get("conflict"):
            lines.append("    !! %s; answer it in the desk pane" % c["conflict"])
        for o in c["options"]:
            lines.append("    [%s%s] %s%s" % (o["letter"], ", rec" if o["rec"] else "", o["text"],
                                             ("   (downside: %s)" % o["downside"]) if o["downside"] else ""))
        return lines

    def screen(self):
        cards = self.cards()
        self.cursor = max(0, min(self.cursor, len(cards) - 1))
        snap_called = called_list()
        lines = ["DECISIONS   %d open | %d called" % (len(cards), len(snap_called))]
        if not cards:
            lines.append("  no open calls")
        for i, c in enumerate(cards):
            lines += self.render_card(c, i == self.cursor)
        return lines

    def called_lines(self):
        rows = called_list()
        lines = ["Called (%d)" % len(rows)]
        for r in rows:
            b = {"relayed": "relayed %s" % r["time"], "queued": "queued (no live lane)", "reopened": "reopened",
                 "failed": "relay FAILED [t retry]"}[r["badge"]]
            lines.append('  %s%s  "%s"  -> %s  %s%s%s%s' % (
                r["id"], ("  [%s]" % (r["watch"].get("sourceKind") or "watch")) if r.get("watch") else "",
                r["verbatim"], ", ".join(r["relayedTo"]) or "?", b, ("  " + r["acted"]) if r["acted"] else "",
                ("  TASK -> %s" % r["forwarded"]) if r.get("forwarded") else "",
                ("  learned %s" % r["learned"]) if r.get("learned") else ""))
        return lines

    def current(self):
        cards = self.cards()
        if not cards:
            return None
        self.cursor = max(0, min(self.cursor, len(cards) - 1))
        return cards[self.cursor]

    def result_line(self, res):
        return "%s %s -> %s" % (res["id"], res.get("verbatim", ""), "; ".join(
            "%s %s" % (k, classify(v)) for k, v in res.get("delivery", {}).items()))

    def do(self, key, arg=None):
        """Run one command; returns output lines (or None to quit)."""
        c = self.current()
        try:
            if key in ("q", "quit"):
                return None
            if key in ("j", "down"):
                self.cursor += 1
                return self.screen()
            if key in ("k", "up"):
                self.cursor = max(0, self.cursor - 1)
                return self.screen()
            if key == "?":
                return HELP.split("\n")
            if key == "v":
                return self.called_lines()
            if key == "r":
                res = refresh_all()
                out = ["refresh: %s; %d found, ungrounded: %d" % (res["request"], len(res["rows"]), res["ungrounded"])]
                for r in res["rows"]:
                    out.append('  %s  B: "%s" (%s)  approve: fleet decisions refresh choose %s B, then refresh '
                               'confirm %s' % (r["id"], r["b"]["quote"], r["b"]["source"], r["id"], r["id"]))
                return out
            if key == "l":
                res = learn_this(arg or None)
                return ["learn this: %s -> LEARN %s \"%s\"" % (res["id"], res["lid"], res["quote"])]
            if key == "t":
                outs = [self.result_line(retry(r["id"])) for r in called_list() if r["retry"]]
                return outs or ["nothing to retry"]
            if c is None:
                return ["no open calls"]
            if key in ("a", "b", "c", "d"):
                return [self.result_line(pick(c["id"], option=key, via="ui", frontend="tui"))] + self.screen()
            if key in ("o", "e"):
                if not arg:
                    return ["%s needs text" % key]
                return [self.result_line(pick(c["id"], text=arg, kind="own" if key == "o" else "edit",
                                              via="ui", frontend="tui"))] + self.screen()
            if key == "x":
                return [self.result_line(pick(c["id"], text=arg or None, refuse=True, via="ui", frontend="tui"))] + self.screen()
            if key == "s":
                snooze(c["id"], 30)
                return ["snoozed %s for 30m" % c["id"]] + self.screen()
        except (ValueError, PermissionError) as e:  # AlreadyAnswered and NotFound are ValueErrors too
            return ["error: %s" % e]
        return ["unknown key %r; ? for help" % key]


def tui_plain(stdin, out):
    t = Tui(out)
    out.write("\n".join(t.screen()) + "\n")
    for raw in stdin:
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        key, _, arg = raw.strip().partition(" ")
        res = t.do(key, arg or None)
        if res is None:
            break
        out.write("\n".join(res) + "\n")
        out.flush()
    return 0


def tui_gum_one():
    cards = open_cards()[0]
    if not cards:
        print("no open calls")
        return 0
    c = cards[0]
    labels = ["%s%s: %s" % (o["letter"], " (rec)" if o["rec"] else "", o["text"]) for o in c["options"]]
    labels += ["own answer...", "snooze 30m", "skip"]
    p = subprocess.run(["gum", "choose", "--header", "%s from %s: %s" % (c["id"], c["from"], c["question"])] + labels,
                       stdout=subprocess.PIPE, universal_newlines=True)
    sel = p.stdout.strip()
    if not sel or sel == "skip":
        return 0
    if sel == "snooze 30m":
        snooze(c["id"], 30)
        return 0
    if sel == "own answer...":
        q = subprocess.run(["gum", "input", "--placeholder", "own answer"], stdout=subprocess.PIPE,
                           universal_newlines=True)
        if not q.stdout.strip():
            return 0
        res = pick(c["id"], text=q.stdout.strip(), via="ui", frontend="tui")
    else:
        res = pick(c["id"], option=sel[0], via="ui", frontend="tui")
    print(Tui(sys.stdout).result_line(res))
    return 0


def tui_curses(one=False):
    import curses

    def prompt(scr, label):
        h, w = scr.getmaxyx()
        scr.move(h - 1, 0)
        scr.clrtoeol()
        scr.addstr(h - 1, 0, label[:w - 1])
        curses.echo()
        curses.curs_set(1)
        scr.timeout(-1)  # block: under the 1 s refresh timeout getstr returns early and the rest typed runs as keys
        try:
            s = scr.getstr(h - 1, min(len(label), w - 2), 400).decode("utf-8", "replace")
        finally:
            scr.timeout(1000)
            curses.noecho()
            curses.curs_set(0)
        return s.strip()

    def main(scr):
        curses.curs_set(0)
        scr.timeout(1000)
        t = Tui(None)
        msg, view = ["? for help"], "open"
        while True:
            lines = t.called_lines() if view == "called" else t.screen()
            if one and view == "open":  # popup mode: the newest call only
                cards = t.cards()
                t.cursor = 0
                lines = lines[:1] + (t.render_card(cards[0], True) if cards else ["  no open calls"])
            scr.erase()
            h, w = scr.getmaxyx()
            for i, ln in enumerate((lines + [""] + msg)[:h - 1]):
                attr = curses.A_BOLD if ln.startswith(">") or i == 0 else 0
                try:
                    scr.addstr(i, 0, ln[:w - 1], attr)
                except curses.error:
                    pass
            scr.refresh()
            ch = scr.getch()
            if ch == -1:
                continue
            key = {curses.KEY_DOWN: "down", curses.KEY_UP: "up"}.get(ch, chr(ch) if 0 <= ch < 256 else "")
            if key == "v":
                view = "open" if view == "called" else "called"
                continue
            arg = None
            if key == "o":
                arg = prompt(scr, "own answer (Enter sends, empty cancels): ")
                if not arg:
                    continue
            elif key == "l":
                rows = called_list()
                arg = prompt(scr, "learn this, call id [%s]: " % (rows[0]["id"] if rows else "none")) or None
            elif key == "e":
                cur = t.current()
                rec = cur["options"][0]["text"] if cur and cur["options"] else ""
                arg = prompt(scr, "edit [%s]: " % rec) or None
                if not arg:
                    continue
            res = t.do(key, arg)
            if res is None:
                return
            msg = res[:1] if key not in ("?",) else res
            if one and key in ("a", "b", "c", "d", "o", "e", "x", "s") and not res[0].startswith("error"):
                return

    # mesh logs each delivery to stderr; on a curses screen that scribbles over the cards, so park it in a file
    err = open(state_path("decisions-tui.log"), "a")
    saved = os.dup(2)
    os.dup2(err.fileno(), 2)
    try:
        curses.wrapper(main)
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        err.close()
    return 0


# ---------------------------------------------------------------- CLI

def _print(res, as_json=False):
    if as_json:
        print(json.dumps(res, indent=2))
    else:
        print(json.dumps(res))


def state_text(snap):
    t = Tui(None)
    lines = ["DECISIONS   %d open | %d called | %d snoozed" % (snap["openCount"], snap["calledCount"],
                                                                snap["snoozed"])]
    for c in snap["open"]:
        lines += t.render_card(c, False)
    return "\n".join(lines + t.called_lines())


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(__doc__.split("\n\n", 1)[1])
        return 0 if args else 2
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "serve":
            port, bind, op = None, "127.0.0.1", False
            i = 0
            while i < len(rest):
                if rest[i] == "--port":
                    port = int(rest[i + 1])
                    i += 2
                elif rest[i] == "--bind":
                    bind = rest[i + 1]
                    i += 2
                elif rest[i] == "--open":
                    op = True
                    i += 1
                else:
                    raise ValueError("unknown serve flag %s" % rest[i])
            return serve(port, bind, op)
        if cmd == "tui":
            human_only("answer calls")
            if "--plain" in rest or not sys.stdin.isatty():
                return tui_plain(sys.stdin, sys.stdout)
            if "--one" in rest and shutil.which("gum"):
                return tui_gum_one()
            return tui_curses(one="--one" in rest)
        if cmd == "pick":
            human_only("answer calls")
            if not rest:
                raise ValueError("usage: pick <id> <letter|--text T|--edit T|--refuse [why]>")
            cid, r = rest[0], rest[1:]
            if r[:1] == ["--text"] and len(r) >= 2:
                res = pick(cid, text=r[1], kind="own", via="ui", frontend="cli")
            elif r[:1] == ["--edit"] and len(r) >= 2:
                res = pick(cid, text=r[1], kind="edit", via="ui", frontend="cli")
            elif r[:1] == ["--refuse"]:
                res = pick(cid, text=r[1] if len(r) > 1 else None, refuse=True, via="ui", frontend="cli")
            elif len(r) == 1:
                res = pick(cid, option=r[0], via="ui", frontend="cli")
            else:
                raise ValueError("usage: pick <id> <letter|--text T|--edit T|--refuse [why]>")
            print("%s %s: %s" % (res["id"], res["status"], json.dumps(res["delivery"])))
            for n in res.get("notes") or []:
                sys.stderr.write("WARN %s\n" % n)
            return 0
        if cmd == "learn":
            human_only("learn this")
            _print(learn_this(rest[0] if rest else None))
            return 0
        if cmd == "intake":
            human_only("take in WATCH items")
            text = sys.stdin.read() if (not rest or rest == ["-"]) else " ".join(rest)
            _print(intake(text), as_json=True)
            return 0
        if cmd == "forward":
            human_only("forward answered task calls")
            _print(forward_answered(), as_json=True)
            return 0
        if cmd == "retry":
            human_only("retry relays")
            res = retry(rest[0])
            print("%s %s: %s" % (res["id"], res["status"], json.dumps(res["delivery"])))
            return 0
        if cmd == "snooze":
            human_only("snooze calls")
            _print(snooze(rest[0], float(rest[1]) if len(rest) > 1 else 30))
            return 0
        if cmd == "state":
            snap = snapshot()
            print(json.dumps(snap, indent=2) if "--json" in rest else state_text(snap))
            return 0
        if cmd == "find":
            hits = find(" ".join(rest))
            if not hits:
                print("no past decision matches")
                return 1
            for h in hits:
                print('%s%s [%s] %s -> "%s"' % (h["id"], " (exact repeat)" if h["exact"] else "", h.get("at"),
                                                h.get("question"), h.get("verbatim")))
            return 0
        if cmd == "ask":
            if not rest:
                raise ValueError("usage: ask \"<question>\" [--options a,b] [--wait] [--timeout 30m]")
            q, opts, allow, wait, timeout, new = rest[0], [], None, False, 1800.0, False
            i = 1
            while i < len(rest):
                f = rest[i]
                if f == "--options":
                    opts = [o.strip() for o in rest[i + 1].split(",") if o.strip()]
                    i += 2
                elif f == "--allow":
                    allow = [o.strip() for o in rest[i + 1].split(",") if o.strip()]
                    i += 2
                elif f == "--timeout":
                    timeout = parse_duration(rest[i + 1])
                    i += 2
                elif f == "--wait":
                    wait = True
                    i += 1
                elif f == "--new":
                    new = True
                    i += 1
                else:
                    raise ValueError("unknown ask flag %s" % f)
            return ask(q, opts, allow, wait, timeout, new)
        if cmd == "report":
            if len(rest) < 2 or rest[1] not in ("ok", "fail"):
                raise ValueError("usage: report <id> ok|fail [note]")
            print(mesh.t_decision_report({"id": rest[0], "ok": rest[1] == "ok", "note": " ".join(rest[2:])}))
            return 0
        if cmd == "refresh":
            if rest[:1] == ["choose"]:
                human_only("approve refresh rows")
                _print(refresh_choose(rest[1], rest[2]))
                return 0
            if rest[:1] == ["confirm"]:
                human_only("approve refresh rows")
                res = refresh_confirm(rest[1])
                _print(res)
                return 0
            human_only("refresh all")
            res = refresh_all()
            if "--json" in rest:
                print(json.dumps(res, indent=2))
            else:
                print("refresh: %s" % res["request"])
                for r in res["rows"]:
                    print("  %s  A: %s\n       B: %s \"%s\" (%s, %s)" % (r["id"], r["question"], r["b"]["kind"],
                                                                     r["b"]["quote"], r["b"]["source"], r["b"]["at"]))
                print("  ungrounded: %d" % res["ungrounded"])
            return 0
    except AlreadyAnswered as e:
        sys.stderr.write("already answered: %s\n" % e)
        return EXIT_ALREADY
    except PermissionError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 4
    except (ValueError, NotFound, IndexError) as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    sys.stderr.write("unknown command %s; fleet decisions -h\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
