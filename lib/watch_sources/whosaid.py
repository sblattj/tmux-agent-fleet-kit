"""WATCH / LEARN source: whosaid call decisions (a local meeting workspace, no network).

Config: {"name": "calls", "kind": "whosaid", "workspace": "~/meetings", "meetings": ["2026-01-05-0900"],
         "bin": "whosaid", "timeoutSec": 60, "maxMeetingsPerPoll": 50}
- Runs `<bin> decisions <workspace> <meeting> --json` per meeting folder, without a shell, stdin closed, in its own
  process group, killed after timeoutSec (the same bound as the command source). Local: whosaid's decisions ledger
  needs no model and no network.
- Meetings: the `meetings` list, else every folder in the workspace whose name does not start with `_` or `.`, in
  name order (whosaid names them by date). Cursor: the last meeting folder read; a poll reads the folders after it,
  at most maxMeetingsPerPoll. A rebuild polls with no cursor and reads from the first folder.
- One event per decision: type `decision.<status>` (agreed, proposed, contested), ref `<meeting>#<decision id>`,
  quote = the decision's verbatim turn text, title = the topic label. No speaker name is put on the event; LEARN
  attributes call lines to "the call". fields.edges.decided-by names the meeting, so LEARN's entity graph links the
  topic to the call it was decided in.
- Call decisions are heard, not confirmed: LEARN shows them as "not decided yet" until a desk answer quotes them.
"""
import datetime
import json
import os
import re
import signal
import subprocess
import sys

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_watch_contract as C  # noqa: E402

UTC = datetime.timezone.utc
MAX_MEETINGS = 50
_MEETING_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})")


def _iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def meeting_time(name, offset_sec=0.0):
    """Folder `YYYY-MM-DD-HHMM[-n]` plus the decision's offset -> ISO UTC (the folder time is read as UTC)."""
    m = _MEETING_TIME.match(name or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        t = datetime.datetime(y, mo, d, h, mi, tzinfo=UTC)
    except ValueError:
        return None
    return _iso(t + datetime.timedelta(seconds=float(offset_sec or 0)))


class WhosaidSource(C.Source):
    kind = "whosaid"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self.name = cfg.get("name") or "whosaid"
        self.ws = os.path.expanduser(str(cfg.get("workspace") or ""))
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None, "skipped": 0}

    def _fail(self, msg):
        self._health["lastError"] = msg
        self._health["failStreak"] += 1
        raise C.SourceError(msg)

    def meetings(self, cursor):
        names = self.cfg.get("meetings")
        if not isinstance(names, list):
            try:
                names = [n for n in os.listdir(self.ws)
                         if not n.startswith(("_", ".")) and os.path.isdir(os.path.join(self.ws, n))]
            except OSError as e:
                self._fail("whosaid: workspace not readable: %s" % (e.strerror or type(e).__name__))
        names = sorted(str(n) for n in names)
        if cursor:
            names = [n for n in names if n > str(cursor)]
        return names[: int(self.cfg.get("maxMeetingsPerPoll") or MAX_MEETINGS)]

    def _run(self, meeting):
        argv = [str(self.cfg.get("bin") or "whosaid"), "decisions", self.ws, meeting, "--json"]
        timeout = self.cfg.get("timeoutSec") or C.COMMAND_TIMEOUT_SEC
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        except OSError as e:
            self._fail("whosaid: could not start %s: %s" % (argv[0], e.strerror or e))
        try:
            out, _err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            self._fail("whosaid: timeout after %ss on %s" % (timeout, meeting))
        if p.returncode != 0:
            self._fail("whosaid: decisions exit %d on %s" % (p.returncode, meeting))
        try:
            return json.loads(out or "{}")
        except ValueError:
            self._fail("whosaid: decisions output on %s is not JSON" % meeting)

    def poll(self, cursor):
        if not self.ws or not os.path.isdir(self.ws):
            self._fail("whosaid: workspace %r is not a folder" % (self.cfg.get("workspace"),))
        events, last = [], cursor
        cap = int(self.cfg.get("maxEventsPerPoll") or C.DEFAULTS["maxEventsPerPoll"])
        for m in self.meetings(cursor):
            doc = self._run(m)
            for d in (doc.get("decisions") if isinstance(doc, dict) else None) or []:
                ev = self.to_event(m, d)
                if ev is None:
                    self._health["skipped"] += 1
                elif len(events) < cap:
                    events.append(ev)
            last = m
        self._health.update(lastOk=_iso(datetime.datetime.now(UTC)), lastError=None, failStreak=0)
        return events, last, None

    def to_event(self, meeting, d):
        if not isinstance(d, dict) or not str(d.get("text") or "").strip() or not d.get("id"):
            return None
        status = str(d.get("status") or "proposed")
        at = meeting_time(meeting, d.get("t")) or _iso(datetime.datetime.now(UTC))
        ref = "%s#%s" % (meeting, d["id"])
        typ = "decision.%s" % status
        topic = str(d.get("topicLabel") or d.get("topic") or "")
        return C.Event(eid=C.make_eid(self.kind, self.name, ref, at, typ), source=self.name, sourceKind=self.kind,
                       at=at, type=typ, ref=ref, title=topic, quote=C.cap_quote(str(d["text"]).strip()),
                       url="", actor="", fields={"status": status, "meeting": meeting, "topic": topic,
                                                 "edges": {"decided-by": ["call:%s" % meeting]}},
                       tags=["call", status])

    def health(self):
        return dict(self._health)
