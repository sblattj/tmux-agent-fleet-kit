#!/usr/bin/env python3
"""Side-run watcher (issue #14): one detached `fleet side watch ID` per run.

It detects completion, enforces the bound, and sends the parent exactly one inbox message. Stdlib only.
See INTERFACES.md ("Side-run watcher").

  spawn(id)   start a detached watcher (setsid, no tty, stdio to $FLEET_STATE/log/side-watch.log); `fleet side start`
              calls it. FLEET_SIDE_WATCH_SPAWN=0 turns spawning off.
  watch(id)   the loop. Exits at once (0) when another watcher holds $FLEET_STATE/side/<id>.watch.lock.
  notify(id)  claim notifiedAt under the store lock, then deliver one message; a second call sends nothing.
"""
import argparse
import fcntl
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
import fleet_side_usage as usage_mod  # noqa: E402
import fleet_probe as probe  # noqa: E402

DEFAULT_POLL = 5.0
DEFAULT_GRACE = 10.0
SELF_SLACK = 60


def _side():
    # bin/fleet runs fleet_side.py as __main__; reuse that module so SideError is the class main() catches.
    m = sys.modules.get("__main__")
    if m is not None and getattr(m, "__file__", "") and os.path.basename(m.__file__) == "fleet_side.py":
        return m
    import fleet_side
    return fleet_side


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def lock_path(id):
    return os.path.join(mesh.state_dir(), "side", "%s.watch.lock" % id)


def dur(sec):
    """432 -> '7m12s', 1800 -> '30m', 3720 -> '1h2m', 2 -> '2s'."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%dm" % (h, m) if m else "%dh" % h
    if m:
        return "%dm%ds" % (m, s) if s else "%dm" % m
    return "%ds" % s


def alive(pid):
    """Running, not a zombie. A zombie answers kill(0), so ask ps for its state; a ps failure trusts kill(0)."""
    if not _side().pid_alive(pid):
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return True
    if not out:
        return False
    return not out.startswith("Z")


def result_failed(path):
    """True when the result file is an ach RunResult (it has runId) whose exitStatus is not 'success'."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(d, dict) and "runId" in d and "exitStatus" in d and d.get("exitStatus") != "success"


def _sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except (OSError, TypeError):
        return None


def _tree(pid):
    """pid and its live descendants (from one ps snapshot)."""
    t = usage_mod._ps()
    out, todo = [pid], [pid]
    while todo and t:
        for k in t["kids"].get(todo.pop(), ()):
            if k not in out:
                out.append(k)
                todo.append(k)
    return out


def _kill(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def enforce_bound(pid, grace):
    """SIGTERM the run's tree, wait up to `grace`, then SIGKILL whatever is left. Returns True when the root died."""
    pids = _tree(pid)
    _kill(pids, signal.SIGTERM)
    end = time.time() + grace
    while time.time() < end:
        if not alive(pid):
            break
        time.sleep(0.1)
    left = [p for p in pids if alive(p)]
    if left:
        _kill(left, signal.SIGKILL)
        end = time.time() + 2
        while time.time() < end and alive(pid):
            time.sleep(0.05)
    return not alive(pid)


def message(r):
    """side S3 codex gpt-x done in 7m12s (bound 30m), 2.1M tok in / 40k out, 2 helper procs; result /abs; log /abs"""
    start = probe.to_epoch(r.get("startedAt"))
    end = probe.to_epoch(r.get("endedAt")) or time.time()
    head = " ".join(str(x) for x in ("side", r.get("id"), r.get("host") or "?", r.get("model")) if x)
    helpers = max(0, int(r.get("children") or 0) - 1)
    return "%s %s in %s (bound %s), %s tok in / %s out, %d helper proc%s; result %s; log %s" % (
        head, r.get("status"), dur(end - start) if start else "?", dur(r.get("boundSec") or 0),
        usage_mod.human_tokens(r.get("tokensIn")), usage_mod.human_tokens(r.get("tokensOut")),
        helpers, "" if helpers == 1 else "s", r.get("result") or "-", r.get("log") or "-")


def finish(id, status, max_children):
    """Close the row (unless `fleet side end` already did) and fill usage. Returns the row."""
    side = _side()

    def fn(r):
        if not r.get("endedAt"):
            st = status
            if st == "done" and ((r.get("exit") not in (None, 0)) or result_failed(r.get("result"))):
                st = "failed"
            r.update(status=st, endedAt=side._now())
        elif r.get("status") == "done" and result_failed(r.get("result")):
            r["status"] = "failed"
        r["children"] = max(int(r.get("children") or 0), int(max_children or 0))
        u = usage_mod.usage(r)
        for k in ("tokensIn", "tokensOut"):
            if u.get(k) is not None:
                r[k] = u[k]
        return r
    return side.update(id, fn)


def notify(id):
    """Send the parent one message. notifiedAt is claimed under the store lock first, then the message is delivered,
    so a second watcher, a restart or `fleet side end` can never send a second one. Returns the deliver result or
    None when already notified."""
    side = _side()
    claimed = []

    def fn(r):
        if r.get("notifiedAt") or not r.get("endedAt"):
            return None
        r["notifiedAt"] = side._now()
        claimed.append(dict(r))
        return r
    side.update(id, fn)
    if not claimed:
        return None
    r = claimed[0]
    res = mesh.deliver("side:%s" % id, r["parent"], message(r))
    mesh.log("side watch %s: notified %s: %s" % (id, r["parent"], res))
    return res


def watch(id):
    """The watcher loop. Returns 0; raises SideError for an unknown id."""
    side = _side()
    row = side.get(id)
    if row is None:
        raise side.SideError("no side run %s" % id)
    lp = lock_path(id)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    lf = open(lp, "a")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lf.close()
        return 0  # another watcher owns this id
    try:
        return _loop(id, row)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


def _loop(id, row):
    side = _side()
    poll = max(0.05, _env_float("FLEET_SIDE_POLL", DEFAULT_POLL))
    grace = max(0.0, _env_float("FLEET_SIDE_GRACE", DEFAULT_GRACE))
    start = probe.to_epoch(row.get("startedAt")) or time.time()
    bound = int(row.get("boundSec") or 0)
    self_deadline = start + bound + grace + SELF_SLACK
    pid = int(row.get("pid") or 0)
    max_kids = int(row.get("children") or 0)
    last, stable = None, 0
    status = None
    mesh.log("side watch %s: pid %s bound %ss" % (id, pid, bound))
    while True:
        if not os.path.isdir(mesh.state_dir()):
            return 0  # the state went away (a throwaway state removed under us); never recreate it
        row = side.get(id)
        if row is None:
            return 0
        if row.get("endedAt"):
            status = row.get("status")
            break
        max_kids = max(max_kids, usage_mod.sample_children(row))
        sig = _sig(row.get("result"))
        if sig is not None and sig == last:
            stable += 1
        else:
            stable = 0
        last = sig
        if not alive(pid):
            status = "done"
            break
        if stable >= 2:
            status = "done"
            break
        if time.time() > start + bound:
            ok = enforce_bound(pid, grace)
            mesh.log("side watch %s: bound %ss passed; killed=%s" % (id, bound, ok))
            status = "timeout"
            break
        if time.time() > self_deadline:
            mesh.log("side watch %s: own bound passed; giving up" % id)
            status = "orphan"
            break
        time.sleep(poll)
    r = finish(id, status, max_kids)
    mesh.log("side watch %s: %s" % (id, r.get("status")))
    notify(id)
    return 0


def spawn(id):
    """Start a detached watcher for `id` (the spawn_waker pattern). Returns True when one was started."""
    if os.environ.get("FLEET_SIDE_WATCH_SPAWN", "1") == "0":
        return False
    try:
        d = os.path.join(mesh.state_dir(), "log")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "side-watch.log"), "a") as errf:
            subprocess.Popen([sys.executable, os.path.join(LIB, "fleet_side.py"), "watch", id],
                             stdin=subprocess.DEVNULL, stdout=errf, stderr=errf, start_new_session=True,
                             close_fds=True)
        return True
    except OSError as e:
        mesh.log("side watch spawn %s failed: %s" % (id, e))
        return False


def cmd_watch(argv):
    ap = argparse.ArgumentParser(prog="fleet side watch")
    ap.add_argument("id")
    a = ap.parse_args(argv)
    watch(a.id)
