#!/usr/bin/env python3
"""Side runs in the roster views (issue #17): child lines under the parent lane in peer_list, fleet tick, fleet ls.

Read-only over $FLEET_STATE/sides.jsonl and the roster; never writes either.

visible(row)                -> True for a run that has not ended (no endedAt, status running or orphan)
is_orphan(row, roster)      -> True when the parent lane is parked, done, archived or not in the roster
                               (or the watcher already set status=orphan)
side_rows(roster, now=None) -> {parent: [row + {"line", "orphan"}]} for every visible run, in id order
line(row, roster, now=None) -> "  -> S3 codex gpt-x 7m/30m 2.1M tok" (+ " ORPHAN")
child_lines(name, roster)   -> the lines under one lane
gone(roster, now=None)      -> {parent: [rows]} for parents that are not in the roster at all

CLI (via lib/fleet_side.py COMMANDS): fleet side roster [--gone]
  default: one "<parent>\t<line>" per visible run whose parent is in the roster (bin/fleet cmd_ls reads it)
  --gone:  a "<parent> (gone)" header and its child lines for each parent missing from the roster
"""
import argparse
import os
import re
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_mesh as mesh  # noqa: E402
import fleet_side as side  # noqa: E402
import fleet_side_usage as usage  # noqa: E402

ORPHAN_PARENT = ("parked", "done", "archived")
INDENT = "  -> "


def visible(row):
    return not row.get("endedAt") and row.get("status", "running") in ("running", "orphan")


def is_orphan(row, roster):
    if row.get("status") == "orphan":
        return True
    p = roster.get(row.get("parent"))
    return not isinstance(p, dict) or (p.get("status") or "live") in ORPHAN_PARENT


def _with_tokens(row):
    """Stored tokens first; when unknown, a result file's tokens (cheap: one json read, no codex session scan)."""
    if row.get("tokensIn") is not None and row.get("tokensOut") is not None:
        return row
    tin, tout, src = usage.result_tokens(row.get("result"))
    if src is None:
        return row
    return dict(row, tokensIn=tin, tokensOut=tout)


def _key(row):
    m = re.fullmatch(r"S(\d+)", str(row.get("id", "")))
    return (0, int(m.group(1))) if m else (1, str(row.get("id")))


def line(row, roster, now=None):
    return "%s%s %s%s" % (INDENT, row.get("id"), usage.fmt(_with_tokens(row), now),
                          " ORPHAN" if is_orphan(row, roster) else "")


def side_rows(roster, now=None):
    out = {}
    for r in sorted((r for r in side.rows() if visible(r)), key=_key):
        out.setdefault(r.get("parent"), []).append(dict(r, line=line(r, roster, now), orphan=is_orphan(r, roster)))
    return out


def child_lines(name, roster, now=None):
    return [r["line"] for r in side_rows(roster, now).get(name, [])]


def gone(roster, now=None):
    return {p: rs for p, rs in side_rows(roster, now).items() if p not in roster}


def gone_lines(roster, now=None):
    out = []
    for p, rs in sorted(gone(roster, now).items(), key=lambda kv: str(kv[0])):
        out.append("%s (gone)" % p)
        out.extend(r["line"] for r in rs)
    return out


def cmd_roster(argv):
    ap = argparse.ArgumentParser(prog="fleet side roster")
    ap.add_argument("--gone", action="store_true")
    a = ap.parse_args(argv)
    roster = mesh.load_roster()
    now = time.time()
    if a.gone:
        for ln in gone_lines(roster, now):
            print(ln)
        return
    for p, rs in sorted(side_rows(roster, now).items(), key=lambda kv: str(kv[0])):
        if p in roster:
            for r in rs:
                print("%s\t%s" % (p, r["line"]))
