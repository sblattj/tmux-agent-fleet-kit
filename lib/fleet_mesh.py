#!/usr/bin/env python3
"""fleet-mesh: stdio MCP server + CLI for peer messaging and decisions between fleet lanes.

Python 3.8+ stdlib only. See INTERFACES.md for the contract.
  fleet_mesh.py serve | send <from> <to> <text> | inbox <name> | roster
"""
import datetime
import fcntl
import json
import os
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
    return os.environ.get("FLEET_DESK") or "DECISIONS"


def orch_name():
    return os.environ.get("FLEET_ORCHESTRATOR") or "ORCHESTRATOR"


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
    if cli in ("claude", "kiro"):  # kiro: typed text + a separate Enter starts a turn on an idle lane (PROVEN)
        pane = entry.get("pane")
        if not pane:
            return "tmux send-keys", False, "roster entry lacks pane"
        note = "[fleet] message from %s: run peer_inbox to read it" % frm
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
    return "\n".join("[%s] from %s: %s" % (m.get("at", ""), m.get("from", "?"), m.get("text", ""))
                     for m in msgs)


def t_decision_open(args):
    q = args.get("question")
    if not q:
        raise ValueError("`question` is required")
    asker = _sender(args)
    opts = args.get("options") or []
    if not isinstance(opts, list):
        raise ValueError("`options` must be a list of strings")
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


def t_decision_answer(args):
    did, answer = args.get("id"), args.get("answer")
    if not did or not answer:
        raise ValueError("`id` and `answer` are required")
    path = _decisions_path()
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
            target.update({"status": "answered", "answer": answer, "answeredAt": now()})
            f.seek(0)
            f.truncate()
            f.write("".join(json.dumps(r) + "\n" for r in rows))
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    frm = os.environ.get("FLEET_NAME") or desk_name()
    asker = target.get("from")
    _append_line(os.path.join(state_dir(), "decisions-log.jsonl"), json.dumps(
        {"id": did, "at": now(), "asker": asker, "verbatim": answer, "relayedTo": [asker] if asker else []}))
    if not asker:
        return "answered %s; no asker to deliver to" % did
    return "answered %s; %s" % (did, deliver(frm, asker, "[answer %s] %s" % (did, answer)))


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
     _schema({"question": _S, "options": {"type": "array", "items": _S}, "from": _S}, ["question"]),
     t_decision_open),
    ("decision_list", "List decisions, optionally filtered by status (open|answered).",
     _schema({"status": {"type": "string", "enum": ["open", "answered"]}}), t_decision_list),
    ("decision_answer", "Answer a decision; delivers to the asker only and appends decisions-log.jsonl.",
     _schema({"id": _S, "answer": _S}, ["id", "answer"]), t_decision_answer),
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
    if cmd == "roster":
        print(json.dumps(load_roster(), indent=2, sort_keys=True))
        return 0
    sys.stderr.write("usage: fleet_mesh.py serve | send <from> <to> <text> | inbox <name> | roster\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
