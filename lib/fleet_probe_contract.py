"""DECIDE probe contract: probes.json (standing targets), probes.jsonl (one row per probe), nuke state, the
decisions-log rows DECIDE writes alone, and the fixed `fleet probe` read verbs.

Spec: docs/design/four-desks.md section 2.5 and the phase 4 row of section 7. Owner: kit-lander; phase 4 builds
against it. Python 3.8+ stdlib only, no I/O.
"""
import os
import re

CONTRACT_VERSION = 1

STALL_SEC = 15 * 60  # a probe lane with no output for 15 min is parked by ROUTE as stalled (Q105)
DECIDED_BY_PROBE = "decide:probe"
CHOICES = ("yes", "no", "nuke")  # every "found X, start a lane?" call offers these three (Q106)

# the only verbs `fleet probe` runs (tool allowlist layer); none can write
READ_VERBS = ("git-log", "git-show", "git-diff", "git-blame", "gh-get", "ci-status", "read")
STAT_VERBS = ("stats", "nuked", "resume")

PROBE_STATUS = ("queued", "running", "done", "stalled", "failed")
# actions DECIDE logs alone in decisions-log.jsonl (decidedBy decide:probe, human false)
ALONE_ACTIONS = ("probe-start", "probe-nothing", "probe-found", "fyi-forward")
HUMAN_ACTIONS = ("nuke", "resume")  # logged too, but the human made them


def state_paths(state):
    d = os.path.join(state, "decide")
    return {
        "dir": d,
        "targets": os.path.join(d, "probes.json"),
        "rows": os.path.join(d, "probes.jsonl"),
        "nuked": os.path.join(d, "probes-nuked.json"),
        "scratch": os.path.join(d, "probes"),  # throwaway clones: probes/<id>/, deleted after
        "log": os.path.join(d, "probe.log"),  # every `fleet probe` verb call, for the no-write test
    }


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _iso_or_none(v):
    return v is None or (isinstance(v, str) and bool(_ISO.match(v)))


# probes.json: {"targets": [{"target": "acme/widget", "what": "open issues labelled good-first-issue",
#                            "source": "oss", "everySec": 3600}]}
TARGET_REQUIRED = ("target", "what")


def validate_targets(doc):
    errs = []
    if not isinstance(doc, dict) or not isinstance(doc.get("targets"), list):
        return ["probes.json must be {targets: [...]}"]
    seen = set()
    for i, t in enumerate(doc["targets"]):
        where = "targets[%d]" % i
        if not isinstance(t, dict):
            errs.append("%s is not an object" % where)
            continue
        for k in TARGET_REQUIRED:
            if not isinstance(t.get(k), str) or not t.get(k):
                errs.append("%s.%s missing" % (where, k))
        if t.get("target") in seen:
            errs.append("%s.target %r repeats" % (where, t.get("target")))
        seen.add(t.get("target"))
        if "everySec" in t and (not isinstance(t["everySec"], int) or t["everySec"] <= 0):
            errs.append("%s.everySec must be a positive int" % where)
        if any(k in t for k in ("token", "auth", "password")):
            errs.append("%s carries credentials; probes use watch.json probe.auth" % where)
    return errs


# probes.jsonl row (2.5): {id, target, lane, cli, startedAt, endedAt, tokensIn, tokensOut, found, callId}
# plus phase 4: status, and the event id that prompted the probe when there was one.
ROW_REQUIRED = ("id", "target", "lane", "cli", "startedAt", "endedAt", "tokensIn", "tokensOut", "found", "callId")
CLIS = ("claude", "codex", "opencode", "kiro", "agy")


def new_row(id_, target, lane, cli, started_at):
    return {"id": id_, "target": target, "lane": lane, "cli": cli, "startedAt": started_at, "endedAt": None,
            "tokensIn": None, "tokensOut": None, "found": False, "callId": None, "status": "running", "eid": None}


def validate_row(r):
    errs = []
    if not isinstance(r, dict):
        return ["row is not an object"]
    for k in ROW_REQUIRED:
        if k not in r:
            errs.append("missing %s" % k)
    if "id" in r and not (isinstance(r["id"], str) and _ID.match(r["id"])):
        errs.append("id must match %s" % _ID.pattern)
    for k in ("target", "lane"):
        if k in r and (not isinstance(r[k], str) or not r[k]):
            errs.append("%s must be a non-empty string" % k)
    if "cli" in r and r["cli"] not in CLIS:
        errs.append("cli must be one of %s" % ", ".join(CLIS))
    if "startedAt" in r and not (isinstance(r["startedAt"], str) and _ISO.match(r["startedAt"])):
        errs.append("startedAt must be ISO 8601")
    if "endedAt" in r and not _iso_or_none(r["endedAt"]):
        errs.append("endedAt must be ISO 8601 or null")
    for k in ("tokensIn", "tokensOut"):  # null when the CLI does not report usage (UNVERIFIED for kiro, opencode)
        if k in r and r[k] is not None and (not isinstance(r[k], int) or r[k] < 0):
            errs.append("%s must be a non-negative int or null" % k)
    if "found" in r and not isinstance(r["found"], bool):
        errs.append("found must be a bool")
    if r.get("found") is False and r.get("callId"):
        errs.append("callId set on a probe that found nothing (nothing found is logged only)")
    if r.get("status", "done") not in PROBE_STATUS:
        errs.append("status must be one of %s" % ", ".join(PROBE_STATUS))
    return errs


# probes-nuked.json: {"<target>": {"nukedAt": iso, "callId": "Q12"}}; `fleet probe resume <target>` deletes the key
def validate_nuked(doc):
    if not isinstance(doc, dict):
        return ["probes-nuked.json must be an object"]
    errs = []
    for t, v in doc.items():
        if not isinstance(v, dict) or not _iso_or_none(v.get("nukedAt")) or not v.get("nukedAt"):
            errs.append("nuked[%s] needs nukedAt" % t)
    return errs


# decisions-log.jsonl rows DECIDE writes for probes; the base shape is {"id","at","asker","verbatim","relayedTo"}
# (INTERFACES.md, Shared decisions log)
def log_row(action, at, target, verbatim="", call_id=None, relayed_to=None):
    human = action in HUMAN_ACTIONS
    return {"id": call_id, "at": at, "asker": "DECIDE", "verbatim": verbatim, "relayedTo": relayed_to,
            "action": action, "target": target, "decidedBy": "human" if human else DECIDED_BY_PROBE, "human": human}


def validate_log_row(r):
    errs = []
    if not isinstance(r, dict):
        return ["log row is not an object"]
    a = r.get("action")
    if a not in ALONE_ACTIONS + HUMAN_ACTIONS:
        errs.append("action must be one of %s" % ", ".join(ALONE_ACTIONS + HUMAN_ACTIONS))
    if not (isinstance(r.get("at"), str) and _ISO.match(r["at"])):
        errs.append("at must be ISO 8601")
    if not r.get("target"):
        errs.append("target missing")
    if a in ALONE_ACTIONS and (r.get("decidedBy") != DECIDED_BY_PROBE or r.get("human") is not False):
        errs.append("alone action needs decidedBy decide:probe and human false")
    if a in HUMAN_ACTIONS and r.get("human") is not True:
        errs.append("nuke/resume rows are the human's: human true")
    return errs
