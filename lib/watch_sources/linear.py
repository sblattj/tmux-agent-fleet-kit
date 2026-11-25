"""WATCH / LEARN source: Linear issues over the GraphQL API (read-only).

Config: {"name": "tracker", "kind": "linear", "teams": ["ENG"], "auth": {"type": "pat", "token": "env:LINEAR_KEY"},
         "api": "https://api.linear.app/graphql"}
- auth.type "pat" (a personal API key) sends `Authorization: <key>`; "bearer" (an OAuth token) sends
  `Authorization: Bearer <token>`, the two forms Linear's API documents. The key arrives resolved in `creds`
  (env:, keychain: or cmd: references in the config, never a value) and is never put on an Event or in state.
- Poll: `issues(filter: {updatedAt: {gt: cursor}, team: {key: {in: teams}}}, orderBy: updatedAt)`, up to PAGES_MAX
  pages of PAGE_SIZE. Every request is a POST of a query (GraphQL has no GET form here); no mutation is ever sent.
  Cursor: the updatedAt of the last issue read. No cursor (a first poll or a rebuild) reads from the start.
- Change types: issue.created, issue.transition (a state change in the issue history; issue.closed when the new
  state's type is completed or canceled), issue.comment. Ref is the issue identifier (ENG-12).
- fields.edges gives LEARN's entity links: part-of (the project, else the parent issue, else the team),
  blocked-by (a "blocks" relation on another issue), owner (the assignee's display name), ships-in (the cycle).
"""
import datetime as _dt
import http.client
import json
import os
import sys
import urllib.error
import urllib.request

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from fleet_watch_contract import (  # noqa: E402
    DEFAULTS,
    HTTP_TIMEOUT_SEC,
    Event,
    Source,
    SourceError,
    cap_quote,
    make_eid,
)

UTC = _dt.timezone.utc
API = "https://api.linear.app/graphql"
USER_AGENT = "fleet-watch"
PAGE_SIZE = 50
PAGES_MAX = 5
CLOSED_STATES = ("completed", "canceled")

QUERY = """query Issues($first: Int!, $after: String, $filter: IssueFilter) {
  issues(first: $first, after: $after, filter: $filter, orderBy: updatedAt) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id identifier title url createdAt updatedAt
      state { name type }
      team { key }
      project { name }
      parent { identifier }
      cycle { name number }
      assignee { displayName }
      history(first: 20) { nodes { createdAt fromState { name } toState { name type } } }
      comments(first: 20) { nodes { id body createdAt } }
      inverseRelations(first: 20) { nodes { type issue { identifier } } }
    }
  }
}"""


def urllib_http(method, url, headers, body, timeout):
    """(status, headers {lowercase: value}, body bytes). Never raises for an HTTP status; network errors raise
    OSError."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read() if e.fp else b""
        finally:
            e.close()
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data


def _after(ts, cursor):
    return bool(ts) and (not cursor or str(ts) > str(cursor))


def _iso(s):
    """Linear's ISO with milliseconds -> second-precision Z (the Event.at and eid form)."""
    try:
        d = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LinearSource(Source):
    kind = "linear"
    http = staticmethod(urllib_http)

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self.name = cfg.get("name") or "linear"
        self.api = cfg.get("api") or API
        self.teams = [str(t) for t in cfg.get("teams") or []]
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None}

    def _headers(self):
        h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        tok = self.creds.get("token")
        if tok:
            h["Authorization"] = ("Bearer " + tok) if self.creds.get("type") == "bearer" else tok
        return h

    def _fail(self, msg, retry_after=None, status=None):
        self._health["lastError"] = msg
        self._health["failStreak"] += 1
        raise SourceError(msg, retry_after=retry_after, status=status)

    def _post(self, variables):
        body = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
        try:
            status, headers, data = self.http("POST", self.api, self._headers(), body, HTTP_TIMEOUT_SEC)
        except (OSError, http.client.HTTPException) as e:
            self._fail("linear: %s" % type(e).__name__)
        rem = headers.get("x-ratelimit-requests-remaining")
        if rem is not None and str(rem).isdigit():
            self._health["rateRemaining"] = int(rem)
        if status == 429 or (status == 400 and b"RATELIMITED" in (data or b"")):
            ra = headers.get("retry-after")
            self._fail("linear: rate limited", retry_after=int(ra) if str(ra).isdigit() else None, status=status)
        if status != 200:
            self._fail("linear: HTTP %d" % status, status=status)
        try:
            doc = json.loads(data or b"{}")
        except ValueError:
            self._fail("linear: response is not JSON")
        if doc.get("errors"):
            self._fail("linear: query error (%d)" % len(doc["errors"]))
        return (doc.get("data") or {}).get("issues") or {}

    def poll(self, cursor):
        flt = {}
        if cursor:
            flt["updatedAt"] = {"gt": cursor}
        if self.teams:
            flt["team"] = {"key": {"in": self.teams}}
        cap = int(self.cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"])
        events, last, after = [], cursor, None
        for _ in range(PAGES_MAX):
            page = self._post({"first": PAGE_SIZE, "after": after, "filter": flt or None})
            for issue in page.get("nodes") or []:
                for ev in self.issue_events(issue, cursor):
                    if len(events) < cap:
                        events.append(ev)
                if issue.get("updatedAt") and (not last or issue["updatedAt"] > last):
                    last = issue["updatedAt"]
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        self._health.update(lastOk=_dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), lastError=None,
                            failStreak=0)
        return events, last, None

    @staticmethod
    def edges(issue):
        e = {}
        part = ((issue.get("project") or {}).get("name") or (issue.get("parent") or {}).get("identifier")
                or (issue.get("team") or {}).get("key"))
        if part:
            e["part-of"] = [str(part)]
        blockers = [str((r.get("issue") or {}).get("identifier")) for r in
                    (issue.get("inverseRelations") or {}).get("nodes") or []
                    if r.get("type") == "blocks" and (r.get("issue") or {}).get("identifier")]
        if blockers:
            e["blocked-by"] = blockers
        if (issue.get("assignee") or {}).get("displayName"):
            e["owner"] = [str(issue["assignee"]["displayName"])]
        cyc = issue.get("cycle") or {}
        if cyc.get("name") or cyc.get("number") is not None:
            e["ships-in"] = [str(cyc.get("name") or "cycle %s" % cyc.get("number"))]
        return e

    def _event(self, issue, at, typ, quote, extra=None):
        ref = str(issue.get("identifier") or issue.get("id"))
        at = _iso(at)
        if not at or not str(quote or "").strip():
            return None
        fields = {"state": (issue.get("state") or {}).get("name"), "edges": self.edges(issue)}
        fields.update(extra or {})
        return Event(eid=make_eid(self.kind, self.name, ref, at, typ), source=self.name, sourceKind=self.kind,
                     at=at, type=typ, ref=ref, url=str(issue.get("url") or ""), title=str(issue.get("title") or ""),
                     quote=cap_quote(str(quote).strip()), fields=fields, tags=[])

    def issue_events(self, issue, cursor):
        out = []
        if _after(issue.get("createdAt"), cursor):
            out.append(self._event(issue, issue["createdAt"], "issue.created", issue.get("title")))
        for h in (issue.get("history") or {}).get("nodes") or []:
            to = h.get("toState") or {}
            if not to.get("name") or not _after(h.get("createdAt"), cursor):
                continue
            typ = "issue.closed" if to.get("type") in CLOSED_STATES else "issue.transition"
            frm = (h.get("fromState") or {}).get("name") or "?"
            out.append(self._event(issue, h["createdAt"], typ, "%s: %s -> %s" % (issue.get("title"), frm, to["name"]),
                                   {"from": frm, "to": to["name"]}))
        for c in (issue.get("comments") or {}).get("nodes") or []:
            if _after(c.get("createdAt"), cursor):
                out.append(self._event(issue, c["createdAt"], "issue.comment", c.get("body"), {"comment": c.get("id")}))
        return [e for e in out if e is not None]

    def health(self):
        return dict(self._health)
