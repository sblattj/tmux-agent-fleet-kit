#!/usr/bin/env python3
"""fleet side: track runs a lane starts outside the fleet's tmux panes (codex, agy, gemini, ach run, ...).

Python 3.8+ stdlib only. See INTERFACES.md ("Side runs") for the contract.
  fleet_side.py start --parent NAME --host H [--model M] --pid PID [--log P] [--result P] [--bound 30m]
                [--session-id ID]
              | end ID [--status done|failed] [--exit N]
              | ls [--parent NAME] [--json]
              | show ID

Store: $FLEET_STATE/sides.jsonl, one row per run, rewritten under an exclusive flock (fleet_probe._flock_rw
pattern). Not roster.json: bin/fleet rewrites the roster with jq and no lock.
Later side-run modules (fleet_side_watch.py, fleet_side_run.py, fleet_side_ach.py) import the API below and add a
subcommand with one COMMANDS line.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_mesh as mesh  # noqa: E402

STATUSES = ("running", "done", "failed", "timeout", "killed", "orphan")
END_STATUSES = ("done", "failed")  # what `fleet side end` accepts; the watcher sets timeout|killed|orphan
MAX_BOUND_SEC = 12 * 3600
DEFAULT_BOUND_SEC = 1800


class SideError(Exception):
    """A refused request; the CLI prints FAIL <message> and exits 1."""


# ---------------------------------------------------------------- store

def path():
    return os.path.join(mesh.state_dir(), "sides.jsonl")


def _flock_rw(fn):
    """Run fn(rows) -> (rows|None, result) under an exclusive lock on sides.jsonl; rewrite it when rows is not None.
    Same pattern as lib/fleet_probe.py _flock_rw."""
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a+") as f:
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
            out, res = fn(rows)
            if out is not None:
                f.seek(0)
                f.truncate()
                f.write("".join(json.dumps(r) + "\n" for r in out))
                f.flush()
            return res
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_bound(v):
    """'30m' | '2h' | '90s' | '1800' -> seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        sec = int(v)
    else:
        m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(v or ""))
        if not m:
            raise SideError("bad bound %r (use 90s, 30m, 2h or seconds)" % (v,))
        sec = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if sec <= 0:
        raise SideError("bound must be positive")
    if sec > MAX_BOUND_SEC:
        raise SideError("bound %ss is over 12h" % sec)
    return sec


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def _abs(p):
    return os.path.abspath(os.path.expanduser(p)) if p else None


def _next_id(rows):
    n = 0
    for r in rows:
        m = re.fullmatch(r"S(\d+)", str(r.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return "S%d" % (n + 1)


# ---------------------------------------------------------------- API

def start(parent=None, host=None, model=None, pid=None, log=None, result=None, bound=None, source="cli",
          sessionId=None, **extra):
    """Validate and append a running row; returns it. Raises SideError when refused."""
    parent = parent or os.environ.get("FLEET_NAME")
    if not parent:
        raise SideError("no parent (pass --parent or set FLEET_NAME)")
    if parent not in mesh.load_roster():
        raise SideError("no lane %s in the roster" % parent)
    if not host:
        raise SideError("no host (pass --host)")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise SideError("bad pid %r" % (pid,))
    if pid <= 0 or not pid_alive(pid):
        raise SideError("pid %s is not alive" % pid)
    bound_sec = parse_bound(DEFAULT_BOUND_SEC if bound is None else bound)
    base = {"id": None, "parent": parent, "host": host, "model": model, "pid": pid, "log": _abs(log),
            "result": _abs(result), "boundSec": bound_sec, "startedAt": _now(), "status": "running",
            "endedAt": None, "exit": None, "tokensIn": None, "tokensOut": None, "children": 0,
            "notifiedAt": None, "source": source, "sessionId": sessionId or None}
    for k, v in extra.items():
        base.setdefault(k, v)

    def fn(rows):
        row = dict(base, id=_next_id(rows))
        return rows + [row], row
    row = _flock_rw(fn)
    import fleet_side_watch  # noqa: E402  (issue #14: one detached watcher per run; FLEET_SIDE_WATCH_SPAWN=0 skips)
    fleet_side_watch.spawn(row["id"])
    return row


def update(id, fn):
    """Replace row `id` with fn(dict(row)) under the lock; returns the new row. Raises SideError for an unknown id.
    fn may return None to leave the row unchanged."""
    def apply(rows):
        for i, r in enumerate(rows):
            if r.get("id") == id:
                new = fn(dict(r))
                if new is None:
                    return None, r
                rows[i] = new
                return rows, new
        raise SideError("no side run %s" % id)
    return _flock_rw(apply)


def end(id, status="done", exit=None):
    """Mark a run ended. Idempotent: a row that already has endedAt is returned unchanged."""
    if status not in STATUSES or status == "running":
        raise SideError("bad end status %r" % (status,))

    def fn(r):
        if r.get("endedAt"):
            return None
        r.update(status=status, endedAt=_now())
        if exit is not None:
            r["exit"] = int(exit)
        return r
    return update(id, fn)


def rows(parent=None):
    out = mesh._parse_lines(path())
    return [r for r in out if parent is None or r.get("parent") == parent]


def get(id):
    for r in rows():
        if r.get("id") == id:
            return r
    return None


def stop_for_parent(name, keep=False):
    """Park/stop hook (issue #18): kill NAME's running side runs, or keep=True marks them orphan. See fleet_side_lifecycle."""
    import fleet_side_lifecycle
    return fleet_side_lifecycle.stop_for_parent(name, keep=keep)


def _lazy(mod, fn):
    """A COMMANDS entry from another module, imported only when the subcommand runs (no import cycle)."""
    return lambda argv: getattr(__import__(mod), fn)(argv)


# ---------------------------------------------------------------- CLI

def cmd_start(argv):
    ap = argparse.ArgumentParser(prog="fleet side start")
    ap.add_argument("--parent")
    ap.add_argument("--host", required=True)
    ap.add_argument("--model")
    ap.add_argument("--pid", required=True)
    ap.add_argument("--log")
    ap.add_argument("--result")
    ap.add_argument("--bound")
    ap.add_argument("--session-id", dest="session_id")
    a = ap.parse_args(argv)
    row = start(parent=a.parent, host=a.host, model=a.model, pid=a.pid, log=a.log, result=a.result, bound=a.bound,
                sessionId=a.session_id)
    print(row["id"])


def cmd_end(argv):
    ap = argparse.ArgumentParser(prog="fleet side end")
    ap.add_argument("id")
    ap.add_argument("--status", default="done", choices=END_STATUSES)
    ap.add_argument("--exit", type=int)
    a = ap.parse_args(argv)
    row = end(a.id, a.status, exit=a.exit)
    print("%s %s endedAt=%s" % (row["id"], row["status"], row["endedAt"]))


def cmd_ls(argv):
    ap = argparse.ArgumentParser(prog="fleet side ls")
    ap.add_argument("--parent")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rs = rows(parent=a.parent)
    if a.json:
        print(json.dumps(rs, indent=1))
        return
    for r in rs:
        print("%-5s %-8s %-20s %-10s pid=%-7s %s" % (r.get("id"), r.get("status"), r.get("parent"), r.get("host"),
                                                     r.get("pid"), r.get("startedAt")))


def cmd_show(argv):
    ap = argparse.ArgumentParser(prog="fleet side show")
    ap.add_argument("id")
    a = ap.parse_args(argv)
    r = get(a.id)
    if r is None:
        raise SideError("no side run %s" % a.id)
    print(json.dumps(r, indent=1))


COMMANDS = {
    "start": cmd_start,
    "end": cmd_end,
    "ls": cmd_ls,
    "show": cmd_show,
    "stop-parent": _lazy("fleet_side_lifecycle", "cmd_stop_parent"),  # issue #18
    "orphans": _lazy("fleet_side_lifecycle", "cmd_orphans"),  # issue #18
    "watch": lambda argv: __import__("fleet_side_watch").cmd_watch(argv),  # issue #14
    "run": lambda argv: __import__("fleet_side_run").cmd_run(argv),  # issue #15: lib/fleet_side_run.py
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write("usage: fleet side %s ...\n" % "|".join(COMMANDS))
        return 2
    try:
        COMMANDS[argv[0]](argv[1:])
    except SideError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
