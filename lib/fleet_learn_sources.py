"""LEARN's own reads of the real sources (Q702): git hosts, trackers and calls through WATCH's adapter registry.

One shared config names which git repos and which trackers: `$FLEET_STATE/sources.json`, the same `sources` list
shape as watch.json (lib/fleet_watch_contract.py validate_config), with refs-only credentials (env:, keychain:,
cmd:). Without it LEARN reads the `sources` of `$FLEET_STATE/watch.json`, so one file can serve both desks. Use
the same source names as WATCH: an event's eid carries the source name, and LEARN keys rows by eid, so a row
WATCH delivered and the same change LEARN polled are one row.

No config, no polling: nothing here reaches the network unless a config under $FLEET_STATE names a source.

This module reuses the registry (C.load_source), the credential resolver (fleet_watch_creds.resolve_auth) and the
listen secret patterns; it never runs WATCH's Engine (that opens DECIDE questions and writes WATCH state).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fleet_watch_contract as C  # noqa: E402
import fleet_watch_creds as creds_mod  # noqa: E402

OVERRIDES = {}  # kind -> Source class, for tests
_listen = None


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


def _is_secret(text):
    global _listen
    if _listen is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("fleet_listen", os.path.join(HERE, "fleet_listen.py"))
        _listen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_listen)
    return _listen.is_secret(text)


def _source_class(kind):
    return OVERRIDES.get(kind) or C.load_source(kind)


def poll_all(full=False, cursors=None, cfg=None, env=None):
    """Poll every configured source into memory. full=True (a rebuild) ignores cursors and reads each source's
    whole first-poll window. Returns {"events": [event dict], "cursors": {name: cursor}, "sources": [summary]}.
    Raises SourceFailed on the first failing source (nothing is written by this function)."""
    cfg = load_config() if cfg is None else cfg
    out = {"events": [], "cursors": {}, "sources": [], "secretDropped": 0}
    if not cfg:
        return out
    errs = C.validate_config(cfg)
    if errs:
        raise SourceFailed("config", "; ".join(errs))
    cursors = {} if full else dict(cursors or {})
    for src in cfg.get("sources") or []:
        name, kind = src.get("name"), src.get("kind")
        if kind in ("webhook", "call"):  # receivers and the live call: WATCH's alone, never polled by LEARN
            continue
        try:
            auth = creds_mod.resolve_auth(src.get("auth"), env)
        except creds_mod.Refused as e:
            raise SourceFailed(name, str(e))
        try:
            cls = _source_class(kind)
            s = cls(C.source_cfg_with_me(cfg, src), auth, {})
            events, cur, _next = s.poll(cursors.get(name))
        except C.SourceError as e:
            raise SourceFailed(name, str(e))
        except (ImportError, KeyError) as e:
            raise SourceFailed(name, "no adapter: %s" % e)
        n = 0
        for ev in events:
            d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
            if _is_secret(json.dumps(d, sort_keys=True)):
                out["secretDropped"] += 1
                continue
            out["events"].append(d)
            n += 1
        out["cursors"][name] = cur
        out["sources"].append({"name": name, "kind": kind, "events": n})
    return out
