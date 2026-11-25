"""WATCH contract: Source interface, Event schema, watch.json config schema, snapshot() shape and source registry.

Spec: docs/design/four-desks.md sections 3.1 (interface, Event), 3.3 (config), 3.4 (credentials), 3.6 (dedupe, bounds)
and 6 (state files). Every WATCH lane builds against this file; change it only through kit-lander.

Python 3.8+ stdlib only. Nothing here does network or file I/O except load_config (reads one file) and
load_source (imports a module).

Naming note: the 3.1 example Event has the key "kind" twice (source kind "jira" and routing kind "fyi"). Here the
source's kind is `sourceKind` and `kind` is the routing class set by the filters.
"""
import dataclasses
import hashlib
import importlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_VERSION = 1

# --- source registry: kind -> module path (lib/ is on sys.path; each module defines one Source subclass) ------------
SOURCES = {
    "call": "watch_sources.call",
    "command": "watch_sources.command",
    "github": "watch_sources.github",
    "jira": "watch_sources.jira",
    "confluence": "watch_sources.confluence",
    "ado": "watch_sources.ado",
    "bitbucket": "watch_sources.bitbucket",
    "gitlab": "watch_sources.gitlab",  # GitLab and Gitea
    "webhook": "watch_sources.webhook",  # phase 6 loopback receiver
    "whosaid": "watch_sources.whosaid",  # call decisions from a local whosaid workspace (LEARN Q702)
    "linear": "watch_sources.linear",  # Linear issues over GraphQL (LEARN Q702)
}

# --- routing ---------------------------------------------------------------------------------------------------------
KIND_ASK = "ask"
KIND_TASK = "task-candidate"
KIND_FYI = "fyi"
KIND_DROP = "drop"  # internal: an event a drop filter matched; counted per rule, never written to events.jsonl
KINDS = (KIND_ASK, KIND_TASK, KIND_FYI)  # the value of Event.kind after the filters
LEARN_ITEM = "learn-item"  # an extra delivery to LEARN, not a kind: any routed event with score >= learn.minScore
ROUTING_CLASSES = KINDS + (LEARN_ITEM,)
DECIDE = "DECIDE"  # Event.to is always DECIDE (Q88); WATCH never addresses ROUTE
LEARN = "LEARN"
NEVER_TO = ("ROUTE",)
ORIGINS = ("outside", "call")

# score: the noise bar LEARN uses (learn.minScore). 0 dropped, 1 fyi, 2 state change, 3 ask.
SCORE_DROPPED, SCORE_FYI, SCORE_STATE_CHANGE, SCORE_ASK = 0, 1, 2, 3
STATE_CHANGE_TYPES = ("merged", "released", "closed", "transition")  # substrings of Event.type that score 2

# --- defaults and bounds (3.3, 3.5, 3.6) -----------------------------------------------------------------------------
DEFAULTS = {"pollSec": 900, "maxEventsPerPoll": 200, "quoteChars": 600, "batchSec": 30, "ratePerMin": 30}
POLL_FLOOR_SEC = 60
HTTP_TIMEOUT_SEC = 20
WEBHOOK_PORT = 7349  # reserved loopback ports: 7340 decisions, 7341 learn, 7342 watch page, 7349 webhook receiver
RATE_PER_MIN = 30
BACKOFF_CAP_SEC = 1800
FAIL_STREAK_NOTIFY = 3
SEEN_DAYS = 30
EID_MAX = 200
COMMAND_TIMEOUT_SEC = 60  # default timeoutSec for a command source


def state_paths(state):
    """Section 6 WATCH state files under $FLEET_STATE."""
    w = os.path.join(state, "watch")
    return {
        "config": os.path.join(state, "watch.json"),
        "dir": w,
        "events": os.path.join(w, "events.jsonl"),
        "seen": os.path.join(w, "seen.db"),
        "cursors": os.path.join(w, "cursors.json"),
        "health": os.path.join(w, "health.json"),
        "webhook": os.path.join(w, "webhook"),  # spools: webhook/<route name>.jsonl (0600, dir 0700)
    }


# --- Event -----------------------------------------------------------------------------------------------------------
def make_eid(source_kind, instance, ref, updated_at, change_kind):
    """Dedupe key `kind:instance:ref:updatedAt:changeKind`; hashed (kind:sha256) when longer than EID_MAX chars."""
    eid = ":".join(str(p) for p in (source_kind, instance, ref, updated_at, change_kind))
    if len(eid) > EID_MAX:
        eid = "%s:h:%s" % (source_kind, hashlib.sha256(eid.encode("utf-8")).hexdigest())
    return eid


def cap_quote(text, limit=DEFAULTS["quoteChars"]):
    """Cap a quote at `limit` chars (defaults.quoteChars); an over-long quote ends with '...'."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def score_for(kind, type_):
    if kind == KIND_ASK:
        return SCORE_ASK
    if kind not in KINDS:
        return SCORE_DROPPED
    if any(t in (type_ or "") for t in STATE_CHANGE_TYPES):
        return SCORE_STATE_CHANGE
    return SCORE_FYI


@dataclasses.dataclass
class Event:
    """One line of watch/events.jsonl. A Source's poll() fills the identity and content fields; the pipeline
    (scrub, dedupe, filters) fills kind, to, score, secretDropped and count."""
    eid: str  # dedupe key, make_eid()
    source: str  # config source name, e.g. "acme-tracker"
    sourceKind: str  # registry key, e.g. "jira"
    at: str  # ISO 8601 UTC of the change
    type: str  # change type, e.g. "issue.transition", "pr.review_requested", "build.failed", "utterance"
    ref: str  # PROJ-1, acme/widget#12
    url: str = ""
    actor: str = ""
    me: bool = False  # the actor is one of watch.json me.names / me.ids
    title: str = ""
    quote: str = ""  # capped at quoteChars, secret-scrubbed
    fields: Dict[str, Any] = dataclasses.field(default_factory=dict)
    tags: List[str] = dataclasses.field(default_factory=list)  # e.g. assigned-to-me, review-requested, mention
    kind: str = KIND_FYI  # routing class: ask | task-candidate | fyi
    to: str = DECIDE
    origin: str = "outside"
    score: int = SCORE_FYI
    secretDropped: bool = False
    count: int = 1  # >1 when a burst within batchSec collapsed into this event

    def burst_key(self):
        """Second dedupe key (3.6): (kind, ref, type) within batchSec collapses bursts."""
        return (self.sourceKind, self.ref, self.type)

    def is_learn_item(self, min_score=1):
        return self.kind in KINDS and self.score >= min_score

    def to_dict(self):
        return dataclasses.asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


EVENT_REQUIRED = ("eid", "source", "sourceKind", "at", "type", "ref")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def validate_event(d, quote_chars=DEFAULTS["quoteChars"]):
    """Errors for one events.jsonl row (dict). Empty list = valid."""
    errs = []
    if not isinstance(d, dict):
        return ["event is not an object"]
    for k in EVENT_REQUIRED:
        if not isinstance(d.get(k), str) or not d.get(k):
            errs.append("missing or empty %s" % k)
    if d.get("sourceKind") and d["sourceKind"] not in SOURCES:
        errs.append("unknown sourceKind %r" % d["sourceKind"])
    if d.get("at") and not _ISO.match(str(d["at"])):
        errs.append("at is not ISO 8601: %r" % d["at"])
    if len(d.get("eid") or "") > EID_MAX + 80:
        errs.append("eid longer than allowed")
    if d.get("kind", KIND_FYI) not in KINDS:
        errs.append("kind must be one of %s" % ", ".join(KINDS))
    if d.get("to", DECIDE) != DECIDE:
        errs.append("to must be DECIDE (got %r)" % d.get("to"))
    if d.get("origin", "outside") not in ORIGINS:
        errs.append("origin must be one of %s" % ", ".join(ORIGINS))
    if len(d.get("quote") or "") > quote_chars:
        errs.append("quote longer than %d chars" % quote_chars)
    if not isinstance(d.get("score", 1), int) or not 0 <= d.get("score", 1) <= 3:
        errs.append("score must be an int 0..3")
    if not isinstance(d.get("fields", {}), dict):
        errs.append("fields must be an object")
    if not isinstance(d.get("tags", []), list):
        errs.append("tags must be a list")
    if "auth" in d or "token" in d:
        errs.append("event carries an auth/token key")
    return errs


# --- Source interface (3.1) ------------------------------------------------------------------------------------------
class SourceError(Exception):
    """A poll failed. retry_after (seconds) when the server said so (Retry-After, 429)."""

    def __init__(self, msg, retry_after=None, status=None):
        super().__init__(msg)
        self.retry_after = retry_after
        self.status = status


class Source:
    """One class per source kind in lib/watch_sources/<kind>.py.

    cfg:   this source's watch.json entry, auth refs NOT resolved, plus cfg["me"] injected by the engine before
           construction: {"names": me.names, "id": me.ids[<source name>] or me.ids[<kind>] or None}
           (source_cfg_with_me). A Source reads "me" only from there, never from the top-level config.
           The engine also merges source_settings() (defaults < cfg.defaults < the entry: pollSec,
           maxEventsPerPoll, quoteChars, batchSec) into cfg. cfg["auth"] stays as the unresolved references;
           the resolved values arrive only in creds (auth.token, auth.secret, auth.password, auth.user).
    creds: resolved credentials for this poll only {"token": ..., "user": ...}; hold in memory, never log or store.
    state: a deep copy of cursors.json[name].state at construction, saved after every poll, success or failure,
           whether the Source mutates or reassigns self.state. A state that is not JSON or holds a
           secret-looking string is not saved (the previous one stays) and health.stateRefused says why.
    poll() events from a `webhook` entry keep their own source/sourceKind when the source names a configured
           entry (e.g. "oss"/github), so a delivery and the next poll of that source dedupe on one eid.
    """
    kind = ""

    def __init__(self, cfg, creds, state):
        self.cfg = cfg
        self.creds = {} if creds is None else creds
        self.state = state if state is not None else {}

    def poll(self, cursor):
        """Read-only, bounded (HTTP_TIMEOUT_SEC per call, maxEventsPerPoll). Returns
        (events: list[Event], new_cursor, next_poll_sec). next_poll_sec is a FLOOR, never a shortcut: the engine
        waits effective_wait(pollSec, next_poll_sec) = max(POLL_FLOOR_SEC, pollSec, next_poll_sec), so a Source
        returns the server's X-Poll-Interval / Retry-After there (None = no server hint). Raises SourceError on
        failure (SourceError.retry_after feeds the same floor).

        Make HTTP calls with urllib.request.urlopen(...) looked up at call time (not `from urllib.request import
        urlopen`), so the engine's per-source token bucket (ratePerMin, lib/fleet_watch_rate.py) gates them."""
        raise NotImplementedError

    def health(self):
        """{"lastOk": iso|None, "lastError": str|None, "failStreak": int, "rateRemaining": int|None}"""
        return {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None}

    def accept_webhook(self, headers, body):
        """Optional, phase 6: accept_webhook(headers, body) -> list[Event]. NORMALIZE ONLY: the loopback receiver
        (lib/watch_sources/webhook.py) has already checked the signature (VERIFIERS registry; an adapter with a
        vendor scheme registers one with register_verifier at import, e.g. basic auth for Azure DevOps, which signs
        nothing) and builds this Source with empty creds. Return the same Events poll() would make. Raise
        SourceError(status=400) for a payload it cannot map. WEBHOOK_SOURCES lists the kinds that implement it.
        The engine resolves every auth reference (token, password, secret, user) into creds under the same key.

        eid rule for a webhook-receivable source: build the eid only from fields BOTH the webhook payload and the
        poll response carry (the object's updatedAt / mergedAt / closedAt and the change type), never from a
        delivery id or an events-API event id, so a webhook and the next catch-up poll dedupe to one event."""
        raise NotImplementedError("%s has no webhook support" % (self.kind or type(self).__name__))


def effective_wait(poll_sec, next_poll_sec=None):
    """Seconds until the next poll: max(POLL_FLOOR_SEC, pollSec, next_poll_sec). next_poll_sec only raises it."""
    return max(POLL_FLOOR_SEC, int(poll_sec or 0), int(next_poll_sec or 0))


def me_for_source(cfg, src):
    """{"names": [...], "id": me.ids[source name] or me.ids[kind] or None} for one source entry."""
    me = (cfg or {}).get("me") or {}
    ids = me.get("ids") or {}
    sid = ids.get(src.get("name"))
    if sid is None:
        sid = ids.get(src.get("kind"))
    return {"names": list(me.get("names") or []), "id": sid}


def source_cfg_with_me(cfg, src):
    """The cfg a Source is constructed with: a copy of its entry plus the injected "me"."""
    out = dict(src)
    out["me"] = me_for_source(cfg, src)
    return out


# kinds whose Source implements accept_webhook (phase 6 fills this in as adapters land)
WEBHOOK_SOURCES = ("ado", "gitlab", "github", "jira", "bitbucket")


def load_source(kind):
    """Import the registered module and return its Source subclass (the one whose .kind == kind)."""
    if kind not in SOURCES:
        raise KeyError("unknown source kind %r" % kind)
    lib = os.path.dirname(os.path.abspath(__file__))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    mod = importlib.import_module(SOURCES[kind])
    # adapters import this file as `fleet_watch_contract`; a caller may hold another copy (loaded by path), so
    # accept a subclass of either copy's Source
    bases = {Source}
    canon = sys.modules.get("fleet_watch_contract")
    if canon is not None and hasattr(canon, "Source"):
        bases.add(canon.Source)
    bases = tuple(bases)
    for v in vars(mod).values():
        if isinstance(v, type) and issubclass(v, bases) and v not in bases and getattr(v, "kind", "") == kind:
            return v
    raise ImportError("%s defines no Source with kind %r" % (SOURCES[kind], kind))


# --- credentials (3.4): references only ------------------------------------------------------------------------------
CRED_SCHEMES = ("env", "keychain", "cmd")
SECRET_KEYS = ("token", "password", "secret")  # every value under these keys in an auth block must be a reference
AUTH_TYPES = ("bearer", "basic", "pat", "none", "hmac")


def parse_ref(value):
    """'env:NAME' -> ('env', 'NAME'). Raises ValueError for anything that is not a reference (a literal)."""
    if not isinstance(value, str) or ":" not in value:
        raise ValueError("not a credential reference")
    scheme, arg = value.split(":", 1)
    if scheme not in CRED_SCHEMES or not arg.strip():
        raise ValueError("not a credential reference")
    if scheme == "env" and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", arg):
        raise ValueError("env reference needs a variable name")
    return scheme, arg


def is_credential_ref(value):
    try:
        parse_ref(value)
        return True
    except ValueError:
        return False


def auth_errors(auth, where):
    """Errors for one auth block. A literal secret is reported by key only, never echoed."""
    errs = []
    if auth is None:
        return errs
    if not isinstance(auth, dict):
        return ["%s.auth must be an object" % where]
    if auth.get("type", "bearer") not in AUTH_TYPES:
        errs.append("%s.auth.type must be one of %s" % (where, ", ".join(AUTH_TYPES)))
    for k, v in auth.items():
        if k in SECRET_KEYS and not is_credential_ref(v):
            errs.append("%s.auth.%s holds a literal value; use env:, keychain: or cmd:" % (where, k))
    return errs


# --- watch.json (3.3) ------------------------------------------------------------------------------------------------
SOURCE_REQUIRED = {
    "github": ("repos",),
    "jira": ("base",),
    "confluence": ("base",),
    "ado": ("org", "project"),
    "bitbucket": ("repos",),
    "gitlab": ("base", "repos"),
    "command": ("argv",),
    "call": (),
    "webhook": ("auth",),  # the receiver refuses a route without a secret reference
    "whosaid": ("workspace",),
    "linear": ("teams",),
}
FILTER_DROP_KEYS = ("actor", "type", "actorMatches")
FILTER_WHEN_KEYS = ("tags", "type", "branchOwner", "any", "actorMatches", "source")
_HOURS = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def strip_jsonc(text):
    """Drop whole-line // comments (same rule as lib/fleet_listen.py _strip_jsonc)."""
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


def load_config(path):
    with open(path) as f:
        return json.loads(strip_jsonc(f.read()))


def validate_config(cfg):
    """Errors for a watch.json dict. Empty list = valid. `fleet watch check` exits 1 on any error."""
    errs = []
    if not isinstance(cfg, dict):
        return ["config is not an object"]
    me = cfg.get("me", {})
    if not isinstance(me, dict) or not isinstance(me.get("names", []), list) or not isinstance(me.get("ids", {}), dict):
        errs.append("me must be {names: [], ids: {}}")
    d = cfg.get("defaults", {})
    if not isinstance(d, dict):
        errs.append("defaults must be an object")
        d = {}
    for k in DEFAULTS:
        if k == "ratePerMin":  # a rate may be fractional (0.5 = one request per 2 min)
            if k in d and (isinstance(d[k], bool) or not isinstance(d[k], (int, float)) or d[k] <= 0):
                errs.append("defaults.ratePerMin must be a positive number")
        elif k in d and (not isinstance(d[k], int) or d[k] <= 0):
            errs.append("defaults.%s must be a positive int" % k)
    if isinstance(d.get("pollSec"), int) and d["pollSec"] < POLL_FLOOR_SEC:
        errs.append("defaults.pollSec below the %d s floor" % POLL_FLOOR_SEC)
    srcs = cfg.get("sources")
    if not isinstance(srcs, list) or not srcs:
        errs.append("sources must be a non-empty list")
        srcs = []
    names = set()
    for i, s in enumerate(srcs):
        where = "sources[%d]" % i
        if not isinstance(s, dict):
            errs.append("%s is not an object" % where)
            continue
        n = s.get("name")
        if not isinstance(n, str) or not n:
            errs.append("%s.name missing" % where)
        elif n in names:
            errs.append("%s.name %r repeats" % (where, n))
        else:
            names.add(n)
            where = "sources[%s]" % n
        k = s.get("kind")
        if k not in SOURCES:
            errs.append("%s.kind %r is not one of %s" % (where, k, ", ".join(SOURCES)))
        else:
            for req in SOURCE_REQUIRED[k]:
                if req not in s:
                    errs.append("%s needs %s for kind %s" % (where, req, k))
        if "pollSec" in s and (not isinstance(s["pollSec"], int) or s["pollSec"] < POLL_FLOOR_SEC):
            errs.append("%s.pollSec must be an int >= %d" % (where, POLL_FLOOR_SEC))
        for key in SECRET_KEYS:
            if key in s:
                errs.append("%s.%s outside an auth block; credentials go in auth as references" % (where, key))
        errs.extend(auth_errors(s.get("auth"), where))
    flt = cfg.get("filters", [])
    if not isinstance(flt, list):
        errs.append("filters must be a list")
        flt = []
    for i, f in enumerate(flt):
        where = "filters[%d]" % i
        if not isinstance(f, dict):
            errs.append("%s is not an object" % where)
        elif "drop" in f:
            if not isinstance(f["drop"], dict) or not set(f["drop"]) <= set(FILTER_DROP_KEYS) or not f["drop"]:
                errs.append("%s.drop keys must be from %s" % (where, ", ".join(FILTER_DROP_KEYS)))
        elif "kind" in f:
            if f["kind"] not in KINDS:
                errs.append("%s.kind must be one of %s" % (where, ", ".join(KINDS)))
            w = f.get("when")
            if not isinstance(w, dict) or not set(w) <= set(FILTER_WHEN_KEYS) or not w:
                errs.append("%s.when keys must be from %s" % (where, ", ".join(FILTER_WHEN_KEYS)))
        else:
            errs.append("%s needs drop or kind" % where)
    q = cfg.get("quiet")
    if q is not None:
        if not isinstance(q, dict) or ("hours" in q and not _HOURS.match(str(q["hours"]))):
            errs.append("quiet.hours must be HH:MM-HH:MM")
    lr = cfg.get("learn", {})
    if not isinstance(lr, dict) or not isinstance(lr.get("minScore", 1), int) or not 0 <= lr.get("minScore", 1) <= 3:
        errs.append("learn.minScore must be an int 0..3")
    probe = cfg.get("probe")
    if probe is not None:
        if not isinstance(probe, dict):
            errs.append("probe must be an object")
        else:
            errs.extend(auth_errors(probe.get("auth"), "probe"))
    return errs


def source_settings(cfg, src):
    """Effective numeric settings for one source: defaults < cfg.defaults < the source entry."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg.get("defaults") or {}).items() if k in DEFAULTS})
    out.update({k: v for k, v in src.items() if k in DEFAULTS})
    out["pollSec"] = max(POLL_FLOOR_SEC, out["pollSec"])
    return out


# --- snapshot() (for the phase 7 page and the terminal status pane) --------------------------------------------------
def empty_snapshot(at=""):
    return {
        "desk": "watch",
        "version": CONTRACT_VERSION,
        "at": at,
        "quiet": False,  # inside quiet.hours now
        "held": 0,  # asks held for the end of quiet hours
        "sources": [],  # [{name, kind, lastOk, lastError, failStreak, nextPollAt, rateRemaining}]
        "counts": {KIND_ASK: 0, KIND_TASK: 0, KIND_FYI: 0, LEARN_ITEM: 0, "dropped": {}},  # dropped: {rule: n}
        "recent": [],  # newest last, Event dicts, at most 50
    }


SNAPSHOT_SOURCE_KEYS = ("name", "kind", "lastOk", "lastError", "failStreak", "nextPollAt", "rateRemaining")


def validate_snapshot(s):
    errs = []
    if not isinstance(s, dict) or s.get("desk") != "watch":
        return ["snapshot.desk must be watch"]
    for k in empty_snapshot():
        if k not in s:
            errs.append("snapshot missing %s" % k)
    for i, src in enumerate(s.get("sources") or []):
        miss = [k for k in SNAPSHOT_SOURCE_KEYS if k not in src]
        if miss:
            errs.append("snapshot.sources[%d] missing %s" % (i, ", ".join(miss)))
        if "auth" in src:
            errs.append("snapshot.sources[%d] carries auth" % i)
    c = s.get("counts") or {}
    for k in ROUTING_CLASSES:
        if not isinstance(c.get(k), int):
            errs.append("snapshot.counts.%s must be an int" % k)
    if not isinstance(c.get("dropped"), dict):
        errs.append("snapshot.counts.dropped must be an object")
    rec = s.get("recent") or []
    if len(rec) > 50:
        errs.append("snapshot.recent holds more than 50 events")
    for i, e in enumerate(rec):
        for err in validate_event(e):
            errs.append("snapshot.recent[%d]: %s" % (i, err))
    return errs
