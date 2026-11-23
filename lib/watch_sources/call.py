"""WATCH `call` source (docs/design/four-desks.md 3.2): the live call transcript, as today's `fleet listen` file source.

Config: {"name": "call", "kind": "call", "file": "/path/transcript.txt"}
    or  {"name": "call", "kind": "call", "listener": "$FLEET_STATE/listener.json"}  (uses that config's source.path
        when its source.kind is file or replay)
Cursor: the byte offset already read. Each complete new line that parses as an utterance
(fleet_listen.parse_utterance, "[mm:ss] Speaker: text") becomes one Event: type "utterance", origin "call",
ref = the transcript's base name, actor = the speaker, quote = the text. A shrunk file (rewritten) restarts at 0.
"""
import datetime
import importlib.util
import json
import os
import sys

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_watch_contract as C  # noqa: E402

_listen = None


def _listen_mod():
    global _listen
    if _listen is None:
        spec = importlib.util.spec_from_file_location("fleet_listen", os.path.join(_LIB, "fleet_listen.py"))
        _listen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_listen)
    return _listen


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expand(p):
    st = os.environ.get("FLEET_STATE") or ""
    return os.path.expanduser(p.replace("$FLEET_STATE", st).replace("${FLEET_STATE}", st))


class CallSource(C.Source):
    kind = "call"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None}

    def path(self):
        if self.cfg.get("file"):
            return _expand(self.cfg["file"])
        lp = self.cfg.get("listener")
        if not lp:
            raise C.SourceError("call source needs file or listener")
        try:
            with open(_expand(lp)) as f:
                lc = json.loads(C.strip_jsonc(f.read()))
        except (OSError, ValueError) as e:
            raise C.SourceError("cannot read listener config: %s" % (getattr(e, "strerror", None) or e))
        src = lc.get("source") or {}
        if src.get("kind") not in ("file", "replay") or not src.get("path"):
            raise C.SourceError("listener source must be kind file or replay with a path")
        return _expand(src["path"])

    def poll(self, cursor):
        try:
            path = self.path()
            with open(path, "rb") as f:
                data = f.read()
        except C.SourceError as e:
            self._health.update(lastError=str(e), failStreak=self._health["failStreak"] + 1)
            raise
        except OSError as e:
            self._health.update(lastError=e.strerror or str(e), failStreak=self._health["failStreak"] + 1)
            raise C.SourceError("cannot read transcript: %s" % (e.strerror or e))
        off = int(cursor or 0)
        if off > len(data):
            off = 0
        end = data.rfind(b"\n", off) + 1  # only complete lines
        if end <= off:
            self._health.update(lastOk=_iso_now(), lastError=None, failStreak=0)
            return [], off, None
        cap = self.cfg.get("maxEventsPerPoll") or C.DEFAULTS["maxEventsPerPoll"]
        name = self.cfg.get("name") or "call"
        ref = os.path.basename(path)
        events, pos = [], off
        for raw in data[off:end].split(b"\n")[:-1]:
            line_at = pos
            pos += len(raw) + 1
            u = _listen_mod().parse_utterance(raw.decode("utf-8", "replace"))
            if not u:
                continue
            if len(events) >= cap:
                pos = line_at  # resume here next poll
                break
            at = _iso_now()
            events.append(C.Event(
                eid=C.make_eid(self.kind, name, ref, "%d" % line_at, "utterance"), source=name, sourceKind=self.kind,
                at=at, type="utterance", ref=ref, actor=u["speaker"], quote=u["text"],
                fields={"clock": u["clock"], "offset": line_at}, origin="call"))
        self._health.update(lastOk=_iso_now(), lastError=None, failStreak=0)
        return events, pos, None

    def health(self):
        return dict(self._health)
