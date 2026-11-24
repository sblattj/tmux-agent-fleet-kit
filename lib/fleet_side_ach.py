#!/usr/bin/env python3
"""Side runs from the ach run registry (issue #19): `fleet side adopt-ach [--apply]`.

ach writes one record per run to <state>/runs/<runId>.json, state = $AGENTIC_CODING_HARNESS_STATE_DIR or
~/.agentic-coding-harness (agentic-coding-harness src/core/store.ts stateDir, src/core/registry.ts RunRecord).
No change to ach: this module only reads those records.

adopt(apply=False) -> [(verdict, record, detail)]   verdict "register" | "registered" | "skip"
finish_status(row) -> None while the run's record says running, else "done" | "failed" (for the #14 watcher)
tick_line(apply)   -> what `fleet tick` prints (quiet unless something matches)

A record is a candidate when status=running and its pid is alive. Its parent lane is the first roster lane whose
`pid` or tmux `#{pane_pid}` is on the record pid's ppid chain. Already registered = a side row with source=ach and
the same achRunId.
"""
import fcntl
import json
import os
import re
import subprocess
import sys

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_mesh as mesh  # noqa: E402
import fleet_side as side  # noqa: E402

TERMINAL = {"success": "done", "error": "failed", "aborted": "failed", "unavailable": "failed",
            "interrupted": "failed"}
DEFAULT_BOUND = "30m"


def ach_state():
    return os.environ.get("AGENTIC_CODING_HARNESS_STATE_DIR") or \
        os.path.join(os.path.expanduser("~"), ".agentic-coding-harness")


def _load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def records():
    """(path, record) for every parseable run record, oldest file name first."""
    d = os.path.join(ach_state(), "runs")
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(d, n)
        r = _load(p)
        if r and r.get("runId"):
            out.append((p, r))
    return out


def is_run_record(d):
    """An ach RunRecord (registry file), as opposed to a RunResult (result.json)."""
    return isinstance(d, dict) and "runId" in d and isinstance(d.get("totals"), dict) and "status" in d


# ---------------------------------------------------------------- process ancestry

def _ps():
    """{pid: (ppid, args)} from one ps call."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    t = {}
    for ln in out.splitlines():
        f = ln.split(None, 2)
        if len(f) >= 2 and f[0].isdigit() and f[1].isdigit():
            t[int(f[0])] = (int(f[1]), f[2] if len(f) > 2 else "")
    return t


def _pane_pids():
    """{pane_id: pane_pid} from the fleet's tmux server ($FLEET_TMUX_SOCKET), {} when there is none."""
    try:
        p = subprocess.run(mesh._tmux_argv("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"),
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for ln in p.stdout.splitlines() if p.returncode == 0 else []:
        f = ln.split()
        if len(f) == 2 and f[1].isdigit():
            out[f[0]] = int(f[1])
    return out


def lane_pids(roster=None, panes=None):
    """{pid: lane} for every roster lane's `pid` and its pane's #{pane_pid}."""
    roster = mesh.load_roster() if roster is None else roster
    panes = _pane_pids() if panes is None else panes
    out = {}
    for name in sorted(roster):
        e = roster[name]
        if not isinstance(e, dict) or e.get("status") in ("parked", "archived"):
            continue
        for pid in (e.get("pid"), panes.get(e.get("pane") or "")):
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if pid > 1:
                out.setdefault(pid, name)
    return out


def lane_for(pid, lanes, table):
    """The lane whose pid is on pid's ppid chain (pid itself excluded), or None."""
    seen = set()
    cur = table.get(pid, (None,))[0]
    while cur and cur > 1 and cur not in seen:
        if cur in lanes:
            return lanes[cur]
        seen.add(cur)
        cur = table.get(cur, (None,))[0]
    return None


# ---------------------------------------------------------------- model

def _output_dir(args):
    m = re.search(r"--output-dir(?:=|\s+)(\S+)", args or "")
    return m.group(1) if m else None


def model_of(rec, args=None):
    """metadata.model, else invocation.json's model from the run's --output-dir (read from its argv)."""
    md = rec.get("metadata")
    if isinstance(md, dict) and isinstance(md.get("model"), str) and md["model"]:
        return md["model"]
    od = _output_dir(args)
    if od:
        od = os.path.expanduser(od)
        if not os.path.isabs(od) and rec.get("cwd"):
            od = os.path.join(rec["cwd"], od)  # relative to the ach run's cwd, not ours
        inv = _load(os.path.join(od, "invocation.json"))
        if inv and isinstance(inv.get("model"), str) and inv["model"]:
            return inv["model"]
    return None


# ---------------------------------------------------------------- adopt

def adopt(apply=False):
    """Scan and (with apply) register. --apply holds $FLEET_STATE/side-adopt-ach.lock for the whole scan, so two
    concurrent adopters can't both register one run."""
    if not apply:
        return _adopt(False)
    lk = os.path.join(mesh.state_dir(), "side-adopt-ach.lock")
    os.makedirs(os.path.dirname(lk), exist_ok=True)
    with open(lk, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            return _adopt(True)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _adopt(apply):
    bound = side.parse_bound(os.environ.get("FLEET_SIDE_ACH_BOUND") or DEFAULT_BOUND)
    known = set(r.get("achRunId") for r in side.rows() if r.get("source") == "ach")
    table = _ps()
    lanes = lane_pids()
    out = []
    for path, rec in records():
        if rec.get("status") != "running":
            continue
        rid = rec["runId"]
        if rid in known:
            continue
        pid = rec.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or not side.pid_alive(pid):
            out.append(("skip", rec, "pid %s not alive" % (pid,)))
            continue
        lane = lane_for(pid, lanes, table)
        if not lane:
            out.append(("skip", rec, "no lane ancestor for pid %d" % pid))
            continue
        fields = dict(parent=lane, host=rec.get("agent") or "ach", model=model_of(rec, table.get(pid, (0, ""))[1]),
                      pid=pid, log=rec.get("rawTranscript"), result=os.path.abspath(path), bound=bound,
                      source="ach", sessionId=rec.get("sessionId"), achRunId=rid)
        if not apply:
            out.append(("register", rec, fields))
            continue
        try:
            row = side.start(**fields)
        except side.SideError as e:
            out.append(("skip", rec, str(e)))
            continue
        known.add(rid)
        out.append(("registered", rec, row))
    return out


def _line(verdict, rec, detail):
    if verdict == "registered":
        r = detail
        return "%s parent=%s host=%s source=ach achRunId=%s pid=%s" % (r["id"], r["parent"], r["host"],
                                                                         r["achRunId"], r["pid"])
    if verdict == "register":
        f = detail
        return "would register parent=%s host=%s source=ach achRunId=%s pid=%s" % (f["parent"], f["host"],
                                                                                   f["achRunId"], f["pid"])
    return "skip %s: %s" % (rec.get("runId"), detail)


def tick_lines(apply=False):
    """For `fleet tick`: only matches (would register / registered), never skip noise."""
    return [_line(*x) for x in adopt(apply) if x[0] != "skip"]


# ---------------------------------------------------------------- completion (for the #14 watcher)

def finish_status(row):
    """For a source=ach row: None while its run record says running (or can't be read), else done|failed from
    the record's terminal status. None for any other row, so the watcher's own rules apply."""
    if row.get("source") != "ach":
        return None
    rec = _load(row.get("result"))
    if not is_run_record(rec):
        return None
    return TERMINAL.get(rec.get("status"))


# ---------------------------------------------------------------- CLI

def cmd_adopt_ach(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="fleet side adopt-ach")
    ap.add_argument("--apply", action="store_true", help="register the matches (default: dry run)")
    a = ap.parse_args(argv)
    try:
        res = adopt(apply=a.apply)
    except side.SideError as e:  # fleet_side's own class, not __main__'s when run as `fleet side`
        sys.stderr.write("FAIL %s\n" % e)
        sys.exit(1)
    for x in res:
        print(_line(*x))
    if not res:
        print("(no running ach runs to adopt)")


if __name__ == "__main__":
    try:
        if "--tick" in sys.argv[1:]:
            for ln in tick_lines(apply="--apply" in sys.argv[1:]):
                print("side adopt-ach: " + ln)
        else:
            cmd_adopt_ach(sys.argv[1:])
    except side.SideError as e:
        sys.stderr.write("FAIL %s\n" % e)
        sys.exit(1)
