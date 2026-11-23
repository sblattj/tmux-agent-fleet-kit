"""WATCH `command` source (docs/design/four-desks.md 3.2): any program printing Event-shaped JSON lines.

Config: {"name": "replay", "kind": "command", "argv": ["prog", "arg"], "timeoutSec": 60}
- argv runs without a shell, stdin closed, in its own process group; killed with the group after timeoutSec
  (default COMMAND_TIMEOUT_SEC), the same bound as run_bounded in lib/fleet_listen.py.
- The program keeps its own cursor: the engine passes the last one in $FLEET_WATCH_CURSOR, and the program may print
  one line {"cursor": "<value>"} to set the next one. Every other line is an event object; missing identity fields
  are filled (source = this source's name, sourceKind = "command", eid from make_eid when absent).
- A resolved credential (auth block) reaches the program only as $FLEET_WATCH_TOKEN / $FLEET_WATCH_USER, never argv.
- At most maxEventsPerPoll events per poll; lines that are not JSON objects are counted in health()["skipped"].
"""
import datetime
import json
import os
import signal
import subprocess
import sys

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_watch_contract as C  # noqa: E402


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CommandSource(C.Source):
    kind = "command"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None, "skipped": 0}

    def _env(self, cursor):
        env = dict(os.environ)
        for k in ("FLEET_WATCH_TOKEN", "FLEET_WATCH_USER"):
            env.pop(k, None)
        env["FLEET_WATCH_CURSOR"] = "" if cursor is None else str(cursor)
        if self.creds.get("token"):
            env["FLEET_WATCH_TOKEN"] = self.creds["token"]
        if self.creds.get("user"):
            env["FLEET_WATCH_USER"] = self.creds["user"]
        return env

    def _fail(self, msg):
        self._health["lastError"] = msg
        self._health["failStreak"] += 1
        raise C.SourceError(msg)

    def poll(self, cursor):
        argv = self.cfg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            self._fail("argv must be a non-empty list of strings")
        timeout = self.cfg.get("timeoutSec") or C.COMMAND_TIMEOUT_SEC
        cap = self.cfg.get("maxEventsPerPoll") or C.DEFAULTS["maxEventsPerPoll"]
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=self._env(cursor), cwd=self.cfg.get("cwd") or None,
                                 start_new_session=True)
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
            self._fail("timeout after %ss" % timeout)
        if p.returncode != 0:
            self._fail("exit %d" % p.returncode)  # stderr is not echoed: it may carry a credential
        events, new_cursor = [], cursor
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                self._health["skipped"] += 1
                continue
            if not isinstance(d, dict):
                self._health["skipped"] += 1
                continue
            if set(d) == {"cursor"}:
                new_cursor = d["cursor"]
                continue
            if len(events) >= cap:
                continue
            ev = self.to_event(d)
            if ev is None:
                self._health["skipped"] += 1
                continue
            events.append(ev)
        self._health.update(lastOk=_iso_now(), lastError=None, failStreak=0)
        return events, new_cursor, None

    def to_event(self, d):
        d = dict(d)
        d["source"] = self.cfg.get("name") or "command"
        d["sourceKind"] = self.kind
        d.setdefault("at", _iso_now())
        if not d.get("type") or not d.get("ref"):
            return None
        d.setdefault("eid", C.make_eid(self.kind, d["source"], d["ref"], d["at"], d["type"]))
        for k in ("kind", "to", "score", "secretDropped", "count"):  # set by the pipeline, never by the program
            d.pop(k, None)
        d.pop("origin", None)
        try:
            ev = C.Event.from_dict(d)
        except TypeError:
            return None
        if not isinstance(ev.fields, dict):
            ev.fields = {}
        if not isinstance(ev.tags, list):
            ev.tags = []
        return ev

    def health(self):
        return dict(self._health)
