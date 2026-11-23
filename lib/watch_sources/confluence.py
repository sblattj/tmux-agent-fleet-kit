"""WATCH source: Confluence (Cloud and Data Center), polled with CQL. Spec: docs/design/four-desks.md 3.1, 3.2.

Poll query (vendor docs are cited in test/fixtures/watch/confluence/README.md):
  GET {base}/rest/api/content/search?cql=<cql>&limit=<n>&expand=version,history,container,space,body.storage
  cql = type in (page, blogpost, comment) [AND space in ("ACME")] AND lastmodified >= "yyyy/MM/dd HH:mm"
        ORDER BY lastmodified ASC
  Pagination follows `_links.next` (a cursor URL), re-joined to the configured base so the Authorization header
  never goes to a host taken from a response body.

Cursor: {"since": <ISO UTC of the newest version.when emitted>, "ids": [<"id:version"> at that instant],
"pending": {"next", "hi", "hiIds"} only while a run of polls is part way through the result pages}.
CQL dates are minute precision and read in the API user's configured time zone, so the CQL bound is only a
prefilter, biased early by `overlapSec` (default 3600, covers a DST shift) and shifted by `timeZone` ("+HH:MM",
default "+00:00"). What is new is decided client side: version.when after `since`, or at `since` with an id not in
`ids`. Only new rows count toward maxEventsPerPoll, so a busy overlap window cannot stall the cursor. A poll that
stops with pages left saves the `next` link and the next poll resumes there. The cursor never depends on server
order (ORDER BY lastmodified is not documented as orderable); see _advance.

Change mapping (Event.type):
  page / blogpost, version 1  -> page.created      (fields.contentType says page or blogpost)
  page / blogpost, version >1 -> page.edited
  comment                     -> page.commented    (ref is the parent page id when container is a page)
Tags: mention (a comment's storage body names my accountId), my-page (someone changed or commented on a page I
created). Event.me: version.by.accountId == me.id, or its displayName/publicName is in me.names.

Credentials come in `creds` ({"token", "user"}) already resolved by the engine; auth.type bearer or basic.
Python 3.8+ stdlib only.
"""
import base64
import datetime as _dt
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import fleet_watch_contract as W

CONTENT_TYPES = ("page", "blogpost", "comment")
EXPAND = "version,history,container,space,body.storage"
DEFAULT_OVERLAP_SEC = 3600
DEFAULT_FIRST_LOOKBACK_SEC = 0  # first poll starts "now": no backlog flood
SEARCH_PATH = "/rest/api/content/search"
_MENTION = re.compile(r'ri:account-id="([^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_TZ = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def parse_when(s):
    """ISO 8601 (with Z or an offset, optional fraction) -> aware UTC datetime, or None."""
    if not isinstance(s, str) or not s:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2})?$", t)
    if not m:
        return None
    frac = (m.group(2) or "")[:7].ljust(7, "0") if m.group(2) else ""
    off = m.group(3) or "+00:00"
    if ":" not in off:
        off = off[:3] + ":" + off[3:]
    try:
        d = _dt.datetime.fromisoformat(m.group(1) + frac + off)
    except ValueError:
        return None
    return d.astimezone(_dt.timezone.utc)


def iso_z(d):
    """Aware datetime -> 'YYYY-MM-DDTHH:MM:SS[.fff]Z'."""
    d = d.astimezone(_dt.timezone.utc)
    if d.microsecond:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_tz(s):
    m = _TZ.match(s or "+00:00")
    if not m:
        raise ValueError("timeZone must be +HH:MM or -HH:MM")
    mins = int(m.group(2)) * 60 + int(m.group(3))
    return _dt.timezone(_dt.timedelta(minutes=mins if m.group(1) == "+" else -mins))


def cql_date(d, tz):
    """CQL date literal, minute precision, in the API user's time zone (CQL format 'yyyy/MM/dd HH:mm')."""
    return d.astimezone(tz).strftime("%Y/%m/%d %H:%M")


def _cql_str(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def build_cql(since_dt, spaces=None, tz=_dt.timezone.utc, overlap_sec=DEFAULT_OVERLAP_SEC, extra=None):
    bound = since_dt - _dt.timedelta(seconds=overlap_sec)
    parts = ["type in (%s)" % ", ".join(CONTENT_TYPES)]
    if spaces:
        parts.append("space in (%s)" % ", ".join(_cql_str(s) for s in spaces))
    if extra:
        parts.append("(%s)" % extra)
    parts.append("lastmodified >= %s" % _cql_str(cql_date(bound, tz)))
    return " AND ".join(parts) + " ORDER BY lastmodified ASC"


def _text(storage, limit):
    t = html.unescape(_TAG.sub(" ", storage or ""))
    return W.cap_quote(" ".join(t.split()), limit)


def _user(u):
    u = u or {}
    return {"accountId": u.get("accountId") or "", "displayName": u.get("displayName") or u.get("publicName") or "",
            "publicName": u.get("publicName") or ""}


class ConfluenceSource(W.Source):
    kind = "confluence"

    def __init__(self, cfg, creds, state, transport=None, clock=None):
        super().__init__(cfg, creds, state)
        self.base = (cfg.get("base") or "").rstrip("/")
        self.instance = urllib.parse.urlparse(self.base).netloc or self.base
        self.spaces = list(cfg.get("spaces") or [])
        self.tz = parse_tz(cfg.get("timeZone"))
        self.overlap = int(cfg.get("overlapSec", DEFAULT_OVERLAP_SEC))
        self.max_events = int(cfg.get("maxEventsPerPoll", W.DEFAULTS["maxEventsPerPoll"]))
        self.quote_chars = int(cfg.get("quoteChars", W.DEFAULTS["quoteChars"]))
        self.page_limit = min(int(cfg.get("pageLimit", 50)), self.max_events)
        me = cfg.get("me") or {}
        self.me_id = me.get("id")
        self.me_names = set(me.get("names") or [])
        self._transport = transport or self._urlopen
        self._clock = clock or _utcnow
        self.max_pages = int(cfg.get("maxPages", 20))
        st = self.state
        self._health = {"lastOk": st.get("lastOk"), "lastError": st.get("lastError"),
                        "failStreak": int(st.get("failStreak") or 0), "rateRemaining": None}

    # --- HTTP ----------------------------------------------------------------------------------------------------
    def _headers(self):
        h = {"Accept": "application/json", "User-Agent": "fleet-watch"}
        auth = (self.cfg.get("auth") or {}).get("type", "bearer")
        tok = self.creds.get("token")
        if auth == "basic" and tok:
            raw = ("%s:%s" % (self.creds.get("user") or "", tok)).encode("utf-8")
            h["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        elif auth in ("bearer", "pat") and tok:
            h["Authorization"] = "Bearer " + tok
        return h

    @staticmethod
    def _urlopen(url, headers, timeout):
        """Returns (status, headers dict, body bytes). Never raises for an HTTP status."""
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b""

    def _get(self, url):
        try:
            status, hdrs, body = self._transport(url, self._headers(), W.HTTP_TIMEOUT_SEC)
        except Exception as e:  # network error: never echo headers (they hold the token)
            raise W.SourceError("confluence request failed: %s" % type(e).__name__)
        lower = {k.lower(): v for k, v in (hdrs or {}).items()}
        rem = lower.get("x-ratelimit-remaining")
        if rem is not None and str(rem).isdigit():
            self._health["rateRemaining"] = int(rem)
        if status == 429 or status >= 500:
            ra = lower.get("retry-after")
            raise W.SourceError("confluence HTTP %d" % status, retry_after=int(ra) if str(ra or "").isdigit() else None,
                                status=status)
        if status >= 400:
            raise W.SourceError("confluence HTTP %d" % status, status=status)
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except ValueError:
            raise W.SourceError("confluence returned non-JSON")

    def _next_url(self, data):
        """`_links.next` joined to the configured base (path and query only; a host in the body is ignored)."""
        return self._join(((data or {}).get("_links") or {}).get("next"))

    def _join(self, nxt):
        if not nxt:
            return None
        p = urllib.parse.urlparse(nxt)
        path = p.path
        root = urllib.parse.urlparse(self.base).path.rstrip("/")
        if root and not path.startswith(root + "/"):
            path = root + path
        b = urllib.parse.urlparse(self.base)
        return urllib.parse.urlunparse((b.scheme, b.netloc, path, "", p.query, ""))

    def search_url(self, since_dt):
        q = urllib.parse.urlencode({
            "cql": build_cql(since_dt, self.spaces, self.tz, self.overlap, self.cfg.get("cql")),
            "limit": str(self.page_limit), "expand": EXPAND})
        return self.base + SEARCH_PATH + "?" + q

    # --- poll ----------------------------------------------------------------------------------------------------
    def poll(self, cursor):
        cur = cursor if isinstance(cursor, dict) else {}
        since = parse_when(cur.get("since"))
        seen_at = set(cur.get("ids") or [])
        if since is None:
            since = self._clock() - _dt.timedelta(seconds=int(self.cfg.get("firstLookbackSec",
                                                                             DEFAULT_FIRST_LOOKBACK_SEC)))
            seen_at = set()
        pend = cur.get("pending") if isinstance(cur.get("pending"), dict) else None
        hi = parse_when((pend or {}).get("hi")) or since
        hi_ids = set((pend or {}).get("hiIds") or []) if pend else set(seen_at)
        url = self._join(pend["next"]) if pend and pend.get("next") else self.search_url(since)
        try:
            try:
                events, read, nxt = self._read(url, since, seen_at)
            except W.SourceError as e:
                if not (pend and e.status in (400, 404, 410)):
                    raise
                # a saved `next` cursor the server no longer takes: start over from `since` (seen.db dedupes)
                hi, hi_ids = since, set(seen_at)
                events, read, nxt = self._read(self.search_url(since), since, seen_at)
        except W.SourceError as e:
            self._health["lastError"] = str(e)
            self._health["failStreak"] += 1
            self._save_health()
            raise
        new_cursor = self._advance(since, seen_at, hi, hi_ids, read, events, nxt)
        self._health.update(lastOk=iso_z(self._clock()), lastError=None, failStreak=0)
        self._save_health()
        return events, new_cursor, None

    def _read(self, url, since, seen_at):
        """Follow pages until done, until maxEventsPerPoll NEW changes (overlap rows older than the cursor do not
        count), or until maxPages requests. Returns (events, [(when, key)] read in server order, next url or None)."""
        events, read, pages = [], [], 0
        while url:
            data = self._get(url)
            pages += 1
            for c in (data or {}).get("results") or []:
                ver = c.get("version") or {}
                when = parse_when(ver.get("when"))
                if when is None:  # nullable in the schema; an Event never carries an empty `at`
                    continue
                key = "%s:%s" % (c.get("id"), ver.get("number"))
                read.append((when, key))
                if when < since or (when == since and key in seen_at):
                    continue
                ev = self.to_event(c, when)
                if ev is not None:
                    events.append(ev)
            url = self._next_url(data)
            if url and (len(events) >= self.max_events or pages >= self.max_pages):
                break
        events.sort(key=lambda e: (e.at, e.eid))
        return events, read, url

    def _advance(self, since, seen_at, hi, hi_ids, read, events, nxt):
        """The next cursor. `hi`/`hiIds` carry the newest change emitted so far across a run of truncated polls.
        Complete (no page left): since = the newest emitted instant over the run, ids = what sits at it.
        Truncated: save the unread `next` link in `pending` so the next poll resumes there instead of re-reading
        the first pages; `since` moves up only when every row read came back in ascending order (the server kept
        ORDER BY, which the docs do not promise), else it stays so no older unread row is skipped."""
        for e in events:
            w, k = parse_when(e.at), "%s:%s" % (e.fields.get("contentId", ""), e.fields.get("version"))
            if w > hi:
                hi, hi_ids = w, {k}
            elif w == hi:
                hi_ids.add(k)
        if not nxt:
            if hi == since:
                hi_ids |= seen_at
            return {"since": iso_z(hi), "ids": sorted(hi_ids)}
        ordered = all(read[i][0] <= read[i + 1][0] for i in range(len(read) - 1))
        p = urllib.parse.urlparse(nxt)
        pending = {"next": urllib.parse.urlunparse(("", "", p.path, "", p.query, "")), "hi": iso_z(hi),
                   "hiIds": sorted(hi_ids)}
        if ordered:
            return {"since": iso_z(hi), "ids": sorted(hi_ids), "pending": pending}
        return {"since": iso_z(since), "ids": sorted(seen_at), "pending": pending}

    def _save_health(self):
        self.state.update({k: self._health[k] for k in ("lastOk", "lastError", "failStreak")})

    # --- mapping -------------------------------------------------------------------------------------------------
    def _is_me(self, u):
        if self.me_id and u["accountId"] == self.me_id:
            return True
        return bool(self.me_names & {u["displayName"], u["publicName"]} - {""})

    def to_event(self, c, when=None):
        ctype = c.get("type")
        if ctype not in CONTENT_TYPES:
            return None
        ver = c.get("version") or {}
        when = when or parse_when(ver.get("when"))
        if when is None:
            return None
        number = ver.get("number")
        actor = _user(ver.get("by"))
        space = (c.get("space") or {}).get("key") or ""
        container = c.get("container") or {}
        links = c.get("_links") or {}
        tags, fields = [], {"contentId": str(c.get("id") or ""), "contentType": ctype, "version": number,
                            "space": space, "actorId": actor["accountId"]}
        title = c.get("title") or ""
        page_id, page_title = str(c.get("id") or ""), title
        creator = _user((c.get("history") or {}).get("createdBy"))
        if ctype == "comment":
            typ = "page.commented"
            # container is the page or blog post for a comment (Container schema); degrade when it is a space
            if container.get("type") in ("page", "blogpost") and container.get("id"):
                page_id = str(container["id"])
                page_title = container.get("title") or title
                fields["pageId"] = page_id
                pcreator = _user((container.get("history") or {}).get("createdBy"))
                if pcreator["accountId"] and self.me_id and pcreator["accountId"] == self.me_id:
                    tags.append("my-page")
            storage = ((c.get("body") or {}).get("storage") or {}).get("value") or ""
            mentioned = sorted(set(_MENTION.findall(storage)))
            fields["mentionedIds"] = mentioned
            if self.me_id and self.me_id in mentioned:
                tags.append("mention")
            quote = _text(storage, self.quote_chars)
        else:
            typ = "page.created" if number == 1 else "page.edited"
            quote = W.cap_quote(ver.get("message") or "", self.quote_chars)
            if number != 1 and creator["accountId"] and self.me_id and creator["accountId"] == self.me_id:
                tags.append("my-page")
        webui = links.get("webui") or ""
        url = (self.base + webui) if webui.startswith("/") else ""
        ref = "%s:%s" % (space, page_id) if space else page_id
        at = iso_z(when)
        return W.Event(
            eid=W.make_eid(self.kind, self.instance, "%s:%s" % (ref, c.get("id")), at, "%s:v%s" % (typ, number)),
            source=self.cfg.get("name") or "confluence", sourceKind=self.kind, at=at, type=typ, ref=ref, url=url,
            actor=actor["displayName"], me=self._is_me(actor), title=page_title, quote=quote, fields=fields,
            tags=tags)

    def health(self):
        return dict(self._health)
