"""WATCH source: whosaid meeting decisions (local transcripts, no network). Used by WATCH and `fleet learn sync|rebuild`.

Config: {"name": "calls", "kind": "whosaid", "workspace": "~/meetings", "bin": "whosaid", "timeoutSec": 60,
         "maxMeetings": 20}
- workspace is a whosaid meeting workspace: one folder per meeting (folders starting with "_" or "." are skipped).
- Poll: the meeting folders changed since the cursor, oldest first, at most maxMeetings; for each one
  `<bin> decisions <workspace> <meeting> --json --engine local` runs without a shell, stdin closed, in its own
  process group, killed after timeoutSec. --engine local keeps it on this machine: no model, no network.
- One Event per decision: type decision.<agreed|proposed|contested>, ref "<meeting>#<decision id>", quote the
  decision's verbatim text, actor its speaker, title the topic label. fields.links (Q697) carries typed links:
  part-of the meeting, decided-by the speaker.
- Cursor: {"mtime": <newest meeting folder mtime processed>, "done": [meeting names at that mtime]}.

Python 3.8+ stdlib only.
"""
import datetime as _dt
import json
import os
import signal
import subprocess
import sys

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_watch_contract as C  # noqa: E402

UTC = _dt.timezone.utc
MAX_MEETINGS = 20
STATUSES = ("agreed", "proposed", "contested")


def _iso(ts):
    return _dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WhosaidSource(C.Source):
    kind = "whosaid"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None, "skipped": 0}

    def _fail(self, msg):
        self._health["lastError"] = msg
        self._health["failStreak"] += 1
        raise C.SourceError(msg)

    def meetings(self, ws, cursor):
        """[(mtime, name)] changed since the cursor, oldest first."""
        cur = cursor if isinstance(cursor, dict) else {}
        since, done = float(cur.get("mtime") or 0), set(cur.get("done") or [])
        out = []
        for name in os.listdir(ws):
            p = os.path.join(ws, name)
            if name.startswith(("_", ".")) or not os.path.isdir(p):
                continue
            m = os.path.getmtime(p)
            if m > since or (m == since and name not in done):
                out.append((m, name))
        return sorted(out)

    def _decisions(self, ws, meeting):
        argv = [self.cfg.get("bin") or "whosaid", "decisions", ws, meeting, "--json", "--engine", "local"]
        timeout = self.cfg.get("timeoutSec") or C.COMMAND_TIMEOUT_SEC
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        except OSError as e:
            self._fail("could not start %s: %s" % (argv[0], e.strerror or e))
        try:
            out, _err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            self._fail("timeout after %ss on %s" % (timeout, meeting))
        if p.returncode != 0:
            self._health["skipped"] += 1  # one unreadable meeting never stops the rest
            return None
        try:
            d = json.loads(out)
        except ValueError:
            self._health["skipped"] += 1
            return None
        return d if isinstance(d, dict) else None

    def poll(self, cursor):
        ws = os.path.expanduser(str(self.cfg.get("workspace") or ""))
        if not ws or not os.path.isdir(ws):
            self._fail("workspace is not a folder")
        cap = self.cfg.get("maxEventsPerPoll") or C.DEFAULTS["maxEventsPerPoll"]
        todo = self.meetings(ws, cursor)[: int(self.cfg.get("maxMeetings") or MAX_MEETINGS)]
        events = []
        cur = dict(cursor) if isinstance(cursor, dict) else {"mtime": 0, "done": []}
        for mtime, name in todo:
            d = self._decisions(ws, name)
            for row in (d or {}).get("decisions") or []:
                ev = self.to_event(row, name, mtime)
                if ev is None:
                    self._health["skipped"] += 1
                elif len(events) < cap:
                    events.append(ev)
            if mtime > float(cur.get("mtime") or 0):
                cur = {"mtime": mtime, "done": [name]}
            else:
                cur["done"] = sorted(set(cur.get("done") or []) | {name})
        self._health.update(lastOk=_iso(_dt.datetime.now(UTC).timestamp()), lastError=None, failStreak=0)
        return events, cur, None

    def to_event(self, row, meeting, mtime):
        if not isinstance(row, dict) or not str(row.get("text") or "").strip() or not row.get("id"):
            return None
        status = row.get("status") if row.get("status") in STATUSES else "proposed"
        name = self.cfg.get("name") or "whosaid"
        ref = "%s#%s" % (meeting, row["id"])
        at = _iso(mtime)
        t = "decision." + status
        speaker = str(row.get("speaker") or "")
        links = [{"rel": "part-of", "type": "meeting", "ref": meeting}]
        if speaker:
            links.append({"rel": "decided-by", "type": "person", "ref": speaker})
        return C.Event(eid=C.make_eid(self.kind, name, ref, at, t), source=name, sourceKind=self.kind, at=at, type=t,
                       ref=ref, url="", actor=speaker, title=C.cap_quote(str(row.get("topicLabel") or ""), 300),
                       quote=C.cap_quote(str(row["text"]), self.cfg.get("quoteChars") or C.DEFAULTS["quoteChars"]),
                       fields={"status": status, "meeting": meeting, "topic": row.get("topic") or "",
                               "t": row.get("t_str") or "", "links": links})

    def health(self):
        return dict(self._health)
