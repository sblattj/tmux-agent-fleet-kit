#!/usr/bin/env python3
"""Side runs follow their parent's lifecycle (issue #18). Python 3.8+ stdlib only.

stop_for_parent(name, keep=False) -> [rows]   park/stop: kill every running side run of NAME (status=killed), or with
                                              keep=True leave them alive and mark them status=orphan.
orphans(dead=(), now=None)        -> [rows]   live side runs whose parent is parked, done or gone, or past their bound;
                                              each row gains "why" (parked|done|archived|gone|over-bound) and "ageSec".
kill_orphans(dead=())             -> [rows]   kill what orphans() lists and mark it status=killed.
kill_run(row, grace=None)         -> bool     SIGTERM the run's process group (the pid alone when it does not lead its
                                              own group, or leads ours), SIGKILL after the grace period.

CLI (through `fleet side`, see COMMANDS in lib/fleet_side.py):
  fleet side stop-parent NAME [--keep-sides]   prints "OK stopped N side run(s) of NAME" or "OK kept N ..."
  fleet side orphans [--dead A,B] [--json] [--kill]   one ORPHAN-SIDE line per run
A killed run gets endedAt and notifiedAt, so the watcher never sends the parent (going away) a completion message.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_mesh as mesh  # noqa: E402
import fleet_side as side  # noqa: E402

LIVE = ("running", "orphan")
GONE_STATUSES = ("parked", "done", "archived")


def grace_sec():
    try:
        return max(0.0, float(os.environ.get("FLEET_SIDE_KILL_GRACE", "5")))
    except ValueError:
        return 5.0


def alive(pid):
    """pid_alive, but a zombie (exited, not yet reaped) counts as dead."""
    if not side.pid_alive(pid):
        return False
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
    except (OSError, ValueError):
        return True
    return not st.startswith("Z") if st else False


def _signal(pid, sig):
    """Signal the run's group when it leads one that is not ours, else the pid alone."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    try:
        if pgid == pid and pgid != os.getpgrp():
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_run(row, grace=None):
    """True when the pid is gone afterwards."""
    try:
        pid = int(row.get("pid"))
    except (TypeError, ValueError):
        return True
    if pid <= 1 or not alive(pid):
        return True
    _signal(pid, signal.SIGTERM)
    deadline = time.time() + (grace_sec() if grace is None else grace)
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    _signal(pid, signal.SIGKILL)
    for _ in range(20):
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


def _mark(id, status):
    now = side._now()

    def fn(r):
        if r.get("status") not in LIVE:
            return None
        r["status"] = status
        if status == "killed":
            r["endedAt"] = r.get("endedAt") or now
            r["notifiedAt"] = r.get("notifiedAt") or now
        return r
    return side.update(id, fn)


def stop_for_parent(name, keep=False):
    out = []
    for r in side.rows(parent=name):
        if r.get("status") != "running":
            continue
        if keep:
            out.append(_mark(r["id"], "orphan"))
        else:
            kill_run(r)
            out.append(_mark(r["id"], "killed"))
    return out


def _age(r, now):
    try:
        import calendar
        t = calendar.timegm(time.strptime(r.get("startedAt") or "", "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0
    return max(0, int(now - t))


def orphans(dead=(), now=None):
    now = time.time() if now is None else now
    roster = mesh.load_roster()
    dead = set(dead or ())
    out = []
    for r in side.rows():
        if r.get("status") not in LIVE or not alive(r.get("pid")):
            continue
        p = r.get("parent")
        e = roster.get(p)
        st = (e.get("status") if isinstance(e, dict) else None) or "live"
        age = _age(r, now)
        if not isinstance(e, dict) or p in dead:
            why = "gone"
        elif st in GONE_STATUSES:
            why = st
        elif age > int(r.get("boundSec") or side.DEFAULT_BOUND_SEC):
            why = "over-bound"
        else:
            continue
        out.append(dict(r, why=why, ageSec=age))
    return out


def kill_orphans(dead=()):
    out = []
    for r in orphans(dead):
        kill_run(r)
        out.append(dict(_mark(r["id"], "killed"), why=r["why"]))
    return out


def line(r):
    return "ORPHAN-SIDE %s parent=%s pid=%s age=%dm bound=%dm why=%s" % (
        r.get("id"), r.get("parent"), r.get("pid"), int(r.get("ageSec", 0)) // 60,
        int(r.get("boundSec") or side.DEFAULT_BOUND_SEC) // 60, r.get("why"))


# ---------------------------------------------------------------- CLI

def cmd_stop_parent(argv):
    ap = argparse.ArgumentParser(prog="fleet side stop-parent")
    ap.add_argument("name")
    ap.add_argument("--keep-sides", action="store_true")
    a = ap.parse_args(argv)
    rs = stop_for_parent(a.name, keep=a.keep_sides)
    verb = "kept" if a.keep_sides else "stopped"
    print("OK %s %d side run%s of %s%s" % (verb, len(rs), "" if len(rs) == 1 else "s", a.name,
                                           (" (" + " ".join(r["id"] for r in rs) + ")") if rs else ""))


def cmd_orphans(argv):
    ap = argparse.ArgumentParser(prog="fleet side orphans")
    ap.add_argument("--dead", default="", help="comma-separated lanes whose pane is gone")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kill", action="store_true")
    a = ap.parse_args(argv)
    dead = [n for n in a.dead.split(",") if n]
    rs = kill_orphans(dead) if a.kill else orphans(dead)
    if a.json:
        print(json.dumps(rs))
        return
    for r in rs:
        print(("OK killed side %s pid=%s" % (r["id"], r["pid"])) if a.kill else "  " + line(r))


if __name__ == "__main__":
    sys.exit(side.main(sys.argv[1:]))
