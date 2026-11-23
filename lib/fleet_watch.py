#!/usr/bin/env python3
"""fleet watch: the WATCH desk engine (docs/design/four-desks.md 2.1, 3, 6).

  source poll -> normalize -> secret scrub -> dedupe -> filters -> route
  ask            -> decision_open (from WATCH, to DECIDE), held during quiet hours
  task-candidate -> one TASK-CANDIDATE message to DECIDE per run
  fyi            -> one FYI message to DECIDE per run
  score >= learn.minScore -> one LEARN-ITEM message to LEARN per run (origin as the event)

WATCH never addresses ROUTE: every delivery goes through _send(), which refuses ROUTE and the ROUTE desk name.

State, all under $FLEET_STATE/watch/ (this process writes nowhere else except through fleet_mesh deliver and
decision_open):
  events.jsonl   routed events, append-only, secret-scrubbed, quote-capped
  seen.db        dedupe keys (sqlite3), sha256 of each eid so no event text lands in the db, kept SEEN_DAYS days
  cursors.json   per source: cursor, source state (ETag...), nextPollAt, failStreak, backoff, paused
  health.json    per source: lastOk, lastError, failStreak, nextPollAt, rateRemaining
  stats.json     cumulative counts per kind and dropped per rule (fleet watch stats)
  held.jsonl     asks held during quiet hours, opened as one batch after
  watch.log      one line per run and per failure; counts and names only, never event text

Verbs shared by the status pane and `fleet watch serve` (same function, same state):
  snapshot(state, now=None)      read-only, the contract shape
  poll_now(state, name)          cursors.json[name].nextPollAt = now
  pause(state, name), resume(state, name)   cursors.json[name].paused
  release_held(state, now=None)  held.jsonl -> one decision_open per held ask; returns the Q ids
  pane_key(state, key, selected) p poll_now, z pause, r resume, h release_held

Python 3.8+ stdlib only.
"""
import argparse
import contextlib
import datetime
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import os
import random
import re
import select
import shutil
import sqlite3
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import fleet_watch_contract as C  # noqa: E402
import fleet_watch_creds as creds_mod  # noqa: E402
import fleet_watch_rate as rate  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(LIB, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mesh = _load("fleet_mesh")
_listen = None


def is_secret(text):
    global _listen
    if _listen is None:
        _listen = _load("fleet_listen")
    return _listen.is_secret(text)


WATCH_NAME = "WATCH"
ASK_ALLOW = ["pick", "own", "snooze", "refuse"]
ASK_OPTIONS = ["handle it", "not mine"]
RECENT_MAX = 50


# ---------------------------------------------------------------- time

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def in_quiet(quiet, now, tz=None):
    """quiet = {"hours": "19:00-08:00"}; local time of `now` (tz overrides the local zone)."""
    if not isinstance(quiet, dict) or not quiet.get("hours"):
        return False
    a, b = quiet["hours"].split("-")
    loc = now.astimezone(tz) if tz else now.astimezone()
    m = loc.hour * 60 + loc.minute
    sa = int(a[:2]) * 60 + int(a[3:])
    sb = int(b[:2]) * 60 + int(b[3:])
    if sa == sb:
        return False
    return sa <= m < sb if sa < sb else (m >= sa or m < sb)


# ---------------------------------------------------------------- files

def paths(state):
    p = C.state_paths(state)
    w = p["dir"]
    p.update(stats=os.path.join(w, "stats.json"), held=os.path.join(w, "held.jsonl"),
             log=os.path.join(w, "watch.log"), lock=os.path.join(w, ".lock"))
    return p


def _ensure_dir(state):
    d = paths(state)["dir"]
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _append(path, lines):
    if not lines:
        return
    with open(path, "a") as f:
        for ln in lines:
            f.write(ln + "\n")


def _read_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _tail_jsonl(path, n, max_bytes=1 << 20):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]
    out = []
    for ln in lines[-(n + 1):]:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln.decode("utf-8", "replace")))
            except ValueError:
                pass
    return out[-n:]


@contextlib.contextmanager
def _locked(state):
    _ensure_dir(state)
    with open(paths(state)["lock"], "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def wlog(state, msg, now=None):
    _ensure_dir(state)
    _append(paths(state)["log"], ["%s %s" % (iso(now or utcnow()), msg)])


def redact(text):
    """Error text from a source may carry a credential: a secret-looking string is replaced whole."""
    text = str(text or "")
    return "(redacted: looked like a secret)" if is_secret(text) else text[:300]


# ---------------------------------------------------------------- seen.db

class Seen:
    """Dedupe keys. Stores sha256(eid) and the first-seen epoch; rows older than SEEN_DAYS are purged."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY, at INTEGER NOT NULL)")

    @staticmethod
    def key(eid):
        return hashlib.sha256(eid.encode("utf-8")).hexdigest()

    def has(self, eid):
        return self.db.execute("SELECT 1 FROM seen WHERE k = ?", (self.key(eid),)).fetchone() is not None

    def add(self, eid, at):
        self.db.execute("INSERT OR IGNORE INTO seen (k, at) VALUES (?, ?)", (self.key(eid), int(at)))

    def purge(self, now_epoch, days=C.SEEN_DAYS):
        cur = self.db.execute("DELETE FROM seen WHERE at < ?", (int(now_epoch - days * 86400),))
        return cur.rowcount

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def close(self):
        self.db.commit()
        self.db.close()


# ---------------------------------------------------------------- filters

def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _type_match(pats, t):
    return any(fnmatch.fnmatchcase(t or "", p) for p in _as_list(pats))


def rule_label(f):
    body = f.get("drop") if "drop" in f else f.get("when")
    parts = []
    for k in sorted(body or {}):
        v = body[k]
        parts.append("%s=%s" % (k, ",".join(str(x) for x in _as_list(v))))
    return ("drop " if "drop" in f else "%s " % f.get("kind")) + " ".join(parts)


class Me:
    def __init__(self, cfg):
        me = cfg.get("me") or {}
        self.names = [n for n in me.get("names") or [] if n]
        self.ids = dict(me.get("ids") or {})

    def ids_for(self, ev):
        out = set(self.names)
        for k in (ev.source, ev.sourceKind):
            if self.ids.get(k):
                out.add(self.ids[k])
        return out

    def all(self):
        return set(self.names) | set(v for v in self.ids.values() if v)


def match_drop(d, ev, me):
    for k, v in d.items():
        if k == "actor":
            vals = _as_list(v)
            hit = ("me" in vals and ev.me) or (ev.actor in [x for x in vals if x != "me"])
            if not hit:
                return False
        elif k == "type":
            if not _type_match(v, ev.type):
                return False
        elif k == "actorMatches":
            if not re.search(v, ev.actor or ""):
                return False
    return True


def match_when(w, ev, me):
    for k, v in w.items():
        if k == "any":
            if not v:
                return False
        elif k == "tags":
            if not set(_as_list(v)) & set(ev.tags or []):
                return False
        elif k == "type":
            if not _type_match(v, ev.type):
                return False
        elif k == "branchOwner":
            owner = (ev.fields or {}).get("branchOwner")
            want = me.ids_for(ev) if v == "me" else set(_as_list(v))
            if owner not in want and not (v == "me" and "my-branch" in (ev.tags or [])):
                return False
        elif k == "actorMatches":
            if not re.search(v, ev.actor or ""):
                return False
        elif k == "source":
            if ev.source not in _as_list(v):
                return False
    return True


def classify(filters, ev, me):
    """First match wins. Returns (kind, label): kind is ask|task-candidate|fyi, or KIND_DROP with the rule label."""
    for f in filters or []:
        if "drop" in f:
            if match_drop(f["drop"], ev, me):
                return C.KIND_DROP, rule_label(f)
        elif "kind" in f and match_when(f.get("when") or {}, ev, me):
            return f["kind"], rule_label(f)
    return C.KIND_DROP, "no rule matched"


# ---------------------------------------------------------------- pipeline steps

def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


STR_FIELDS = ("eid", "source", "sourceKind", "at", "type", "ref", "url", "actor", "title", "quote")


def well_typed(ev):
    """A program or adapter may hand over wrong types (a numeric title); such an event is invalid, not a crash."""
    if any(not isinstance(getattr(ev, k), str) for k in STR_FIELDS):
        return False
    if not isinstance(ev.fields, dict) or not isinstance(ev.tags, list) or not isinstance(ev.me, bool):
        return False
    return bool(ev.eid and ev.type and ev.ref)


def has_secret(ev):
    """Checked on the full, uncapped event (a cap could cut a secret in half and hide it from the patterns)."""
    return any(is_secret(s) for s in _strings(ev.to_dict()))


def normalize(ev, src, settings, me, names=()):
    """Stamp source/sourceKind from the polled entry, except for a `webhook` entry: its events keep the source they
    belong to (e.g. source "oss", sourceKind github) when that names a configured source, so a delivery and the next
    catch-up poll of that source share eids, burst keys and attribution."""
    keep = (src["kind"] == "webhook" and ev.source in names and ev.sourceKind in C.SOURCES
            and ev.sourceKind != "webhook")
    if not keep:
        ev.source = src["name"]
        ev.sourceKind = src["kind"]
    ev.origin = "call" if ev.sourceKind == "call" else "outside"
    if not isinstance(ev.fields, dict):
        ev.fields = {}
    ev.tags = [str(t) for t in (ev.tags or []) if t]
    mine = me.ids_for(ev)
    ev.me = bool(ev.me) or (ev.actor in mine)
    text = " ".join([ev.title or "", ev.quote or ""])
    for n in me.all():
        if re.search(r"(?<![\w@])@%s(?![\w-])" % re.escape(n), text, re.I):
            if "mention" not in ev.tags:
                ev.tags.append("mention")
            break
    if len(ev.eid) > C.EID_MAX:
        ev.eid = "%s:h:%s" % (ev.sourceKind, hashlib.sha256(ev.eid.encode("utf-8")).hexdigest())
    return ev


def cap(ev, settings):
    ev.quote = C.cap_quote(ev.quote, settings["quoteChars"])
    ev.title = C.cap_quote(ev.title, 300)
    return ev


def ask_question(ev):
    q = '%s %s: %s %s "%s"' % (ev.source, ev.ref, ev.actor or "someone", ev.type, ev.quote or ev.title)
    if ev.url:
        q += " %s" % ev.url
    return q


def watch_meta(ev):
    m = {"sourceKind": ev.sourceKind, "source": ev.source, "ref": ev.ref, "eid": ev.eid}
    if re.match(r"^https?://", ev.url or ""):
        m["url"] = ev.url
    return m


# ---------------------------------------------------------------- delivery (never ROUTE)

class RouteRefused(RuntimeError):
    pass


def _forbidden():
    return set(C.NEVER_TO) | {mesh.orch_name()}


def _send(to, text):
    if to in _forbidden():
        raise RouteRefused("WATCH never addresses %s" % to)
    return mesh.deliver(WATCH_NAME, to, text)


def _open_decision(question, ev=None, options=None):
    desk = mesh.desk_name()
    if desk in _forbidden():
        raise RouteRefused("WATCH never addresses %s" % desk)
    args = {"from": WATCH_NAME, "question": question, "options": list(options or ASK_OPTIONS), "allow": ASK_ALLOW}
    if ev is not None:
        args["watch"] = watch_meta(ev)
    res = mesh.t_decision_open(args)
    m = re.match(r"opened (Q\d+)", res or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- engine

class Engine:
    """One run over the configured sources. `sources` overrides the registry (kind -> Source class) for tests;
    `now` is a callable returning an aware datetime; `tz` sets the quiet-hours zone (default: local)."""

    def __init__(self, state, cfg, now=None, sources=None, tz=None, env=None, rng=None, sleep=None):
        self.state = state
        self.cfg = cfg
        self.now = now or utcnow
        self.overrides = sources or {}
        self.tz = tz
        self.env = env
        self.rng = rng or random.Random()
        self.sleep = sleep or time.sleep  # the rate gate's short waits (tests pass one that moves their clock)
        self.p = paths(state)
        self.me = Me(cfg)

    # --- state helpers
    def _load_cursors(self):
        return _read_json(self.p["cursors"], {})

    def _stats(self):
        s = _read_json(self.p["stats"], {})
        for k in C.ROUTING_CLASSES:
            s.setdefault(k, 0)
        s.setdefault("dropped", {})
        s.setdefault("runs", 0)
        return s

    def _source_class(self, kind):
        return self.overrides.get(kind) or C.load_source(kind)

    def _src_cfg(self, src, settings):
        c = C.source_cfg_with_me(self.cfg, src)
        c.update({k: settings[k] for k in C.DEFAULTS})  # defaults < cfg.defaults < the entry (source_settings)
        return c

    def _delay_ok(self, settings, next_sec):
        return C.effective_wait(settings["pollSec"], next_sec)

    def _delay_fail(self, streak, retry_after):
        base = min(C.BACKOFF_CAP_SEC, C.POLL_FLOOR_SEC * (2 ** max(0, streak - 1)))
        base = base * (0.8 + 0.4 * self.rng.random())
        return C.effective_wait(min(C.BACKOFF_CAP_SEC, base), retry_after)

    # --- one run
    def run_once(self, force=False, only=None):
        errs = C.validate_config(self.cfg) + rate.config_errors(self.cfg)
        if errs:
            raise ValueError("watch.json invalid: " + "; ".join(errs))
        desk = mesh.desk_name()
        if desk in _forbidden():
            raise RouteRefused("the DECIDE desk name resolves to %s; WATCH never addresses ROUTE" % desk)
        with _locked(self.state):
            return self._run_locked(force, only)

    def _run_locked(self, force, only):
        now = self.now()
        now_s = iso(now)
        cursors = self._load_cursors()
        health = _read_json(self.p["health"], {})
        stats = self._stats()
        seen = Seen(self.p["seen"])
        purged = seen.purge(now.timestamp())
        filters = self.cfg.get("filters") or []
        min_score = (self.cfg.get("learn") or {}).get("minScore", 1)
        quiet_cfg = self.cfg.get("quiet") or {}
        quiet = in_quiet(quiet_cfg, now, self.tz)
        hold = quiet and quiet_cfg.get("decideHolds", True)
        result = {"at": now_s, "polled": [], "skipped": [], "failed": [], "deferred": [], "events": 0,
                  C.KIND_ASK: 0, C.KIND_TASK: 0, C.KIND_FYI: 0, C.LEARN_ITEM: 0, "held": 0, "dropped": {},
                  "purged": purged}
        routed, secret_sources, fail_notes = [], {}, []
        try:
            for src in self.cfg.get("sources") or []:
                name = src["name"]
                if only and name not in only:
                    continue
                cur = cursors.setdefault(name, {})
                settings = C.source_settings(self.cfg, src)
                if cur.get("paused"):
                    result["skipped"].append(name)
                    continue
                nxt = parse_iso(cur.get("nextPollAt"))
                if not force and nxt and nxt > now:
                    result["skipped"].append(name)
                    continue
                evs = self._poll_one(src, settings, cur, now, result, fail_notes)
                if evs is None:
                    continue
                result["polled"].append(name)
                batch = self._pipeline(evs, src, settings, seen, filters, min_score, now, result, secret_sources)
                routed.extend(batch)
            # persist events, then seen, then route (a crash after write but before route re-routes nothing,
            # because the eids are already seen: lost delivery beats a duplicate ask)
            lines = [ev.to_json() for ev in routed]
            _append(self.p["events"], lines)
            for ev in routed:
                seen.add(ev.eid, now.timestamp())
            for eid in result.pop("_seen_extra", []):
                seen.add(eid, now.timestamp())
        finally:
            seen.close()
        self._route(routed, min_score, hold, result)
        if not quiet:
            ids = release_held(self.state, now=now, _locked_already=True)
            if ids:
                result["released"] = ids
        for src_name, n in secret_sources.items():
            _send(mesh.desk_name(), "FYI WATCH: a secret appeared in %s; %d event(s) dropped, nothing stored"
                  % (src_name, n))
        for note in fail_notes:
            _send(mesh.desk_name(), note)
        # stats, cursors, health
        for k in C.KINDS + (C.LEARN_ITEM,):
            stats[k] += result[k]
        for rule, n in result["dropped"].items():
            stats["dropped"][rule] = stats["dropped"].get(rule, 0) + n
        stats["runs"] += 1
        stats["lastRun"] = now_s
        _write_json(self.p["stats"], stats)
        _write_json(self.p["cursors"], cursors)
        for src in self.cfg.get("sources") or []:
            cur = cursors.get(src["name"], {})
            h = health.get(src["name"], {})
            h.update(name=src["name"], kind=src["kind"], nextPollAt=cur.get("nextPollAt"),
                     failStreak=cur.get("failStreak", 0), paused=bool(cur.get("paused")))
            for k in ("lastOk", "lastError", "rateRemaining"):
                h.setdefault(k, None)
            health[src["name"]] = h
        for name, h in result.pop("_health", {}).items():
            health.setdefault(name, {}).update(h)
        _write_json(self.p["health"], health)
        wlog(self.state, "run polled=%s skipped=%s failed=%s ask=%d task=%d fyi=%d learn=%d held=%d dropped=%s"
             % (",".join(result["polled"]) or "-", ",".join(result["skipped"]) or "-",
                ",".join(result["failed"]) or "-", result[C.KIND_ASK], result[C.KIND_TASK], result[C.KIND_FYI],
                result[C.LEARN_ITEM], result["held"], json.dumps(result["dropped"], sort_keys=True))
             + (" deferred=%s" % ",".join(result["deferred"]) if result["deferred"] else ""), now)
        return result

    def _poll_one(self, src, settings, cur, now, result, fail_notes):
        name = src["name"]
        hs = result.setdefault("_health", {}).setdefault(name, {})
        bucket = rate.TokenBucket.from_dict(rate.rate_per_min(self.cfg, src, C.RATE_PER_MIN), cur.get("bucket"))
        wait = bucket.wait_for(now.timestamp())
        if wait > rate.RATE_MAX_WAIT_SEC:  # the bucket cannot pay for even the first request: defer, call nothing
            cur["bucket"] = bucket.to_dict()
            return self._deferred(src, cur, now, wait, result)
        gate = rate.Gate(bucket, lambda: self.now().timestamp(), self.sleep)
        try:
            creds = creds_mod.resolve_auth(src.get("auth"), self.env)
        except creds_mod.Refused as e:
            return self._failed(src, cur, now, "credential: %s" % e, None, result, fail_notes, hs)
        try:
            cls = self._source_class(src["kind"])
            inst = cls(self._src_cfg(src, settings), creds, json.loads(json.dumps(cur.get("state") or {})))
            with rate.gated(gate):
                evs, new_cursor, next_sec = inst.poll(cur.get("cursor"))
        except rate.RateDeferred as e:
            return self._deferred(src, cur, now, e.wait, result)
        except C.SourceError as e:
            return self._failed(src, cur, now, str(e), e.retry_after, result, fail_notes, hs, inst_health(locals()))
        except (ImportError, KeyError) as e:
            return self._failed(src, cur, now, "no adapter for kind %s: %s" % (src["kind"], e), None, result,
                                fail_notes, hs)
        except Exception as e:  # an adapter crash is a failure, never a dead engine
            return self._failed(src, cur, now, "%s: %s" % (type(e).__name__, e), None, result, fail_notes, hs,
                                inst_health(locals()))
        finally:
            creds = None  # noqa: F841 (drop the only reference to the resolved values)
            cur["bucket"] = bucket.to_dict()
            if gate.calls or gate.waited:
                hs["rate"] = {"calls": gate.calls, "waitedSec": round(gate.waited, 3), "perMin": bucket.per_min}
            if locals().get("inst") is not None:
                self._keep_state(src, cur, inst.state, now, hs)
        cur["cursor"] = new_cursor
        cur["failStreak"] = 0
        cur.pop("notified", None)
        cur["lastOk"] = iso(now)
        cur["nextPollAt"] = iso(now + datetime.timedelta(seconds=self._delay_ok(settings, next_sec)))
        h = {}
        try:
            h = inst.health() or {}
        except Exception:
            pass
        rem = h.get("rateRemaining")
        hs.update(lastOk=iso(now), lastError=None, rateRemaining=rem if rem is not None else gate.remaining)
        return list(evs or [])[: settings["maxEventsPerPoll"]]

    def _deferred(self, src, cur, now, wait, result):
        """Rate-limited, not failed: cursor and failStreak stay, the next poll waits for the bucket (and the floor)."""
        delay = max(C.POLL_FLOOR_SEC, int(wait + 0.999))
        cur["nextPollAt"] = iso(now + datetime.timedelta(seconds=delay))
        result["deferred"].append(src["name"])
        wlog(self.state, "deferred %s: rate limit, next poll in %ds" % (src["name"], delay), now)
        return None

    def _keep_state(self, src, cur, st, now, hs):
        """Persist Source.state (ETag, item memory) into cursors.json[name].state, restored at construction.
        A state that is not JSON, or holds a secret-looking string, is not saved (the previous one stays)."""
        try:
            text = json.dumps(st if st is not None else {}, sort_keys=True)
        except (TypeError, ValueError):
            hs["stateRefused"] = "state not JSON-serializable; kept the previous state"
            wlog(self.state, "state %s: %s" % (src["name"], hs["stateRefused"]), now)
            return
        if is_secret(text):
            hs["stateRefused"] = "state held a secret-looking string; not saved (kept the previous state)"
            wlog(self.state, "state %s: %s" % (src["name"], hs["stateRefused"]), now)
            return
        hs["stateRefused"] = None
        cur["state"] = json.loads(text)

    def _failed(self, src, cur, now, msg, retry_after, result, fail_notes, hs, src_health=None):
        name = src["name"]
        msg = redact(msg)
        streak = int(cur.get("failStreak", 0)) + 1
        cur["failStreak"] = streak
        cur["nextPollAt"] = iso(now + datetime.timedelta(seconds=self._delay_fail(streak, retry_after)))
        hs.update(lastError=msg)
        if src_health and src_health.get("rateRemaining") is not None:
            hs["rateRemaining"] = src_health["rateRemaining"]
        result["failed"].append(name)
        wlog(self.state, "fail %s streak=%d next=%s: %s" % (name, streak, cur["nextPollAt"], msg), now)
        if streak >= C.FAIL_STREAK_NOTIFY and not cur.get("notified"):
            cur["notified"] = True
            fail_notes.append("FYI WATCH: source %s failed %d times in a row (%s); quiet until it recovers"
                              % (name, streak, msg))
        return None

    def _pipeline(self, evs, src, settings, seen, filters, min_score, now, result, secret_sources):
        dropped = result["dropped"]
        extra = result.setdefault("_seen_extra", [])
        batch_eids = set()
        bursts = {}
        out = []
        names = {s.get("name") for s in self.cfg.get("sources") or []}

        def drop(rule):
            dropped[rule] = dropped.get(rule, 0) + 1

        for ev in evs:
            if not isinstance(ev, C.Event):
                try:  # an Event from another copy of the contract module, or a plain dict
                    ev = C.Event.from_dict(ev.to_dict() if hasattr(ev, "to_dict") else dict(ev))
                except (TypeError, ValueError):
                    drop("invalid")
                    continue
            if not well_typed(ev):
                drop("invalid")
                continue
            try:
                ev = normalize(ev, src, settings, self.me, names)
                secret = has_secret(ev)
            except Exception:  # never let one odd event end the run
                drop("invalid")
                continue
            if secret:
                if ev.eid in batch_eids or seen.has(ev.eid):
                    drop("duplicate")  # already noted once; no second note
                    continue
                drop("secret")
                secret_sources[src["name"]] = secret_sources.get(src["name"], 0) + 1
                extra.append(ev.eid)
                batch_eids.add(ev.eid)
                continue
            ev = cap(ev, settings)
            if ev.eid in batch_eids or seen.has(ev.eid):
                drop("duplicate")
                continue
            batch_eids.add(ev.eid)
            kind, label = classify(filters, ev, self.me)
            if kind == C.KIND_DROP:
                drop(label)
                extra.append(ev.eid)
                continue
            at = parse_iso(ev.at) or now
            bk = ev.burst_key() + (kind,)  # only same-kind events collapse: a mention never folds into an fyi
            first = bursts.get(bk)
            if first is not None and abs((at - first[1]).total_seconds()) <= settings["batchSec"]:
                first[0].count += 1
                drop("burst collapsed")
                extra.append(ev.eid)
                continue
            ev.kind = kind
            ev.to = C.DECIDE
            ev.score = C.score_for(kind, ev.type)
            ev.secretDropped = False
            errs = C.validate_event(ev.to_dict(), settings["quoteChars"])
            if errs:
                drop("invalid")
                extra.append(ev.eid)
                continue
            bursts[bk] = (ev, at)
            out.append(ev)
        return out

    def _route(self, routed, min_score, hold, result):
        desk = mesh.desk_name()
        tasks, fyis, learn = [], [], []
        for ev in routed:
            result[ev.kind] += 1
            if ev.kind == C.KIND_ASK:
                if hold:
                    _append(self.p["held"], [json.dumps({"question": ask_question(ev), "watch": watch_meta(ev)},
                                                        sort_keys=True)])
                    result["held"] += 1
                else:
                    _open_decision(ask_question(ev), ev)
            elif ev.kind == C.KIND_TASK:
                tasks.append('TASK-CANDIDATE %s %s "%s" %s' % (ev.eid, ev.ref, ev.quote or ev.title, ev.url))
            else:
                fyis.append(ev.eid)
            if ev.is_learn_item(min_score):
                learn.append("LEARN-ITEM %s" % ev.eid)
        result["events"] = len(routed)
        result[C.LEARN_ITEM] = len(learn)
        if tasks:
            _send(desk, "\n".join(tasks))
        if fyis:
            _send(desk, "FYI %d events: %s" % (len(fyis), " ".join(fyis)))
        if learn:
            _send(C.LEARN if not os.environ.get("FLEET_LEARN") else os.environ["FLEET_LEARN"], "\n".join(learn))


def inst_health(loc):
    inst = loc.get("inst")
    if inst is None:
        return None
    try:
        return inst.health()
    except Exception:
        return None


# ---------------------------------------------------------------- verbs (pane and serve)

def _config_path(state):
    return C.state_paths(state)["config"]


def _cfg_or_empty(state):
    try:
        return C.load_config(_config_path(state))
    except (OSError, ValueError):
        return {}


def _source_names(state):
    return [s.get("name") for s in _cfg_or_empty(state).get("sources") or [] if isinstance(s, dict)]


def _edit_cursor(state, name, fn):
    names = _source_names(state)
    if name not in names:
        raise KeyError("no source named %r" % name)
    with _locked(state):
        p = paths(state)["cursors"]
        cur = _read_json(p, {})
        fn(cur.setdefault(name, {}))
        _write_json(p, cur)


def poll_now(state, name, now=None):
    _edit_cursor(state, name, lambda c: c.update(nextPollAt=iso(now or utcnow())))
    return "%s polls on the next run" % name


def pause(state, name):
    _edit_cursor(state, name, lambda c: c.update(paused=True))
    return "%s paused" % name


def resume(state, name):
    _edit_cursor(state, name, lambda c: c.update(paused=False))
    return "%s resumed" % name


def release_held(state, now=None, _locked_already=False):
    """Open every held ask as its own decision (each with its `watch` object), in order. Returns the list of
    Q ids opened ([] when nothing is held)."""
    ctx = contextlib.nullcontext() if _locked_already else _locked(state)
    with ctx:
        p = paths(state)["held"]
        held = _read_jsonl(p)
        if not held:
            return []
        desk = mesh.desk_name()
        if desk in _forbidden():
            raise RouteRefused("WATCH never addresses %s" % desk)
        # claim the file first: a crash mid-way loses the rest rather than opening asks twice
        os.replace(p, p + ".releasing")
        ids = []
        for h in held:
            args = {"from": WATCH_NAME, "question": "(held for quiet hours) " + h["question"],
                    "options": ASK_OPTIONS, "allow": ASK_ALLOW}
            if h.get("watch"):
                args["watch"] = h["watch"]
            m = re.match(r"opened (Q\d+)", mesh.t_decision_open(args) or "")
            if m:
                ids.append(m.group(1))
        os.remove(p + ".releasing")
        return ids


PANE_KEYS = {"p": "poll_now", "z": "pause", "r": "resume", "h": "release_held"}


def pane_key(state, key, selected):
    if key == "p" and selected:
        return poll_now(state, selected)
    if key == "z" and selected:
        return pause(state, selected)
    if key == "r" and selected:
        return resume(state, selected)
    if key == "h":
        ids = release_held(state)
        return "released %d held asks: %s" % (len(ids), " ".join(ids)) if ids else "nothing held"
    return ""


def snapshot(state, now=None):
    now = now or utcnow()
    p = paths(state)
    cfg = _cfg_or_empty(state)
    snap = C.empty_snapshot(iso(now))
    snap["quiet"] = in_quiet(cfg.get("quiet"), now)
    snap["held"] = len(_read_jsonl(p["held"]))
    health = _read_json(p["health"], {})
    cursors = _read_json(p["cursors"], {})
    for s in cfg.get("sources") or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        h = health.get(s["name"], {})
        c = cursors.get(s["name"], {})
        snap["sources"].append({"name": s["name"], "kind": s.get("kind"), "lastOk": h.get("lastOk"),
                                "lastError": h.get("lastError"), "failStreak": c.get("failStreak", 0),
                                "nextPollAt": c.get("nextPollAt"), "rateRemaining": h.get("rateRemaining"),
                                "paused": bool(c.get("paused")), "stateRefused": h.get("stateRefused")})
    st = _read_json(p["stats"], {})
    for k in C.ROUTING_CLASSES:
        snap["counts"][k] = int(st.get(k, 0))
    snap["counts"]["dropped"] = dict(st.get("dropped") or {})
    snap["recent"] = [e for e in _tail_jsonl(p["events"], RECENT_MAX) if not C.validate_event(e)]
    return snap


# ---------------------------------------------------------------- terminal status pane

def _one(s):
    return " ".join(str(s if s is not None else "").split())


def _fit(s, w):
    s = str(s)
    return s if len(s) <= w else s[: max(0, w - 3)] + "..."


def render(snap, selected=None, width=100, msg=""):
    c = snap["counts"]
    out = ["WATCH  %s%s  ask %d  task %d  fyi %d  learn %d  held %d" % (
        snap["at"], "  QUIET" if snap["quiet"] else "", c[C.KIND_ASK], c[C.KIND_TASK], c[C.KIND_FYI],
        c[C.LEARN_ITEM], snap["held"])]
    out.append("")
    out.append("  %-18s %-9s %-21s %-21s %-6s %s" % ("source", "kind", "last ok", "next poll", "fails", "error"))
    for s in snap["sources"]:
        mark = ">" if s["name"] == selected else " "
        err = s["lastError"] or ""
        if s.get("paused"):
            err = "PAUSED " + err
        line = "%s %-18s %-9s %-21s %-21s %-6s %s" % (
            mark, _fit(_one(s["name"]), 18), _fit(_one(s["kind"]), 9), s["lastOk"] or "-", s["nextPollAt"] or "-",
            s["failStreak"], _one(err))
        out.append(_fit(line, width))
    if c["dropped"]:
        out.append("")
        out.append("  dropped: " + ", ".join("%s %d" % (k, v) for k, v in sorted(c["dropped"].items())))
    out.append("")
    out.append("  recent:")
    for e in snap["recent"][-10:]:
        out.append(_fit("  %-14s %-14s %-14s %s \"%s\"" % (e["kind"], _one(e["source"]), _one(e["ref"]), _one(e["type"]),
                                                           _one(e.get("quote") or e.get("title"))), width))
    out.append("")
    out.append("  j/k select  p poll now  z pause  r resume  h release held  q quit" + ("   " + msg if msg else ""))
    return "\n".join(out)


def status_pane(state, interval=5.0):
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sel_i, msg = 0, ""
    try:
        tty.setcbreak(fd)
        while True:
            snap = snapshot(state)
            names = [s["name"] for s in snap["sources"]]
            sel = names[sel_i % len(names)] if names else None
            w = shutil.get_terminal_size((100, 30)).columns
            sys.stdout.write("\x1b[H\x1b[2J" + render(snap, sel, w, msg) + "\n")
            sys.stdout.flush()
            r, _, _ = select.select([sys.stdin], [], [], interval)
            if not r:
                continue
            k = sys.stdin.read(1)
            if k == "q":
                return 0
            if k == "j":
                sel_i += 1
            elif k == "k":
                sel_i -= 1
            else:
                try:
                    msg = pane_key(state, k, sel)
                except KeyError as e:
                    msg = str(e)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------- CLI

def state_dir():
    return mesh.state_dir()


def _load_cfg_or_die(path):
    try:
        return C.load_config(path)
    except OSError as e:
        sys.stderr.write("fleet watch: cannot read %s: %s\n" % (path, e.strerror or e))
        sys.exit(1)
    except ValueError as e:
        sys.stderr.write("fleet watch: %s is not valid JSON: %s\n" % (path, e))
        sys.exit(1)


def cmd_check(a):
    path = a.config or _config_path(state_dir())
    cfg = _load_cfg_or_die(path)
    errs = C.validate_config(cfg) + rate.config_errors(cfg)
    rows = creds_mod.check_refs(cfg, resolve=a.resolve) if isinstance(cfg, dict) else []
    for e in errs:
        print("FAIL %s" % e)
    for where, ok, note in rows:
        if ok:
            print("ok   %s: %s" % (where, note))
        elif not any(note == e for e in errs):
            print("FAIL %s: %s" % (where, note))
    bad = bool(errs) or any(not ok for _, ok, _ in rows)
    print("fleet watch check: %s (%s)" % ("FAIL" if bad else "ok", path))
    return 1 if bad else 0


def cmd_run(a):
    state = state_dir()
    path = a.config or _config_path(state)
    cfg = _load_cfg_or_die(path)
    errs = C.validate_config(cfg) + rate.config_errors(cfg)
    if errs:
        for e in errs:
            sys.stderr.write("fleet watch: %s\n" % e)
        return 1
    eng = Engine(state, cfg)
    while True:
        try:
            res = eng.run_once(force=a.force, only=a.source or None)
        except RouteRefused as e:
            sys.stderr.write("fleet watch: refused: %s\n" % e)
            return 1
        if a.json:
            print(json.dumps(res, sort_keys=True))
        else:
            print("fleet watch: polled %s; ask %d, task-candidate %d, fyi %d, learn-item %d, held %d; failed %s"
                  % (",".join(res["polled"]) or "none", res[C.KIND_ASK], res[C.KIND_TASK], res[C.KIND_FYI],
                     res[C.LEARN_ITEM], res["held"], ",".join(res["failed"]) or "none")
                  + ("; deferred %s (rate limit)" % ",".join(res["deferred"]) if res.get("deferred") else ""))
        sys.stdout.flush()
        if a.once:
            return 0
        a.force = False
        cur = _read_json(paths(state)["cursors"], {})
        nxt = [parse_iso(v.get("nextPollAt")) for v in cur.values() if v.get("nextPollAt") and not v.get("paused")]
        wait = min([(t - utcnow()).total_seconds() for t in nxt if t] or [60])
        time.sleep(max(5, min(60, wait)))


def cmd_status(a):
    state = state_dir()
    if a.once or not sys.stdin.isatty():
        print(render(snapshot(state), a.select))
        return 0
    return status_pane(state, a.interval)


def cmd_stats(a):
    snap = snapshot(state_dir())
    if a.json:
        print(json.dumps(snap["counts"], sort_keys=True))
        return 0
    c = snap["counts"]
    for k in C.ROUTING_CLASSES:
        print("%-15s %d" % (k, c[k]))
    print("dropped by rule:")
    for k, v in sorted(c["dropped"].items(), key=lambda kv: (-kv[1], kv[0])):
        print("  %-40s %d" % (k, v))
    return 0


def cmd_snapshot(a):
    print(json.dumps(snapshot(state_dir()), sort_keys=True, indent=None if a.compact else 1))
    return 0


def cmd_verb(a):
    state = state_dir()
    try:
        if a.cmd == "release-held":
            ids = release_held(state)
            print("released %d held asks: %s" % (len(ids), " ".join(ids)) if ids else "nothing held")
        else:
            print({"poll-now": poll_now, "pause": pause, "resume": resume}[a.cmd](state, a.name))
    except KeyError as e:
        sys.stderr.write("fleet watch: %s\n" % e)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet watch", description="WATCH desk engine (four-desks.md section 3)")
    sp = ap.add_subparsers(dest="cmd")
    r = sp.add_parser("run", help="poll due sources and route events to DECIDE and LEARN")
    r.add_argument("--once", action="store_true", help="one pass, then exit")
    r.add_argument("--force", action="store_true", help="poll every source now, ignoring nextPollAt")
    r.add_argument("--source", action="append", help="only this source (repeatable)")
    r.add_argument("--config", help="watch.json path (default $FLEET_STATE/watch.json)")
    r.add_argument("--json", action="store_true")
    c = sp.add_parser("check", help="validate watch.json; exit 1 on a literal credential or any error")
    c.add_argument("--config")
    c.add_argument("--resolve", action="store_true", help="also resolve each reference (never prints a value)")
    s = sp.add_parser("status", help="terminal status pane")
    s.add_argument("--once", action="store_true", help="print one frame and exit")
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--select", help="source to mark selected (with --once)")
    t = sp.add_parser("stats", help="counts per kind and dropped per rule")
    t.add_argument("--json", action="store_true")
    n = sp.add_parser("snapshot", help="snapshot() as JSON")
    n.add_argument("--compact", action="store_true")
    for v in ("poll-now", "pause", "resume"):
        x = sp.add_parser(v)
        x.add_argument("name")
    sp.add_parser("release-held", help="open held quiet-hours asks now, as one decision")
    a = ap.parse_args(argv)
    fn = {"run": cmd_run, "check": cmd_check, "status": cmd_status, "stats": cmd_stats, "snapshot": cmd_snapshot,
          "poll-now": cmd_verb, "pause": cmd_verb, "resume": cmd_verb, "release-held": cmd_verb}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 2
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
