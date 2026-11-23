"""WATCH source: Jira Cloud and Jira Data Center / Server (four-desks.md 3.2, phase 6).

Poll: JQL `(<query>) AND updated >= "-<N>m" ORDER BY updated ASC` on the search
endpoint with the changelog and comments expanded, then the full changelog or comment list for an issue whose
embedded copy is truncated. N is the minutes since the cursor plus OVERLAP_MIN. JQL's relative form is read against
the server's clock, not a time zone, so the user's or server's zone cannot shift the window and skip changes; the
overlap absorbs clock skew, and the client-side cursor check (plus the engine's eid dedupe) drops the refetched part. Cursor: {"updated": <ISO UTC of the last issue processed>, "ids": [issue ids at that
instant]}. Change types: issue.created, issue.transition, issue.assignee, issue.field:<field>, issue.comment
(tag "mention" when the comment mentions me).

Read-only: every call is a GET. Credentials arrive resolved in `creds` (the engine resolves the watch.json references
through the creds resolver); they go in the Authorization header only and are never stored on an Event or in state.

Webhooks: the receiver (watch_sources/webhook.py) alone verifies the signature. Jira sends
`X-Hub-Signature: sha256=<hex HMAC>`, which its generic "hmac" verifier covers with `"header": "X-Hub-Signature"`.
accept_webhook here only normalizes.

Python 3.8+ stdlib only.
"""
import base64
import datetime as _dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
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
PAGE_SIZE = 50  # issues per search page
SUB_PAGE = 100  # changelog / comment entries per page
MAX_SUB_PAGES = 10  # bound on per-issue changelog or comment pages
SEARCH_FIELDS = "summary,status,assignee,reporter,creator,created,updated,comment"
OVERLAP_MIN = 15  # the JQL window reaches this far before the cursor (clock skew between this host and Jira)


# --- time ------------------------------------------------------------------------------------------------------------
def parse_time(s):
    """Jira '2026-11-20T10:02:11.000+0000' (or ISO with Z / +00:00) -> aware datetime in UTC."""
    if not s:
        return None
    t = str(s).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    m = re.match(r"^(.*[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)([+-])(\d{2}):?(\d{2})$", t)
    if m:
        t = "%s%s%s:%s" % m.groups()
    if "." in t:  # fromisoformat on 3.8 wants 3 or 6 fraction digits
        head, rest = t.split(".", 1)
        frac = re.match(r"^\d+", rest).group(0)
        t = head + "." + (frac + "000000")[:6] + rest[len(frac):]
    d = _dt.datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def iso(d):
    """UTC ISO 8601 with Z; milliseconds only when non-zero (Event.at, eid)."""
    d = d.astimezone(UTC)
    if d.microsecond:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_sec(d):
    """eid form: second precision, as the spec's example eid, so a webhook and a poll of one change agree."""
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(d):
    """Cursor form: always milliseconds, so string order is time order."""
    d = d.astimezone(UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def jql_since(since, now, overlap_min=OVERLAP_MIN):
    """The JQL relative literal for `updated >=`: "-<N>m", N = whole minutes from `since` to `now`, rounded up, plus
    the overlap. Relative dates do not depend on any time zone."""
    secs = max(0.0, (now - since).total_seconds())
    return "-%dm" % (int(-(-secs // 60)) + overlap_min)


# --- text ------------------------------------------------------------------------------------------------------------
def adf_text(node):
    """Flatten Atlassian Document Format to plain text (text nodes, mentions as their @text, blocks on new lines)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "mention":
        a = node.get("attrs") or {}
        return a.get("text") or ("@" + str(a.get("id", "")))
    if t == "hardBreak":
        return "\n"
    inner = adf_text(node.get("content"))
    if t in ("paragraph", "heading", "listItem", "blockquote", "codeBlock"):
        return inner + "\n"
    return inner


def adf_mentions(node, out=None):
    out = set() if out is None else out
    if isinstance(node, list):
        for n in node:
            adf_mentions(n, out)
    elif isinstance(node, dict):
        if node.get("type") == "mention" and (node.get("attrs") or {}).get("id"):
            out.add(str(node["attrs"]["id"]))
        adf_mentions(node.get("content"), out)
    return out


_WIKI_MENTION = re.compile(r"\[~(?:accountid:)?([^\]\s]+)\]")


def body_text_and_mentions(body):
    """Comment body: ADF dict (API v3) or wiki-markup string (API v2). -> (plain text, {mentioned ids or names})."""
    if isinstance(body, dict) or isinstance(body, list):
        return adf_text(body).strip(), adf_mentions(body)
    s = body or ""
    return s.strip(), set(_WIKI_MENTION.findall(s))


def person(p):
    """(display name, set of ids) for a Jira user object. Cloud: accountId; DC: name and key."""
    if not isinstance(p, dict):
        return "", set()
    ids = {str(p[k]) for k in ("accountId", "name", "key") if p.get(k)}
    return p.get("displayName") or p.get("name") or "", ids


# --- HTTP ------------------------------------------------------------------------------------------------------------
def urllib_get(url, headers, timeout):
    """GET -> (status, headers dict lower-cased, body bytes). HTTP errors return their status; network errors raise."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


class JiraSource(Source):
    kind = "jira"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self.base = str(cfg.get("base", "")).rstrip("/")
        host = urllib.parse.urlsplit(self.base).hostname or self.base
        dep = cfg.get("deployment") or ("cloud" if host.endswith(".atlassian.net") else "server")
        if dep not in ("cloud", "server"):
            raise SourceError("jira deployment must be cloud or server, got %r" % dep)
        self.cloud = dep == "cloud"
        self.api = "/rest/api/%s" % ("3" if self.cloud else "2")
        self.instance = str(cfg.get("instance") or host)
        self.query = str(cfg.get("query") or "").strip()
        if re.search(r"\border\s+by\b", self.query, re.I):
            raise SourceError("jira query must not carry ORDER BY; the adapter orders by updated")
        self.max_events = int(cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"])
        self.quote_chars = int(cfg.get("quoteChars") or DEFAULTS["quoteChars"])
        self.since_sec = int(cfg.get("sinceSec") or 0)
        me = cfg.get("me") if isinstance(cfg.get("me"), dict) else {}  # injected by the engine (source_cfg_with_me)
        self.me_ids = {str(me["id"])} if me.get("id") else set()
        self.me_names = {str(n) for n in (me.get("names") or []) if n}
        self.http = urllib_get  # tests replace this with a recorded-fixture transport
        self.now = lambda: _dt.datetime.now(UTC)  # tests inject the clock
        self._health = {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None, "truncated": 0}
        self.calls = 0

    # -- plumbing ------------------------------------------------------------------------------------------------
    def _auth_header(self):
        c = self.creds or {}
        typ = (self.cfg.get("auth") or {}).get("type", "basic" if c.get("user") else "bearer")
        tok = c.get("token") or c.get("password")
        if typ == "none" or not tok:
            return {}
        if typ == "basic":
            raw = ("%s:%s" % (c.get("user", ""), tok)).encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {"Authorization": "Bearer " + tok}

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "tmux-agent-fleet-kit-watch"}
        headers.update(self._auth_header())
        self.calls += 1
        try:
            status, rh, body = self.http(url, headers, HTTP_TIMEOUT_SEC)
        except Exception as e:  # network, timeout
            raise SourceError("jira GET %s failed: %s" % (path, type(e).__name__))
        rh = {str(k).lower(): v for k, v in (rh or {}).items()}
        if rh.get("x-ratelimit-remaining") not in (None, ""):
            try:
                self._health["rateRemaining"] = int(rh["x-ratelimit-remaining"])
            except ValueError:
                pass
        if status == 429 or status >= 500:
            ra = rh.get("retry-after")
            try:
                ra = int(float(ra)) if ra not in (None, "") else None
            except ValueError:
                ra = None
            raise SourceError("jira GET %s returned %d" % (path, status), retry_after=ra, status=status)
        if status >= 400:
            raise SourceError("jira GET %s returned %d" % (path, status), status=status)
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except ValueError:
            raise SourceError("jira GET %s returned non-JSON" % path, status=status)

    def _myself(self):
        """The authenticated user's ids for `me` (Cloud accountId; DC name and key). Cached in state."""
        if "myself" not in self.state:
            m = self._get(self.api + "/myself")
            _, ids = person(m)
            self.state["myself"] = {"ids": sorted(ids)}
        return self.state["myself"]

    # -- the poll ------------------------------------------------------------------------------------------------
    def poll_jql(self, since):
        clause = 'updated >= "%s" ORDER BY updated ASC' % jql_since(since, self.now())
        return ("(%s) AND %s" % (self.query, clause)) if self.query else clause

    def _search_pages(self, jql):
        """Yield issues in updated order, page by page (Cloud: nextPageToken; DC: startAt)."""
        token, start_at = None, 0
        while True:
            params = {"jql": jql, "fields": SEARCH_FIELDS, "expand": "changelog", "maxResults": PAGE_SIZE}
            if self.cloud:
                if token:
                    params["nextPageToken"] = token
                page = self._get(self.api + "/search/jql", params)
            else:
                params["startAt"] = start_at
                page = self._get(self.api + "/search", params)
            issues = page.get("issues") or []
            for it in issues:
                yield it
            if self.cloud:
                token = page.get("nextPageToken")
                if page.get("isLast", not token) or not token or not issues:
                    return
            else:
                start_at += len(issues)
                if not issues or start_at >= int(page.get("total", 0)):
                    return

    def _histories(self, issue, since=None):
        """The issue's changelog entries that can be newer than `since`. The embedded copy holds the most recent
        entries; refetch the full changelog only when it is truncated AND its oldest entry is still after `since`,
        then union by id (so the newest entries survive even past the refetch page bound)."""
        cl = issue.get("changelog") or {}
        hist = list(cl.get("histories") or [])
        total = int(cl.get("total", len(hist)) or 0)
        if total <= len(hist):
            return hist
        times = [parse_time(h.get("created")) for h in hist]
        times = [t for t in times if t is not None]
        if since is not None and times and min(times) <= since:
            return hist  # everything after the cursor is already embedded
        merged = {str(h.get("id")): h for h in self._fetch_histories(issue.get("key"))}
        merged.update({str(h.get("id")): h for h in hist})
        return list(merged.values())

    def _fetch_histories(self, key):
        if self.cloud:
            out, start = [], 0
            for _ in range(MAX_SUB_PAGES):
                page = self._get("%s/issue/%s/changelog" % (self.api, key), {"startAt": start, "maxResults": SUB_PAGE})
                vals = page.get("values") or []
                out.extend(vals)
                start += len(vals)
                if page.get("isLast", True) or not vals:
                    break
            return out
        full = self._get("%s/issue/%s" % (self.api, key), {"expand": "changelog", "fields": "updated"})
        return list((full.get("changelog") or {}).get("histories") or [])

    def _comments(self, issue):
        block = (issue.get("fields") or {}).get("comment") or {}
        comments = list(block.get("comments") or [])
        total = int(block.get("total", len(comments)) or 0)
        if total <= len(comments):
            return comments
        start = max(0, total - SUB_PAGE)  # the newest page holds the changes since the cursor
        page = self._get("%s/issue/%s/comment" % (self.api, issue.get("key")), {"startAt": start, "maxResults": SUB_PAGE})
        return list(page.get("comments") or [])

    def poll(self, cursor):
        try:
            out = self._poll(cursor)
        except SourceError as e:
            self._health["failStreak"] += 1
            self._health["lastError"] = str(e)
            raise
        self._health["failStreak"] = 0
        self._health["lastError"] = None
        self._health["lastOk"] = iso(self.now())
        return out

    def _poll(self, cursor):
        cursor = cursor if isinstance(cursor, dict) else {}
        if cursor.get("updated"):
            since = parse_time(cursor["updated"])
            at_since = set(str(i) for i in cursor.get("ids") or [])
        else:  # first poll: start at now (minus sinceSec), no history flood
            since = self.now() - _dt.timedelta(seconds=self.since_sec)
            at_since = set()
        me = self._myself()
        self.me_all = self.me_ids | set(me["ids"])
        events = []
        new_updated, new_ids = since, set(at_since)
        for issue in self._search_pages(self.poll_jql(since)):
            iid = str(issue.get("id"))
            upd = parse_time((issue.get("fields") or {}).get("updated"))
            if upd is None or upd < since or (upd == since and iid in at_since):
                continue  # the minute floor (or a tz margin) brought back something already processed
            mapped = self.map_issue(issue, since, inclusive=(iid not in at_since))
            if events and len(events) + len(mapped) > self.max_events:
                break  # the cursor stays before this issue; the next poll picks it up
            if not events and len(mapped) > self.max_events:
                # one issue alone exceeds the cap: keep the oldest, count the rest (health "truncated") so the loss
                # is visible; holding the cursor here would stall the source forever
                self._health["truncated"] = self._health.get("truncated", 0) + len(mapped) - self.max_events
                mapped = mapped[: self.max_events]
            events.extend(mapped)
            if upd > new_updated:
                new_updated, new_ids = upd, {iid}
            else:
                new_ids.add(iid)
            if len(events) >= self.max_events:
                break
        return events, {"updated": iso_ms(new_updated), "ids": sorted(new_ids)}, None

    # -- mapping -------------------------------------------------------------------------------------------------
    def _event(self, issue, at, type_, change_kind, actor, quote, fields=None, tags=None):
        name, ids = actor
        key = issue.get("key", "")
        f = issue.get("fields") or {}
        base_fields = {"issueId": str(issue.get("id", ""))}
        st = (f.get("status") or {}).get("name")
        if st:
            base_fields["status"] = st
        base_fields.update(fields or {})
        return Event(
            eid=make_eid("jira", self.instance, key, iso_sec(at), change_kind),
            source=str(self.cfg.get("name", "")),
            sourceKind="jira",
            at=iso(at),
            type=type_,
            ref=key,
            url="%s/browse/%s" % (self.base, key),
            actor=name,
            me=bool(ids & self.me_all) or (bool(name) and name in self.me_names),
            title=f.get("summary") or "",
            quote=cap_quote(quote, self.quote_chars),
            fields=base_fields,
            tags=sorted(set(tags or [])),
        )

    def map_issue(self, issue, since, inclusive=True):
        """Every change on `issue` after `since` (at `since` too when inclusive) -> Events, oldest first."""
        self.me_all = getattr(self, "me_all", self.me_ids)

        def new(t):
            return t is not None and (t > since or (inclusive and t == since))

        f = issue.get("fields") or {}
        out = []
        assignee_name, assignee_ids = person(f.get("assignee"))
        mine = bool(assignee_ids & self.me_all)
        created = parse_time(f.get("created"))
        if new(created):
            who = person(f.get("reporter") or f.get("creator"))
            out.append(self._event(issue, created, "issue.created", "created", who, f.get("summary") or "",
                                   {"assignee": assignee_name}, ["assigned-to-me"] if mine else []))
        for h in self._histories(issue, since):
            at = parse_time(h.get("created"))
            if not new(at):
                continue
            who = person(h.get("author"))
            for item in h.get("items") or []:
                field = str(item.get("fieldId") or item.get("field") or "").strip()
                frm, to = item.get("fromString") or "", item.get("toString") or ""
                label = item.get("field") or field
                if field == "status":
                    out.append(self._event(issue, at, "issue.transition", "status", who,
                                           "Status: %s -> %s" % (frm, to), {"from": frm, "to": to}))
                elif field == "assignee":
                    to_me = str(item.get("to") or "") in self.me_all
                    out.append(self._event(issue, at, "issue.assignee", "assignee", who,
                                           "Assignee: %s -> %s" % (frm or "Unassigned", to or "Unassigned"),
                                           {"from": frm, "to": to}, ["assigned-to-me"] if to_me else []))
                elif field:
                    out.append(self._event(issue, at, "issue.field:%s" % field, "field:%s" % field, who,
                                           "%s: %s -> %s" % (label, frm, to), {"field": field, "from": frm, "to": to}))
        for c in self._comments(issue):
            at = parse_time(c.get("created"))
            if not new(at):
                continue
            text, mentions = body_text_and_mentions(c.get("body"))
            tags = ["mention"] if mentions & self.me_all else []
            out.append(self._event(issue, at, "issue.comment", "comment:%s" % c.get("id", ""), person(c.get("author")),
                                   text, {"commentId": str(c.get("id", ""))}, tags))
        out.sort(key=lambda e: e.at)
        return out

    def health(self):
        return dict(self._health)

    # -- webhooks (phase 6) ------------------------------------------------------------------------------------------
    def accept_webhook(self, headers, body):
        """Normalize one Jira webhook delivery (jira:issue_created / jira:issue_updated, with its changelog or
        comment) into the Events, and so the eids, the poll gives the same change. Normalization only: the receiver
        has already verified X-Hub-Signature (ROUTE ruling), so no secret reaches this adapter."""
        raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise SourceError("jira webhook body is not JSON", status=400)
        if not isinstance(payload, dict):
            raise SourceError("jira webhook body is not an object", status=400)
        issue = payload.get("issue") or {}
        if not issue.get("key"):
            return []
        issue = dict(issue)
        f = dict(issue.get("fields") or {})
        ts = payload.get("timestamp")
        at = _dt.datetime.fromtimestamp(ts / 1000.0, UTC) if isinstance(ts, (int, float)) else self.now()
        ev = payload.get("webhookEvent", "")
        # Only the change this delivery is about, with the same eids the poll would give it.
        if payload.get("changelog"):
            cl = dict(payload["changelog"])
            cl.setdefault("created", f.get("updated") or iso(at))
            cl.setdefault("author", payload.get("user"))
            issue["changelog"] = {"histories": [cl], "total": 1}
        else:
            issue["changelog"] = {"histories": [], "total": 0}
        if payload.get("comment"):
            f["comment"] = {"comments": [payload["comment"]], "total": 1}
        else:
            f["comment"] = {"comments": [], "total": 0}
        if ev != "jira:issue_created":
            f.pop("created", None)
        issue["fields"] = f
        self.me_all = self.me_ids | set((self.state.get("myself") or {}).get("ids") or [])
        return self.map_issue(issue, _dt.datetime(1970, 1, 1, tzinfo=UTC), inclusive=False)
