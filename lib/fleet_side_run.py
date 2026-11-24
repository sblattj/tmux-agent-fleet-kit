#!/usr/bin/env python3
"""fleet side run: launch a side run detached and register it in one step (issue #15).

  fleet side run [--parent NAME] --host H [--model M] [--bound 30m] [--log P] [--result P] -- CMD [ARGS...]

Prints `S7 pid=1234 log=... result=...` and returns at once. Python 3.8+ stdlib only.

Flow: the CLI starts a supervisor (this file, `_supervise`) in its own session, registers the supervisor pid with
fleet_side.start() (which spawns the #14 watcher), hands the supervisor the row over a pipe, and gets the child
pid back. The supervisor starts CMD in a second new session (no controlling tty, stdin /dev/null, stdout and stderr
to the log) with the env contract FLEET_SIDE / FLEET_PARENT / FLEET_NAME / FLEET_SIDE_RESULT, points the row's
pid at the child, then waits. On exit it reads the status with WNOWAIT (the child stays a zombie, so its pid is still
"alive" to the watcher), records `end` plus tokens from fleet_side_usage, and only then reaps it. So a watcher never
sees the pid gone before the exit code is in the row. A SIGTERM/SIGINT/SIGHUP to the supervisor goes to the child's
process group.
"""
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_mesh as mesh  # noqa: E402
_main = sys.modules.get("__main__")
if os.path.basename(getattr(_main, "__file__", "") or "") == "fleet_side.py":
    side = _main  # `fleet side run` runs fleet_side.py as __main__: raise the SideError its main() catches
else:
    import fleet_side as side  # noqa: E402
import fleet_side_usage as usage_mod  # noqa: E402

HANDSHAKE_SEC = 10


def side_dir(sid):
    return os.path.join(mesh.state_dir(), "sides", sid)


def _split(argv):
    if "--" not in argv:
        return argv, []
    i = argv.index("--")
    return argv[:i], argv[i + 1:]


def _status_of(row, code):
    """code: exit code >= 0, or -signum. Returns (status, exit)."""
    if code >= 0:
        return ("done" if code == 0 else "failed"), code
    started = row.get("startedAt")
    over = False
    try:
        import datetime
        t0 = datetime.datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        over = time.time() >= t0.timestamp() + int(row.get("boundSec") or 0)
    except (TypeError, ValueError):
        pass
    return ("timeout" if over else "killed"), 128 - code


def _record(sid, code):
    row = side.get(sid) or {}
    status, ex = _status_of(row, code)
    row = side.end(sid, status, exit=ex)
    u = usage_mod.usage(row)

    def fill(r):
        changed = False
        for k in ("tokensIn", "tokensOut"):
            if r.get(k) is None and u.get(k) is not None:
                r[k] = u[k]
                changed = True
        return r if changed else None
    side.update(sid, fill)


def supervise():
    """Child of the CLI, in its own session. stdin: one JSON line {id, parent, log, result, cmd}; stdout: the pid."""
    req = json.loads(sys.stdin.readline() or "null")
    if not req:
        return 1
    sid = req["id"]
    env = dict(os.environ, FLEET_SIDE=sid, FLEET_PARENT=req["parent"], FLEET_NAME=req["parent"],
               FLEET_SIDE_RESULT=req["result"])
    os.makedirs(os.path.dirname(req["log"]), exist_ok=True)
    os.makedirs(os.path.dirname(req["result"]), exist_ok=True)
    with open(req["log"], "ab") as logf, open(os.devnull, "rb") as devnull:
        try:
            child = subprocess.Popen(req["cmd"], stdin=devnull, stdout=logf, stderr=logf, env=env,
                                     start_new_session=True, close_fds=True)
        except OSError as e:
            logf.write(("fleet side run: cannot start %s: %s\n" % (req["cmd"][0], e)).encode())
            logf.flush()
            side.end(sid, "failed", exit=127)
            sys.stdout.write("ERR %s\n" % e)
            sys.stdout.flush()
            return 1
    pid = child.pid

    def fwd(signum, _frame):
        try:
            os.killpg(pid, signum)
        except OSError:
            pass
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, fwd)

    def point(r):
        r.update(pid=pid, supervisorPid=os.getpid())
        return r
    side.update(sid, point)
    try:
        sys.stdout.write("%d\n" % pid)
        sys.stdout.flush()
    except OSError:
        pass
    # the CLI has what it needs: drop the pipes so it can return
    fd = os.open(os.devnull, os.O_RDWR)
    for n in (0, 1, 2):
        os.dup2(fd, n)
    while True:
        try:
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            break
        except InterruptedError:
            continue
        except ChildProcessError:
            info = None
            break
    if info is None:
        code = -signal.SIGKILL
    elif info.si_code == os.CLD_EXITED:
        code = info.si_status
    else:
        code = -info.si_status
    try:
        _record(sid, code)
    finally:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return 0


def cmd_run(argv):
    import argparse
    opts, cmd = _split(argv)
    ap = argparse.ArgumentParser(prog="fleet side run", usage="fleet side run [--parent NAME] --host H [--model M] "
                                 "[--bound 30m] [--log P] [--result P] -- CMD [ARGS...]")
    ap.add_argument("--parent")
    ap.add_argument("--host", required=True)
    ap.add_argument("--model")
    ap.add_argument("--bound")
    ap.add_argument("--log")
    ap.add_argument("--result")
    a = ap.parse_args(opts)
    if not cmd:
        raise side.SideError("no command (put it after --)")
    if a.bound is not None:
        side.parse_bound(a.bound)
    sup = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_supervise"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                           close_fds=True, text=True)
    try:
        row = side.start(parent=a.parent, host=a.host, model=a.model, pid=sup.pid, log=a.log, result=a.result,
                         bound=a.bound, source="run")
    except BaseException:
        sup.stdin.close()
        sup.kill()
        sup.wait()
        raise
    sid = row["id"]
    log = row["log"] or os.path.join(side_dir(sid), "log")
    result = row["result"] or os.path.join(side_dir(sid), "result.json")

    def paths(r):
        r.update(log=log, result=result)
        return r
    side.update(sid, paths)
    sup.stdin.write(json.dumps({"id": sid, "parent": row["parent"], "log": log, "result": result,
                                "cmd": cmd}) + "\n")
    sup.stdin.close()
    line = sup.stdout.readline().strip()
    sup.stdout.close()
    if not line.isdigit():
        raise side.SideError("side run %s did not start: %s" % (sid, line or "supervisor exited"))
    print("%s pid=%s log=%s result=%s" % (sid, line, log, result))


if __name__ == "__main__":
    if sys.argv[1:2] == ["_supervise"]:
        sys.exit(supervise())
    sys.exit(side.main(["run"] + sys.argv[1:]))
