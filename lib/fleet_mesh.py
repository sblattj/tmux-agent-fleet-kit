#!/usr/bin/env python3
"""fleet-mesh: stdio MCP server + CLI for peer messaging and decisions between fleet lanes.

Python 3.8+ stdlib only. See INTERFACES.md for the contract.
  fleet_mesh.py serve | send <from> <to> <text> | inbox <name> | roster | mirror (hook JSON on stdin)
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.request

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"


# ---------------------------------------------------------------- paths / env

def state_dir():
    s = os.environ.get("FLEET_STATE")
    if s:
        return s
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fleet")


def desk_name():
    """DECIDE desk lane: $FLEET_DECIDE, else DECIDE."""
    return os.environ.get("FLEET_DECIDE") or "DECIDE"


def orch_name():
    """ROUTE desk lane: $FLEET_ROUTE, else ROUTE."""
    return os.environ.get("FLEET_ROUTE") or "ROUTE"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = "%s %s" % (now(), msg)
    try:
        sys.stderr.write("fleet-mesh: %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        d = os.path.join(state_dir(), "log")
        os.makedirs(d, exist_ok=True)
        _append_line(os.path.join(d, "fleet.log"), line)
    except Exception:
        pass


# ---------------------------------------------------------------- file store

def _append_line(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line.rstrip("\n") + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_lines(path):
    try:
        with open(path) as f:
            return [ln for ln in f.read().split("\n") if ln.strip()]
    except FileNotFoundError:
        return []


def _parse_lines(path):
    out = []
    for ln in _read_lines(path):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def load_roster():
    try:
        with open(os.path.join(state_dir(), "roster.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _inbox_path(name):
    return os.path.join(state_dir(), "inbox", "%s.jsonl" % name)


def _read_json(path):
    try:
        with open(path) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _has_fleet(doc, key="mcpServers"):
    servers = doc.get(key)
    return isinstance(servers, dict) and "fleet" in servers


def has_mesh(name, entry):
    """True when the lane's CLI was given the fleet MCP (so it has peer_inbox). Evidence, in order: the roster's
    `mesh` bool; claude: the launch record ($FLEET_STATE/launch/<name>.sh passes --mcp-config with fleet_mesh.py),
    a user or per-project mcpServers.fleet in ~/.claude.json ($FLEET_CLAUDE_JSON), or <dir>/.mcp.json; codex,
    opencode, kiro, agy: a fleet server in the CLI's global MCP config."""
    m = entry.get("mesh")
    if isinstance(m, bool):
        return m
    cli, home = entry.get("cli"), os.path.expanduser("~")
    if cli == "claude":
        try:
            with open(os.path.join(state_dir(), "launch", "%s.sh" % name)) as f:
                if "fleet_mesh.py" in f.read():
                    return True
        except OSError:
            pass
        doc = _read_json(os.environ.get("FLEET_CLAUDE_JSON") or os.path.join(home, ".claude.json"))
        d = entry.get("dir") or ""
        proj = (doc.get("projects") or {}).get(d) if isinstance(doc.get("projects"), dict) else None
        return (_has_fleet(doc) or (isinstance(proj, dict) and _has_fleet(proj))
                or bool(d and _has_fleet(_read_json(os.path.join(d, ".mcp.json")))))
    if cli == "codex":
        try:
            with open(os.path.join(os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"), "config.toml")) as f:
                return "[mcp_servers.fleet]" in f.read()
        except OSError:
            return False
    if cli == "opencode":
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return _has_fleet(_read_json(os.path.join(xdg, "opencode", "opencode.json")), "mcp")
    if cli == "kiro":
        kh = os.environ.get("KIRO_HOME") or os.path.join(home, ".kiro")
        return _has_fleet(_read_json(os.path.join(kh, "settings", "mcp.json")))
    if cli == "agy":
        ah = os.environ.get("FLEET_AGY_HOME") or os.path.join(home, ".gemini")
        return _has_fleet(_read_json(os.path.join(ah, "config", "mcp_config.json")))
    return False


def wake_note(frm, to, entry):
    """The pointer line typed into a pane. A lane with no fleet MCP (not started by `fleet launch`) has no
    peer_inbox tool, so its line also names the inbox file to read by hand."""
    note = "[fleet] message from %s: run peer_inbox to read it" % frm
    if has_mesh(to, entry):
        return note
    return note + " (no peer_inbox tool? read %s)" % _inbox_path(to)


def _cursor_path(name):
    return os.path.join(state_dir(), "inbox", "%s.cursor" % name)


def _check_name(name):
    if not isinstance(name, str) or not name or not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError("invalid lane name: %r" % (name,))
    return name


def read_cursor(name):
    try:
        with open(_cursor_path(name)) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def unread_count(name):
    return max(0, len(_read_lines(_inbox_path(name))) - read_cursor(name))


def read_inbox(name, all_msgs=False):
    _check_name(name)
    path = _inbox_path(name)
    lines = _read_lines(path)
    if all_msgs:
        start = 0
    else:
        start = read_cursor(name)
    msgs = []
    for ln in lines[start:]:
        try:
            msgs.append(json.loads(ln))
        except ValueError:
            msgs.append({"text": ln})
    if not all_msgs:
        _atomic_write(_cursor_path(name), "%d\n" % len(lines))
    return msgs


# ---------------------------------------------------------------- delivery

def _run(argv):
    """Run argv (no shell) with a 15 s timeout. Returns (ok, detail)."""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                           universal_newlines=True)
    except subprocess.TimeoutExpired:
        return False, "timeout after 15s"
    except OSError as e:
        return False, str(e)
    if p.returncode != 0:
        return False, "exit %d: %s" % (p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    return True, "ok"


# agy screens a typed wake must never land on: its Enter would answer the screen for the human (agy's Terms of Service
# screen has a data-sharing opt-in pre-checked, so Enter would accept both). Same strings as bin/fleet agy_screen.
AGY_BLOCKING_SCREENS = ("Do you trust the contents", "Choose your color scheme", "Terms of Service",
                        "Waiting for authentication")


def _pane_text(pane):
    """capture-pane -p for pane; returns the text, or None when tmux fails."""
    try:
        p = subprocess.run(_tmux_argv("capture-pane", "-p", "-t", pane), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15, universal_newlines=True)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout if p.returncode == 0 else None


def _tmux_argv(*args):
    argv = ["tmux"]
    sock = os.environ.get("FLEET_TMUX_SOCKET")
    if sock:
        argv += ["-L", sock]
    return argv + list(args)


def wake(frm, to, text, entry):
    cli = entry.get("cli")
    if cli == "codex":
        binary = os.environ.get("FLEET_CODEX_BIN") or "codex"
        thread = entry.get("session_id") or to  # thread uuid when known, else the /rename name (both PROVEN)
        argv = [binary, "queue", "--thread", thread, "--message", "[fleet message from %s] %s" % (frm, text)]
        ok, detail = _run(argv)
        return "codex queue", ok, detail
    if cli == "opencode":
        port, sid = entry.get("port"), entry.get("session_id")
        if not port or not sid:
            return "opencode prompt_async", False, "roster entry lacks port or session_id"
        url = "http://127.0.0.1:%s/session/%s/prompt_async" % (port, sid)
        body = json.dumps({"parts": [{"type": "text",
                                      "text": "[fleet message from %s] %s" % (frm, text)}]}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.getcode()
            return "opencode prompt_async", 200 <= code < 300, "http %s" % code
        except Exception as e:
            return "opencode prompt_async", False, str(e)
    if cli in ("claude", "kiro", "agy"):  # kiro, agy: typed text + a separate Enter starts a turn on an idle lane (PROVEN)
        pane = entry.get("pane")
        if not pane:
            return "tmux send-keys", False, "roster entry lacks pane"
        if cli == "agy":  # look before typing; fail closed when the pane can't be read
            screen = _pane_text(pane)
            if screen is None:
                return "tmux send-keys", False, "could not read agy pane %s; not typing into it" % pane
            hit = next((m for m in AGY_BLOCKING_SCREENS if m in screen), None)
            if hit:
                return "tmux send-keys", False, "agy pane %s shows a %r screen; not typing into it" % (pane, hit)
        note = wake_note(frm, to, entry)
        ok, detail = _run(_tmux_argv("send-keys", "-t", pane, "-l", note))
        if ok:
            ok, detail = _run(_tmux_argv("send-keys", "-t", pane, "Enter"))
        return "tmux send-keys", ok, detail
    return "none", False, "unknown cli %r" % (cli,)


def fleet_bin():
    return os.environ.get("FLEET_BIN") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "fleet")


def reopen(to):
    """Run `fleet reopen <to>` (timeout 180 s). Returns (ok, detail)."""
    try:
        p = subprocess.run([fleet_bin(), "reopen", to], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=180, universal_newlines=True)
    except subprocess.TimeoutExpired:
        return False, "timeout after 180s"
    except OSError as e:
        return False, str(e)
    if p.returncode != 0:
        return False, "exit %d: %s" % (p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    return True, "ok"


def deliver(frm, to, text):
    """Append to inbox/<to>.jsonl, then wake by roster cli. Returns a result string.

    A parked or done target is reopened first (`fleet reopen <to>`), then woken as usual."""
    _check_name(to)
    ts = now()
    msg_id = "%s-%s-%d" % (ts, to, len(_read_lines(_inbox_path(to))) + 1)
    rec = {"id": msg_id, "at": ts, "from": frm, "to": to, "text": text}
    _append_line(_inbox_path(to), json.dumps(rec))
    entry = load_roster().get(to)
    if not entry:
        log("deliver %s -> %s queued (no live lane)" % (frm, to))
        return "queued (no live lane): %s -> %s" % (frm, to)
    prefix = ""
    if entry.get("status") in ("parked", "done"):
        r_ok, r_detail = reopen(to)
        log("deliver %s -> %s reopen ok=%s %s" % (frm, to, r_ok, r_detail))
        if not r_ok:
            return "delivered %s -> %s (inbox written; reopen FAILED: %s)" % (frm, to, r_detail)
        prefix = "reopened %s; " % to
        entry = load_roster().get(to) or entry
    how, ok, detail = wake(frm, to, text, entry)
    log("deliver %s -> %s wake=%s ok=%s %s" % (frm, to, how, ok, detail))
    if ok:
        return "%sdelivered %s -> %s (inbox + wake via %s ok)" % (prefix, frm, to, how)
    return "%sdelivered %s -> %s (inbox written; wake via %s FAILED: %s)" % (prefix, frm, to, how, detail)


# ---------------------------------------------------------------- mirror (native SendMessage -> mesh)

def claude_sessions_dir():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"),
                        "sessions")


def _lane_by_socket(path, roster):
    """Roster lane whose Claude session advertises messaging socket `path` (matched by session id), or None."""
    want = os.path.realpath(path)
    for name, e in roster.items():
        if e.get("cli") != "claude" or not e.get("pid"):
            continue
        try:
            with open(os.path.join(claude_sessions_dir(), "%s.json" % int(e["pid"]))) as f:
                sess = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        if e.get("session_id") and sess.get("sessionId") != e.get("session_id"):
            continue
        sock = sess.get("messagingSocketPath")
        if sock and os.path.realpath(sock) == want:
            return name
    return None


def mirror_target(to, roster):
    """Map a SendMessage `to` (a lane name, `name [ref]`, or `uds:<socket>`) to a roster lane name, else None."""
    if not isinstance(to, str) or not to.strip():
        return None
    to = to.strip()
    if to.startswith("uds:"):
        return _lane_by_socket(to[4:], roster)
    name = to.split(" [", 1)[0].strip()
    return name if name in roster else None


def mirror(payload):
    """PostToolUse hook body for native SendMessage: append a copy to the mesh inbox of the fleet lane it went to.

    No wake and no reopen (the native message already reached the lane). Returns a result string; never raises."""
    try:
        if payload.get("tool_name") != "SendMessage":
            return "skip: tool %r" % (payload.get("tool_name"),)
        ti = payload.get("tool_input") or {}
        text = ti.get("message")
        frm = os.environ.get("FLEET_NAME")
        if not frm:
            return "skip: FLEET_NAME unset (not a fleet lane)"
        if not isinstance(text, str) or not text:
            return "skip: no message body"
        to = mirror_target(ti.get("to"), load_roster())
        if not to or to == frm:
            return "skip: %r is not another fleet lane" % (ti.get("to"),)
        _check_name(to)
        ts = now()
        rec = {"id": "%s-%s-%d" % (ts, to, len(_read_lines(_inbox_path(to))) + 1), "at": ts, "from": frm,
               "to": to, "text": text, "via": "native"}
        _append_line(_inbox_path(to), json.dumps(rec))
        log("mirror %s -> %s (native SendMessage)" % (frm, to))
        return "mirrored %s -> %s" % (frm, to)
    except Exception as e:
        log("mirror error: %s" % e)
        return "mirror error: %s" % e


# ---------------------------------------------------------------- decisions

def _decisions_path():
    return os.path.join(state_dir(), "decisions.jsonl")


def decisions():
    return _parse_lines(_decisions_path())


def _sender(args):
    s = args.get("from") or os.environ.get("FLEET_NAME")
    if not s:
        raise ValueError("no sender: pass `from` or set FLEET_NAME")
    return s


# ---------------------------------------------------------------- tools

def t_peer_list(args):
    roster = load_roster()
    if not roster:
        return "roster empty"
    rows = []
    for name in sorted(roster):
        e = roster[name]
        rows.append("%s cli=%s mode=%s pane=%s session=%s port=%s unread=%d" % (
            name, e.get("cli"), e.get("mode"), e.get("pane"), e.get("session_id"),
            e.get("port"), unread_count(name)))
    return "\n".join(rows)


def t_peer_send(args):
    to, text = args.get("to"), args.get("text")
    if not to or not isinstance(text, str) or not text:
        raise ValueError("`to` and `text` are required")
    return deliver(_sender(args), to, text)


def t_peer_inbox(args):
    name = args.get("name") or os.environ.get("FLEET_NAME")
    if not name:
        raise ValueError("no inbox name: pass `name` or set FLEET_NAME")
    msgs = read_inbox(name, bool(args.get("all")))
    if not msgs:
        return "no unread messages"
    return "\n".join("[%s] from %s%s: %s" % (m.get("at", ""), m.get("from", "?"),
                                          " (native copy)" if m.get("via") == "native" else "", m.get("text", ""))
                     for m in msgs)


ALLOW_TYPES = ("pick", "edit", "own", "snooze", "refuse")
WATCH_KEYS = ("sourceKind", "source", "ref", "url", "eid")  # the WATCH object a call may carry (four-desks 2.4)
_WATCH_URL = re.compile(r"^https?://[^\s\"'<>\\]+$", re.I)


def _clean_watch(w):
    """Keep WATCH_KEYS only, as one-line strings; drop a url that is not http(s). None when nothing is left."""
    if w is None:
        return None
    if not isinstance(w, dict):
        raise ValueError("`watch` must be an object {sourceKind, source, ref, url, eid}")
    out = {}
    for k in WATCH_KEYS:
        v = w.get(k)
        if isinstance(v, str) and v.strip() and len(v) <= 500 and "\n" not in v:
            out[k] = v.strip()
    if "url" in out and not _WATCH_URL.match(out["url"]):
        del out["url"]
    return out or None


def t_decision_open(args):
    q = args.get("question")
    if not q:
        raise ValueError("`question` is required")
    asker = _sender(args)
    opts = args.get("options") or []
    if not isinstance(opts, list):
        raise ValueError("`options` must be a list of strings")
    allow = args.get("allow")
    if allow is not None:
        if not isinstance(allow, list) or not set(allow) <= set(ALLOW_TYPES):
            raise ValueError("`allow` must be a list drawn from %s" % ", ".join(ALLOW_TYPES))
        allow = [str(a) for a in allow]
    watch = _clean_watch(args.get("watch"))
    path = _decisions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            n = len([ln for ln in f.read().split("\n") if ln.strip()]) + 1
            rec = {"id": "Q%d" % n, "at": now(), "from": asker, "question": q,
                   "options": [str(o) for o in opts], "status": "open",
                   "answer": None, "answeredAt": None}
            if allow is not None:
                rec["allow"] = allow
            if watch:
                rec["watch"] = watch
            f.seek(0, 2)
            f.write(json.dumps(rec) + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    text = "[decision Q%d from %s] %s" % (n, asker, q)
    if opts:
        text += " options: " + " | ".join(str(o) for o in opts)
    res = deliver(asker, desk_name(), text)
    return "opened Q%d; %s" % (n, res)


def t_decision_list(args):
    status = args.get("status")
    rows = [d for d in decisions() if not status or d.get("status") == status]
    if not rows:
        return "no decisions"
    out = []
    for d in rows:
        line = "%s [%s] from %s: %s" % (d.get("id"), d.get("status"), d.get("from"), d.get("question"))
        if d.get("options"):
            line += " options: " + " | ".join(d["options"])
        if d.get("answer"):
            line += " -> " + d["answer"]
        out.append(line)
    return "\n".join(out)


def mark_decision(did, answer, extra=None, only_open=False):
    """Mark decision `did` answered in decisions.jsonl under its file lock. Returns the updated record.

    only_open=True refuses an id that is no longer open (raises AlreadyAnswered), so two front ends racing on the
    same call relay once. `extra` fields (answerKind, via, ...) are merged into the record."""
    path = _decisions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            rows = []
            for ln in f.read().split("\n"):
                if ln.strip():
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        pass
            target = None
            for r in rows:
                if r.get("id") == did:
                    target = r
            if target is None:
                raise ValueError("no such decision: %s" % did)
            if only_open and target.get("status") != "open":
                raise AlreadyAnswered("%s is already %s" % (did, target.get("status")))
            target.update({"status": "answered", "answer": answer, "answeredAt": now()})
            if extra:
                target.update(extra)
            f.seek(0)
            f.truncate()
            f.write("".join(json.dumps(r) + "\n" for r in rows))
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return target


class AlreadyAnswered(ValueError):
    pass


def decisions_log_path():
    return os.path.join(state_dir(), "decisions-log.jsonl")


def reports_path():
    return os.path.join(state_dir(), "decisions-reports.jsonl")


def t_decision_answer(args):
    did, answer = args.get("id"), args.get("answer")
    if not did or not answer:
        raise ValueError("`id` and `answer` are required")
    target = mark_decision(did, answer)
    frm = os.environ.get("FLEET_NAME") or desk_name()
    asker = target.get("from")
    _append_line(decisions_log_path(), json.dumps(
        {"id": did, "at": now(), "asker": asker, "verbatim": answer, "relayedTo": [asker] if asker else [], "by": frm}))
    if not asker:
        return "answered %s; no asker to deliver to" % did
    return "answered %s; %s" % (did, deliver(frm, asker, "[answer %s] %s" % (did, answer)))


def t_decision_report(args):
    """The asker reports whether it acted on an answer: appends decisions-reports.jsonl."""
    did = args.get("id")
    ok = args.get("ok")
    if not did or not isinstance(ok, bool):
        raise ValueError("`id` and boolean `ok` are required")
    rec = {"id": did, "at": now(), "from": _sender(args), "ok": ok, "note": str(args.get("note") or "")}
    _append_line(reports_path(), json.dumps(rec))
    return "reported %s %s" % (did, "acted on" if ok else "act failed")


def _norm(text):
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in (text or "")).split())


def find_decisions(text):
    """Answered decisions whose question matches `text`: exact (normalized) repeats first, then substring hits.

    Returns [{id, question, verbatim, at, asker, exact}] from decisions.jsonl joined with decisions-log.jsonl."""
    want = _norm(text)
    if not want:
        return []
    latest = {}
    for rec in _parse_lines(decisions_log_path()):
        if rec.get("id"):
            latest[rec["id"]] = rec
    hits = []
    for d in decisions():
        if d.get("status") != "answered":
            continue
        q = _norm(d.get("question"))
        exact = q == want
        if not exact and want not in q and q not in want:
            continue
        lg = latest.get(d.get("id")) or {}
        hits.append({"id": d.get("id"), "question": d.get("question"), "verbatim": lg.get("verbatim") or d.get("answer"),
                     "at": lg.get("at") or d.get("answeredAt"), "asker": d.get("from"), "exact": exact})
    hits.sort(key=lambda h: (not h["exact"], h.get("at") or ""), reverse=False)
    return hits


def t_decision_find(args):
    text = args.get("text")
    if not text:
        raise ValueError("`text` is required")
    hits = find_decisions(text)
    if not hits:
        return "no past decision matches"
    return "\n".join('%s%s [%s] %s -> "%s"' % (h["id"], " (exact repeat)" if h["exact"] else "", h.get("at"),
                                              h.get("question"), h.get("verbatim")) for h in hits)


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
TOOLS = [
    ("peer_list", "List roster lanes with unread inbox counts.", _schema({}), t_peer_list),
    ("peer_send", "Send a message to another lane (inbox plus wake).",
     _schema({"to": _S, "text": _S, "from": _S}, ["to", "text"]), t_peer_send),
    ("peer_inbox", "Read unread messages (advances cursor); all=true returns everything.",
     _schema({"name": _S, "all": {"type": "boolean"}}), t_peer_inbox),
    ("decision_open", "Open a decision and deliver it to the decisions desk.",
     _schema({"question": _S, "options": {"type": "array", "items": _S}, "from": _S,
              "allow": {"type": "array", "items": {"type": "string", "enum": list(ALLOW_TYPES)}},
              "watch": {"type": "object", "description": "WATCH source of the call: sourceKind, source, ref, url "
                        "(http/https only), eid. Other keys are dropped.",
                        "properties": {k: _S for k in WATCH_KEYS}}}, ["question"]),
     t_decision_open),
    ("decision_list", "List decisions, optionally filtered by status (open|answered).",
     _schema({"status": {"type": "string", "enum": ["open", "answered"]}}), t_decision_list),
    ("decision_answer", "Answer a decision; delivers to the asker only and appends decisions-log.jsonl.",
     _schema({"id": _S, "answer": _S}, ["id", "answer"]), t_decision_answer),
    ("decision_report", "Report whether you acted on an answer (ok=true acted on, false act failed).",
     _schema({"id": _S, "ok": {"type": "boolean"}, "note": _S, "from": _S}, ["id", "ok"]), t_decision_report),
    ("decision_find", "Search past answered decisions before opening a call; an exact repeat returns its quote.",
     _schema({"text": _S}, ["text"]), t_decision_find),
]
TOOL_MAP = {t[0]: t for t in TOOLS}


def call_tool(name, args):
    if name not in TOOL_MAP:
        return {"content": [{"type": "text", "text": "unknown tool: %s" % name}], "isError": True}
    try:
        text = TOOL_MAP[name][3](args or {})
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        log("tool %s error: %s" % (name, e))
        return {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True}


# ---------------------------------------------------------------- JSON-RPC

def handle(msg):
    """Return a response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, mid = msg.get("method"), msg.get("id")
    is_note = "id" not in msg
    params = msg.get("params") or {}
    if method == "initialize":
        req = params.get("protocolVersion")
        ver = req if req in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fleet-mesh", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}
    elif method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments"))
    elif is_note:
        return None
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    if is_note:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                resp = handle(msg)
            except Exception as e:
                log("internal error: %s" % e)
                resp = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": "Internal error: %s" % e}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- CLI

def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "serve":
        serve()
        return 0
    if cmd == "send" and len(argv) >= 5:
        print(deliver(argv[2], argv[3], " ".join(argv[4:])))
        return 0
    if cmd == "inbox" and len(argv) == 3:
        for m in read_inbox(argv[2]):
            print("[%s] from %s: %s" % (m.get("at", ""), m.get("from", "?"), m.get("text", "")))
        return 0
    if cmd == "mirror":  # PostToolUse hook: stdin is the hook JSON; always exit 0 so a hook never blocks a send
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        print(mirror(payload if isinstance(payload, dict) else {}))
        return 0
    if cmd == "mesh":  # one line per roster lane: "<name> yes|no" (fleet ls)
        for n, e in sorted(load_roster().items()):
            print("%s %s" % (n, "yes" if has_mesh(n, e if isinstance(e, dict) else {}) else "no"))
        return 0
    if cmd == "roster":
        print(json.dumps(load_roster(), indent=2, sort_keys=True))
        return 0
    sys.stderr.write("usage: fleet_mesh.py serve | send <from> <to> <text> | inbox <name> | roster | mesh | mirror\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
