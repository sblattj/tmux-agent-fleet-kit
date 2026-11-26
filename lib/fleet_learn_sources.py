"""LEARN's own reads of the real sources (Q702): git hosts, trackers and calls through WATCH's adapter registry.

One shared config names which git repos and which trackers: `$FLEET_STATE/sources.json`, the same `sources` list
shape as watch.json (lib/fleet_watch_contract.py validate_config), with refs-only credentials (env:, keychain:,
cmd:). Without it LEARN reads the `sources` of `$FLEET_STATE/watch.json`, so one file can serve both desks. Use
the same source names as WATCH: an event's eid carries the source name, and LEARN keys rows by eid, so a row
WATCH delivered and the same change LEARN polled are one row.

No config, no polling: nothing here reaches the network unless a config under $FLEET_STATE names a source.

WATCH reads sources.json too when it has no watch.json of its own (lib/fleet_watch.py _config_path), so one file
names the sources for both desks.

This module reuses the registry (C.load_source), the credential resolver (fleet_watch_creds.resolve_auth) and
WATCH's own event steps (fleet_watch.normalize, has_secret, match_drop on the `drop` filters, cap); it never runs
WATCH's Engine (that opens DECIDE questions and writes WATCH state).

Drain: a source caps each poll (whosaid reads at most maxMeetingsPerPoll meetings), so poll_all polls a source again
from its new cursor until it has nothing more, at most DRAIN_ROUNDS rounds. A rebuild that hits the bound fails
closed; a sync reports it and carries on from the saved cursor next time.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fleet_watch_contract as C  # noqa: E402
import fleet_watch_creds as creds_mod  # noqa: E402
import fleet_watch as FW  # noqa: E402

OVERRIDES = {}  # kind -> Source class, for tests
DRAIN_ROUNDS = 50  # bound on polls per source per sync or rebuild


class SourceFailed(Exception):
    """A configured source failed; a rebuild aborts with the old LEARN state intact."""

    def __init__(self, name, msg):
        super().__init__("source %s failed: %s" % (name, msg))
        self.name = name


def _state():
    import fleet_mesh as mesh
    return mesh.state_dir()


def config_path():
    """(path, which) of the config LEARN reads, or (None, None)."""
    st = _state()
    for name in ("sources.json", "watch.json"):
        p = os.path.join(st, name)
        if os.path.exists(p):
            return p, name
    return None, None


def load_config():
    p, which = config_path()
    if not p:
        return None
    cfg = C.load_config(p)
    if not isinstance(cfg, dict):
        raise ValueError("%s is not an object" % which)
    return cfg


def check(cfg=None, resolve=False):
    """[(where, ok, note)]: config shape errors plus each auth block (references only; resolve=True resolves)."""
    cfg = load_config() if cfg is None else cfg
    if cfg is None:
        return [("config", True, "no sources.json or watch.json: LEARN reads only local files")]
    rows = [("config", False, e) for e in C.validate_config(cfg)]
    rows += creds_mod.check_refs(cfg, resolve=resolve)
    if not rows:
        rows.append(("config", True, "%d source(s), references only" % len(cfg.get("sources") or [])))
    return rows


def _source_class(kind):
    return OVERRIDES.get(kind) or C.load_source(kind)


def _dropped(cfg, ev, me):
    """Why WATCH would drop this event before it reaches anyone: "secret", a drop-filter label, or None."""
    if FW.has_secret(ev):
        return "secret"
    for f in cfg.get("filters") or []:
        if isinstance(f, dict) and isinstance(f.get("drop"), dict) and FW.match_drop(f["drop"], ev, me):
            return FW.rule_label(f)
    return None


def _more(src_obj, events, cur, prev, next_poll):
    """Poll this source again? An adapter that knows it stopped early says so (src.more); otherwise a poll that
    moved the cursor and returned events may have left some behind. A rate-limit hint (next_poll) always stops."""
    if next_poll:
        return False
    flag = getattr(src_obj, "more", None)
    if flag is not None:
        return bool(flag)
    return bool(events) and cur != prev


def poll_all(full=False, cursors=None, cfg=None, env=None):
    """Poll every configured source into memory, draining each until it has nothing more (DRAIN_ROUNDS polls at
    most). full=True (a rebuild) ignores cursors and reads each source from the start. Each event goes through
    WATCH's normalize, has_secret and drop filters, then cap. Returns {"events": [event dict], "cursors": {name:
    cursor}, "sources": [summary], "secretDropped": n, "dropped": {label: n}, "drainCapped": [name]}. Raises
    SourceFailed on the first failing source, and on a rebuild that hits the drain bound (nothing is written by this
    function)."""
    cfg = load_config() if cfg is None else cfg
    out = {"events": [], "cursors": {}, "sources": [], "secretDropped": 0, "dropped": {}, "drainCapped": []}
    if not cfg:
        return out
    errs = C.validate_config(cfg)
    if errs:
        raise SourceFailed("config", "; ".join(errs))
    cursors = {} if full else dict(cursors or {})
    me = FW.Me(cfg)
    names = {s.get("name") for s in cfg.get("sources") or []}
    for src in cfg.get("sources") or []:
        name, kind = src.get("name"), src.get("kind")
        if kind in ("webhook", "call"):  # receivers and the live call: WATCH's alone, never polled by LEARN
            continue
        try:
            auth = creds_mod.resolve_auth(src.get("auth"), env)
        except creds_mod.Refused as e:
            raise SourceFailed(name, str(e))
        settings = C.source_settings(cfg, src)
        try:
            cls = _source_class(kind)
            s = cls(C.source_cfg_with_me(cfg, src), auth, {})
        except (ImportError, KeyError) as e:
            raise SourceFailed(name, "no adapter: %s" % e)
        cur, seen, n, rounds, more = cursors.get(name), set(), 0, 0, True
        while more and rounds < DRAIN_ROUNDS:
            rounds += 1
            try:
                events, new_cur, next_poll = s.poll(cur)
            except C.SourceError as e:
                raise SourceFailed(name, str(e))
            more = _more(s, events, new_cur, cur, next_poll)
            cur = new_cur
            for raw in events or []:
                try:
                    ev = raw if isinstance(raw, C.Event) else C.Event.from_dict(
                        raw.to_dict() if hasattr(raw, "to_dict") else dict(raw))
                except (TypeError, ValueError):
                    out["dropped"]["invalid"] = out["dropped"].get("invalid", 0) + 1
                    continue
                ev = FW.normalize(ev, src, settings, me, names)
                if ev.eid in seen:
                    continue  # a drain round can hand back an event an earlier round already judged
                seen.add(ev.eid)
                why = _dropped(cfg, ev, me)
                if why == "secret":
                    out["secretDropped"] += 1
                    continue
                if why:
                    out["dropped"][why] = out["dropped"].get(why, 0) + 1
                    continue
                out["events"].append(FW.cap(ev, settings).to_dict())
                n += 1
        if more:
            if full:
                raise SourceFailed(name, "still more after %d polls; nothing changed" % DRAIN_ROUNDS)
            out["drainCapped"].append(name)
        out["cursors"][name] = cur
        out["sources"].append({"name": name, "kind": kind, "events": n, "polls": rounds})
    return out
