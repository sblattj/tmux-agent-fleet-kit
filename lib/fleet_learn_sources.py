"""LEARN reads the real sources (Q702) through WATCH's adapter registry, never a fork of it.

The shared config is `$FLEET_STATE/sources.json`, in the watch.json shape (`sources`, `me`, `defaults`, `filters`)
and checked by the same validator (`fleet_watch_contract.validate_config`). WATCH reads it too when it has no
watch.json of its own, so one file names the git repos and trackers for both desks. With no sources.json LEARN falls
back to watch.json. Credentials are references only (env:NAME, keychain:SERVICE, cmd:...), resolved per poll by
lib/fleet_watch_creds.py; a literal secret fails validation and nothing is polled.

poll_all() polls every configured source (not `webhook` or `call`: those push to WATCH and reach LEARN through
watch/events.jsonl and decisions-log.jsonl) with LEARN's own cursors, drops events that carry a secret or match a
`drop` filter, and returns (source, events) pairs. event_row() turns one Event into add_item() arguments, typed links
(Q697) included. One failing source is counted and never stops the rest.

Python 3.8+ stdlib only.
"""
import os
import sys

_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_mesh as mesh  # noqa: E402
import fleet_watch as FW  # noqa: E402
import fleet_watch_contract as C  # noqa: E402
import fleet_watch_creds as CR  # noqa: E402

PUSH_KINDS = ("webhook", "call")
STATE_CHANGE = ("merged", "release", "transition", "closed", "started")


def config_path(state=None):
    state = state or mesh.state_dir()
    p = os.path.join(state, "sources.json")
    return p if os.path.exists(p) else C.state_paths(state)["config"]


def load(state=None):
    """(cfg, path, errors). cfg is None when there is no config file (not an error: LEARN then reads no sources)."""
    path = config_path(state)
    if not os.path.exists(path):
        return None, path, []
    try:
        cfg = C.load_config(path)
    except (OSError, ValueError) as e:
        return None, path, ["cannot read %s: %s" % (os.path.basename(path), getattr(e, "strerror", None) or e)]
    errs = C.validate_config(cfg)
    return (None if errs else cfg), path, errs


def _dropped(cfg, ev, me):
    if FW.has_secret(ev):
        return "secret"
    for f in cfg.get("filters") or []:
        if isinstance(f, dict) and isinstance(f.get("drop"), dict) and FW.match_drop(f["drop"], ev, me):
            return FW.rule_label(f)
    return None


def poll_all(cursors, state=None, env=None, overrides=None):
    """Poll each pull source once. cursors: {name: cursor}, updated in place. Returns (pairs, report)."""
    cfg, path, errs = load(state)
    rep = {"path": path, "present": os.path.exists(path), "errors": errs, "polled": {}, "failed": {},
           "dropped": {}}
    if cfg is None:
        return [], rep
    me = FW.Me(cfg)
    names = {s.get("name") for s in cfg.get("sources") or []}
    out = []
    for src in cfg.get("sources") or []:
        if src.get("kind") in PUSH_KINDS:
            continue
        name = src["name"]
        settings = C.source_settings(cfg, src)
        scfg = C.source_cfg_with_me(cfg, src)
        scfg.update({k: settings[k] for k in C.DEFAULTS})
        try:
            creds = CR.resolve_auth(src.get("auth"), env)
            cls = (overrides or {}).get(src["kind"]) or C.load_source(src["kind"])
            evs, cur, _next = cls(scfg, creds, state or mesh.state_dir()).poll(cursors.get(name))
        except (C.SourceError, CR.Refused, ImportError, OSError) as e:
            rep["failed"][name] = str(e)[:200]  # SourceError text never carries a credential
            continue
        kept = []
        for ev in evs or []:
            if not isinstance(ev, C.Event):
                try:
                    ev = C.Event.from_dict(ev.to_dict() if hasattr(ev, "to_dict") else dict(ev))
                except (TypeError, ValueError):
                    rep["dropped"]["invalid"] = rep["dropped"].get("invalid", 0) + 1
                    continue
            ev = FW.normalize(ev, src, settings, me, names)
            why = _dropped(cfg, ev, me)
            if why:
                rep["dropped"][why] = rep["dropped"].get(why, 0) + 1
                continue
            kept.append(FW.cap(ev, settings))
        cursors[name] = cur
        rep["polled"][name] = len(kept)
        out.append((src, kept))
    return out, rep


def typed_links(ev):
    """fields.links entries with a known rel (C.LINK_RELS), as {rel, type, ref}; anything else is left out."""
    out = []
    for ln in (ev.fields or {}).get("links") or []:
        if isinstance(ln, dict) and ln.get("rel") in C.LINK_RELS and ln.get("type") and ln.get("ref"):
            out.append({"rel": ln["rel"], "type": str(ln["type"]), "ref": str(ln["ref"])})
    return out


def event_row(src, ev):
    """One Event -> add_item() keyword arguments, or None for an event with no quote."""
    if not str(ev.quote or "").strip():
        return None
    t = ev.type or ""
    if t.startswith("decision."):
        kind = "decision"
    elif "closed" in t:
        kind = "closed"
    elif t.startswith("page"):
        kind = "doc"
    else:
        kind = "change"
    status = "heard" if t in ("decision.proposed", "decision.contested") else "confirmed"
    source = {"type": src["kind"], "ref": ev.ref, "source": src["name"], "url": ev.url or "", "eid": ev.eid}
    links = typed_links(ev) + [{"type": src["kind"], "ref": ev.ref, "url": ev.url or ""}]
    return {"kind": kind, "origin": "outside", "quote": ev.quote, "source": source, "title": ev.title or "",
            "links": links, "tags": list(ev.tags or []), "status": status,
            "topic": (ev.title if src["kind"] == "whosaid" and ev.title else ev.ref)}
