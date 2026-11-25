"""WATCH source: Linear (issues), through its GraphQL API. Used by WATCH and by `fleet learn sync|rebuild`.

Config: {"name": "tracker", "kind": "linear", "teams": ["ENG"], "auth": {"type": "pat", "token": "env:LINEAR_API_KEY"},
         "base": "https://api.linear.app/graphql"}   (base optional; teams are Linear team keys)

Poll: one GraphQL query per page, `issues(filter: {team: {key: {in: teams}}, updatedAt: {gt: <cursor>}}, orderBy:
updatedAt)`, oldest change first, at most maxEventsPerPoll issues and MAX_PAGES pages. Cursor: {"updated": <ISO UTC
of the last issue processed>}. A first poll with no cursor reads back FIRST_DAYS days. Change types: issue.closed
(state type completed or canceled), issue.started, issue.created (createdAt == updatedAt), else issue.transition.
fields.links (Q697) carries typed links: part-of the parent issue (else the team), ships-in the project and cycle,
blocked-by each issue with a "blocks" relation to this one, owner the assignee.

Read-only: the query is a GraphQL read; nothing is mutated. A Linear personal API key goes in the Authorization header
as is (Linear wants no "Bearer" prefix for a personal key; auth type bearer adds one, for an OAuth token). The
credential is never stored on an Event or in state. A 429 or a RATELIMITED error raises SourceError with the reset
time as retry_after.

Python 3.8+ stdlib only.
"""
import datetime as _dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from fleet_watch_contract import DEFAULTS, HTTP_TIMEOUT_SEC, Event, Source, SourceError, cap_quote, make_eid  # noqa

UTC = _dt.timezone.utc
BASE = "https://api.linear.app/graphql"
PAGE_SIZE = 50
MAX_PAGES = 10
FIRST_DAYS = 14

QUERY = """query Issues($teams: [String!], $since: DateTimeOrDuration, $first: Int, $after: String) {
  issues(filter: {team: {key: {in: $teams}}, updatedAt: {gt: $since}}, orderBy: updatedAt, first: $first,
         after: $after) {
    nodes { id identifier title url createdAt updatedAt completedAt canceledAt
            state { name type } assignee { name } creator { name } team { key } parent { identifier }
            project { name } cycle { number } inverseRelations { nodes { type issue { identifier } } } }
    pageInfo { hasNextPage endCursor }
  }
}"""


def _iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    """Linear '2026-11-20T10:02:11.123Z' -> aware UTC datetime (fractional seconds dropped); None when unreadable."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", str(s or ""))
    return _dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC) if m else None


def links_of(n):
    """Typed links (Q697 rels) from one issue node."""
    out = []
    parent = (n.get("parent") or {}).get("identifier")
    team = (n.get("team") or {}).get("key")
    if parent:
        out.append({"rel": "part-of", "type": "linear", "ref": parent})
    elif team:
        out.append({"rel": "part-of", "type": "team", "ref": team})
    if (n.get("project") or {}).get("name"):
        out.append({"rel": "ships-in", "type": "project", "ref": n["project"]["name"]})
    if (n.get("cycle") or {}).get("number") is not None:
        out.append({"rel": "ships-in", "type": "cycle", "ref": "%s-cycle-%s" % (team or "team", n["cycle"]["number"])})
    for r in ((n.get("inverseRelations") or {}).get("nodes")) or []:
        other = ((r or {}).get("issue") or {}).get("identifier")
        if (r or {}).get("type") == "blocks" and other:
            out.append({"rel": "blocked-by", "type": "linear", "ref": other})
    if (n.get("assignee") or {}).get("name"):
        out.append({"rel": "owner", "type": "person", "ref": n["assignee"]["name"]})
    return out


def change_type(n):
    st = ((n.get("state") or {}).get("type") or "").lower()
    if st in ("completed", "canceled"):
        return "issue.closed"
    if n.get("createdAt") and n.get("createdAt") == n.get("updatedAt"):
        return "issue.created"
    if st == "started":
        return "issue.started"
    return "issue.transition"


class LinearSource(Source):
    kind = "linear"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None}

    def _fail(self, msg, retry_after=None, status=None):
        self._health["lastError"] = msg
        self._health["failStreak"] += 1
        raise SourceError(msg, retry_after=retry_after, status=status)

    def _auth(self):
        tok = self.creds.get("token") or ""
        if not tok:
            return {}
        if (self.cfg.get("auth") or {}).get("type") == "bearer":
            return {"Authorization": "Bearer " + tok}
        return {"Authorization": tok}

    def _post(self, variables):
        body = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "fleet-watch"}
        h.update(self._auth())
        req = urllib.request.Request(self.cfg.get("base") or BASE, data=body, headers=h, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC)  # looked up at call time (rate bucket)
            hdrs = r.headers
            data = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            self._fail("http %d" % e.code, retry_after=int(ra) if ra and ra.isdigit() else None, status=e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._fail("linear unreachable: %s" % (getattr(e, "reason", None) or e))
        rem = hdrs.get("X-RateLimit-Requests-Remaining") if hdrs else None
        if rem and str(rem).isdigit():
            self._health["rateRemaining"] = int(rem)
        errs = data.get("errors") or []
        if errs:
            code = ((errs[0].get("extensions") or {}).get("code") or "") if isinstance(errs[0], dict) else ""
            if code == "RATELIMITED":
                self._fail("rate limited", retry_after=60, status=429)
            self._fail("graphql error: %s" % (code or "query refused"))
        return ((data.get("data") or {}).get("issues")) or {}

    def poll(self, cursor):
        teams = self.cfg.get("teams")
        if not isinstance(teams, list) or not teams or not all(isinstance(t, str) and t for t in teams):
            self._fail("teams must be a non-empty list of Linear team keys")
        cap = self.cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"]
        qchars = self.cfg.get("quoteChars") or DEFAULTS["quoteChars"]
        since = (cursor or {}).get("updated") if isinstance(cursor, dict) else None
        if not since:
            since = _iso(_dt.datetime.now(UTC) - _dt.timedelta(days=FIRST_DAYS))
        events, last, after = [], since, None
        for _ in range(MAX_PAGES):
            page = self._post({"teams": teams, "since": since, "first": min(PAGE_SIZE, cap), "after": after})
            for n in page.get("nodes") or []:
                if len(events) >= cap or not isinstance(n, dict) or not n.get("identifier"):
                    continue
                events.append(self.to_event(n, qchars))
                if (n.get("updatedAt") or "") > last:
                    last = n["updatedAt"]
            info = page.get("pageInfo") or {}
            if len(events) >= cap or not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        self._health.update(lastOk=_iso(_dt.datetime.now(UTC)), lastError=None, failStreak=0)
        return events, {"updated": last}, None

    def to_event(self, n, qchars=DEFAULTS["quoteChars"]):
        t = change_type(n)
        state = (n.get("state") or {}).get("name") or ""
        at = n.get("completedAt") or n.get("canceledAt") or n.get("updatedAt") or _iso(_dt.datetime.now(UTC))
        at = _iso(_parse(at) or _dt.datetime.now(UTC))
        title = n.get("title") or ""
        quote = "%s %s: %s" % (n["identifier"], state, title) if state else "%s: %s" % (n["identifier"], title)
        actor = (n.get("assignee") or {}).get("name") or (n.get("creator") or {}).get("name") or ""
        return Event(eid=make_eid(self.kind, self.cfg.get("name") or "linear", n["identifier"], at, t),
                     source=self.cfg.get("name") or "linear", sourceKind=self.kind, at=at, type=t,
                     ref=n["identifier"], url=n.get("url") or "", actor=actor, title=cap_quote(title, 300),
                     quote=cap_quote(quote, qchars),
                     fields={"state": state, "stateType": (n.get("state") or {}).get("type") or "",
                             "team": (n.get("team") or {}).get("key") or "", "links": links_of(n)})

    def health(self):
        return dict(self._health)
