#!/usr/bin/env python3
"""Side-run usage accounting (issue #13): pure functions over files; never reads or writes the side store.

usage(row)           -> {"tokensIn", "tokensOut", "children", "source"}; each value an int or None. Never a guess.
sample_children(row) -> live processes in the tree rooted at row["pid"] (the root counts), 0 when it is gone.
fmt(row, now=None)   -> one roster line, e.g. "codex gpt-x 7m/30m 2.1M tok"; unknown tokens print "? tok".

Token sources, first hit wins:
  1. row["result"] parses as an ach RunResult (tokens[] of inputTokens/outputTokens/cacheReadTokens/
     cacheWriteTokens): in = input + cacheRead + cacheWrite, out = output. source "ach".
  2. row["result"] is {"tokens": {"in": N, "out": N}} (the generic host-wrapper contract). source "result".
  3. host "codex" with row["sessionId"]: fleet_probe.tokens_for("codex", sid), plus the same for every helper
     session whose rollout session_meta names it (or one of its helpers) as parent. source "codex".
  4. Otherwise None.
"""
import glob
import json
import os
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_probe as probe  # noqa: E402


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _load(path):
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def result_tokens(path):
    """(in, out, source) from a result file, or (None, None, None) when it is missing or not a known shape."""
    d = _load(path)
    if not isinstance(d, dict):
        return None, None, None
    t = d.get("tokens")
    if isinstance(t, list) and "runId" in d:
        tin = tout = 0
        for rec in t:
            if not isinstance(rec, dict):
                return None, None, None
            parts = [_int(rec.get(k, 0)) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")]
            o = _int(rec.get("outputTokens", 0))
            if None in parts or o is None:
                return None, None, None
            tin += sum(parts)
            tout += o
        return tin, tout, "ach"
    if isinstance(t, dict):
        tin, tout = _int(t.get("in")), _int(t.get("out"))
        if tin is not None and tout is not None:
            return tin, tout, "result"
    return None, None, None


# ---------------------------------------------------------------- codex helper sessions

def _codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def _meta(path):
    """The session_meta payload on a rollout's first line, or None."""
    try:
        with open(path) as f:
            d = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if isinstance(d, dict) and d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
        return d["payload"]
    return None


def _parent_of(meta):
    """Checked on codex 0.153.4 through 0.166.0: a helper names its parent in payload.parent_thread_id and in
    payload.source.subagent.thread_spawn.parent_thread_id (older rollouts carry only the nested one)."""
    p = meta.get("parent_thread_id")
    if isinstance(p, str) and p:
        return p
    src = meta.get("source")
    if isinstance(src, dict):
        spawn = (src.get("subagent") or {}).get("thread_spawn") if isinstance(src.get("subagent"), dict) else None
        if isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str):
            return spawn["parent_thread_id"]
    return None


def codex_helpers(sid):
    """Session ids of every helper below `sid` (helpers of helpers too), found by rollout session_meta."""
    kids = {}
    for fp in glob.glob(os.path.join(_codex_home(), "sessions", "**", "*.jsonl"), recursive=True):
        m = _meta(fp)
        if not m:
            continue
        me, par = m.get("id") or m.get("session_id"), _parent_of(m)
        if me and par:
            kids.setdefault(par, set()).add(me)
    out, todo = [], [sid]
    while todo:
        for k in sorted(kids.get(todo.pop(), ())):
            if k not in out and k != sid:
                out.append(k)
                todo.append(k)
    return out


def codex_tokens(sid):
    tin, tout = probe.tokens_for("codex", sid)
    if tin is None:
        return None, None
    for h in codex_helpers(sid):
        hi, ho = probe.tokens_for("codex", h)
        if hi is not None:
            tin, tout = tin + hi, tout + ho
    return tin, tout


# ---------------------------------------------------------------- public API

def usage(row):
    tin, tout, src = result_tokens(row.get("result"))
    if src is None and row.get("host") == "codex" and row.get("sessionId"):
        tin, tout = codex_tokens(row["sessionId"])
        src = "codex" if tin is not None else None
    return {"tokensIn": tin, "tokensOut": tout, "children": _int(row.get("children")), "source": src}


def _ps():
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,stat="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    kids, alive = {}, set()
    for ln in out.splitlines():
        f = ln.split()
        if len(f) < 2 or not f[0].isdigit() or not f[1].isdigit():
            continue
        if len(f) > 2 and f[2].startswith("Z"):
            continue  # a zombie has exited; it is no longer running work
        pid, ppid = int(f[0]), int(f[1])
        alive.add(pid)
        kids.setdefault(ppid, []).append(pid)
    return {"kids": kids, "alive": alive}


def sample_children(row):
    """Live processes in the tree rooted at row["pid"], the root included: `sh -c 'sleep & sleep & wait'` is 3.
    0 when the root is gone. The watcher calls this every poll and stores the max in row["children"]."""
    pid = _int(row.get("pid"))
    if not pid:
        return 0
    t = _ps()
    if not t or pid not in t["alive"]:
        return 0
    seen, todo = {pid}, [pid]
    while todo:
        for k in t["kids"].get(todo.pop(), ()):
            if k not in seen:
                seen.add(k)
                todo.append(k)
    return len(seen)


def human_tokens(n):
    if n is None:
        return "?"
    if n >= 1000000:
        return "%.1fM" % (n / 1e6)
    if n >= 1000:
        return "%.1fk" % (n / 1e3)
    return str(n)


def fmt(row, now=None):
    """'<host> [<model>] <elapsed>m[/<bound>m] <tokens> tok'; the clock stops at endedAt."""
    now = time.time() if now is None else now
    start = probe.to_epoch(row.get("startedAt"))
    end = probe.to_epoch(row.get("endedAt")) or now
    parts = [str(row.get("host") or "?")]
    if row.get("model"):
        parts.append(str(row["model"]))
    clock = "%dm" % (max(0, int(end - start)) // 60) if start else "?m"
    bound = _int(row.get("boundSec"))
    if bound:
        clock += "/%dm" % (bound // 60)
    parts.append(clock)
    tin, tout = row.get("tokensIn"), row.get("tokensOut")
    total = tin + tout if _int(tin) is not None and _int(tout) is not None else None
    parts.append("%s tok" % human_tokens(total))
    return " ".join(parts)
