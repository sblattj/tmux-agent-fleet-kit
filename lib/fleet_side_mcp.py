#!/usr/bin/env python3
"""MCP tools for side runs: side_start / side_end / side_list (issue #16).

Thin wrappers over lib/fleet_side.py; lib/fleet_mesh.py lists them in TOOLS through t_side_* functions that import
this module on first call (fleet_side imports fleet_mesh, so the import is lazy). Errors are raised and surface as
`isError` through fleet_mesh.call_tool. See INTERFACES.md ("Side runs over MCP").
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_side as side  # noqa: E402
import fleet_side_usage as usage  # noqa: E402


def side_start(args):
    bound = args.get("bound")
    row = side.start(parent=args.get("parent"), host=args.get("host"), model=args.get("model"), pid=args.get("pid"),
                     log=args.get("log"), result=args.get("result"), bound=bound if bound not in ("", None) else None,
                     source="mcp", sessionId=args.get("sessionId"))
    return row["id"]


def side_end(args):
    sid = args.get("id")
    if not sid:
        raise side.SideError("`id` is required")
    status = args.get("status") or "done"
    if status not in side.END_STATUSES:
        raise side.SideError("bad end status %r (use %s)" % (status, " or ".join(side.END_STATUSES)))
    row = side.end(sid, status, exit=args.get("exit"))
    return "%s %s endedAt=%s" % (row["id"], row["status"], row["endedAt"])


def line(row):
    return "%s %s %s" % (row.get("id"), row.get("status"), usage.fmt(row))


def side_list(args):
    parent = args.get("parent") or os.environ.get("FLEET_NAME") or "*"
    rs = side.rows(parent=None if parent == "*" else parent)
    if not args.get("all"):
        rs = [r for r in rs if not r.get("endedAt")]
    if not rs:
        return "no side runs"
    return "\n".join(line(r) for r in rs)
