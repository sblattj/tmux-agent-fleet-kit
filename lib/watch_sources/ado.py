"""Azure DevOps Boards source for `fleet watch` (four-desks.md sections 3.1 to 3.6, phase 6).

Poll = two steps, because WIQL returns work item ids only:
  1. POST {base}/{project}/_apis/wit/wiql?timePrecision=true&$top=N   (WIQL on [System.ChangedDate], explicit UTC)
  2. POST {base}/{project}/_apis/wit/workitemsbatch                    (fields for those ids, at most 200 per call)

Dates: WIQL reads a DateTime literal in the local time of the machine that runs the query unless it carries a zone, and
drops the time of day unless time precision is on. So every literal here is ISO 8601 with a trailing Z, and the WIQL
call always sends timePrecision=true. No macros (@today): the date is computed here, in UTC.

Cursor (JSON, saved by the engine): {"since": "<ISO UTC of the newest ChangedDate seen>", "seen": {"<id>": rev}}.
`seen` holds the ids already reported at exactly `since`, because the query is `>=` and re-returns them.

Change mapping: the batch gives current values only, so the source keeps a small memory per work item
(state["items"], bounded by ITEM_MEMORY) and diffs against it: state -> workitem.transition, assignee ->
workitem.assigned, comment count -> workitem.comment, rev 1 -> workitem.created, anything else -> workitem.updated.

Config entry (watch.json):
  {"name": "boards", "kind": "ado", "org": "acme", "project": "PROJ",
   "auth": {"type": "pat", "token": "env:ADO_PAT"},
   "base": "https://dev.azure.com/acme",   // optional; Azure DevOps Server: https://host/tfs/<collection>
   "query": "[System.AssignedTo] = @me",   // optional extra WIQL clause, ANDed in
   "backfillSec": 0,                        // optional; the first poll looks this far back (default: start now)
   }   // the engine injects cfg["me"] = {names, id} (source_cfg_with_me); id = ADO identity GUID or uniqueName

Service hooks: ADO sends HTTP basic auth (no HMAC). The loopback receiver (watch_sources/webhook.py) checks it with
its built-in "basic" verifier, so a route looks like
  {"name": "boards-hook", "kind": "webhook", "for": "boards", "signature": "basic",
   "auth": {"type": "basic", "user": "env:ADO_HOOK_USER", "secret": "env:ADO_HOOK_SECRET"}, "pollSec": 60}
accept_webhook only normalizes. The receiver builds this class with empty creds and empty state, so the previous
values come from the hook's own {oldValue, newValue} pairs; that gives the same change key, and so the same eid, as
the poll that diffs against its memory.

Stdlib only. Read-only: the only calls are the two POSTs above (both are queries; neither writes).
"""
import base64
import datetime
import html
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

API_VERSION = "7.1"
BATCH_MAX = 200  # Get Work Items Batch takes at most 200 ids
ITEM_MEMORY = 2000  # work items remembered for the diff; oldest by ChangedDate are forgotten first
FIELDS = (
    "System.Id",
    "System.Rev",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.AssignedTo",
    "System.ChangedDate",
    "System.ChangedBy",
    "System.CreatedDate",
    "System.CommentCount",
    "System.History",
)
_MENTION_ID = re.compile(r'data-vss-mention="[^"]*?,([0-9a-fA-F-]{36})"')
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_FRACTION = re.compile(r"\.(\d+)")


# --- small helpers ---------------------------------------------------------------------------------------------------
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_iso(s):
    """ADO dates look like 2026-11-20T10:02:11.25Z (0 to 7 fraction digits). Returns an aware UTC datetime."""
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


def iso_z(d):
    """Seconds-precision ISO 8601 UTC with a Z, the form used in WIQL literals and in Event.at."""
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wiql_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def identity(v):
    """An identity field as {name, unique, id}. The batch API may return an IdentityRef object or, in older
    payloads and service hooks, a string like 'Alice Example <alice@acme.example.com>'."""
    if not v:
        return {"name": "", "unique": "", "id": ""}
    if isinstance(v, dict):
        return {"name": v.get("displayName") or "", "unique": v.get("uniqueName") or "", "id": v.get("id") or ""}
    s = str(v)
    m = re.match(r"^(.*?)\s*<([^>]*)>\s*$", s)
    if m:
        return {"name": m.group(1), "unique": m.group(2), "id": ""}
    return {"name": s, "unique": "", "id": ""}


def html_text(s):
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def default_transport(method, url, headers, body, timeout):
    """One HTTP call. Returns (status, headers dict with lower-case keys, body bytes). Never raises on HTTP status."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


# --- the source ------------------------------------------------------------------------------------------------------
class AdoSource(Source):
    kind = "ado"

    def __init__(self, cfg, creds, state, transport=None, clock=None):
        super().__init__(cfg, creds, state)
        self.transport = transport or default_transport
        self.clock = clock or utc_now
        self.org = cfg.get("org", "")
        self.project = cfg.get("project", "")
        self.base = (cfg.get("base") or "https://dev.azure.com/%s" % urllib.parse.quote(self.org)).rstrip("/")
        self.max_events = int(cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"])
        self.quote_chars = int(cfg.get("quoteChars") or DEFAULTS["quoteChars"])
        # cfg["me"] is injected by the engine (source_cfg_with_me): {"names": [...], "id": <ADO identity GUID or
        # uniqueName, or a list of them, or None>}.
        me = cfg.get("me") or {}
        self.me_names = {str(n).lower() for n in (me.get("names") or []) if n}
        mid = me.get("id")
        self.me_ids = {str(x).lower() for x in ([mid] if isinstance(mid, str) else (mid or [])) if x}
        self.state.setdefault("items", {})
        self.state.setdefault("health", {"lastOk": None, "lastError": None, "failStreak": 0, "rateRemaining": None})
        self._retry_after = None

    # -- http ----------------------------------------------------------------------------------------------------
    def _auth_header(self):
        token = self.creds.get("token") or ""
        typ = (self.cfg.get("auth") or {}).get("type", "pat")
        if not token or typ == "none":
            return None
        if typ == "bearer":
            return "Bearer " + token
        user = (self.creds.get("user") or "") if typ == "basic" else ""  # a PAT goes as Basic base64(":" + PAT)
        return "Basic " + base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")

    def _post(self, path, query, payload):
        url = "%s/%s/%s?%s" % (self.base, urllib.parse.quote(self.project), path,
                               urllib.parse.urlencode(query, safe="$"))
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        try:
            status, h, raw = self.transport("POST", url, headers, body, HTTP_TIMEOUT_SEC)
        except (OSError, ValueError) as e:  # timeout, DNS, refused
            raise SourceError("ado %s: %s" % (path, type(e).__name__))
        rem = h.get("x-ratelimit-remaining")
        if rem is not None:
            try:
                self.state["health"]["rateRemaining"] = int(float(rem))
            except ValueError:
                pass
        retry = _num(h.get("retry-after"))
        if status == 429 or status >= 500:
            raise SourceError("ado %s: HTTP %d" % (path, status), retry_after=retry, status=status)
        if status in (401, 403):
            raise SourceError("ado %s: HTTP %d (check the token's Work Items Read scope)" % (path, status),
                              status=status)
        # A bad or expired PAT can come back as 203 (or 200) with an HTML sign-in page instead of JSON.
        ctype = h.get("content-type", "")
        if status == 203 or (raw[:1] == b"<") or ("html" in ctype):
            raise SourceError("ado %s: HTTP %d sign-in page, not JSON (bad or expired credential)" % (path, status),
                              status=status)
        if status != 200:
            raise SourceError("ado %s: HTTP %d" % (path, status), status=status)
        if retry:  # ADO can send Retry-After on a 200 when it starts delaying; it becomes the next_poll_sec floor
            self._retry_after = max(self._retry_after or 0, retry)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise SourceError("ado %s: response is not JSON" % path, status=status)

    # -- the poll ------------------------------------------------------------------------------------------------
    def build_wiql(self, since, after_id=None):
        """The WIQL text for one poll. `since` is an aware datetime; it goes in as an explicit UTC literal."""
        where = ["[System.TeamProject] = %s" % wiql_quote(self.project),
                 "[System.ChangedDate] >= %s" % wiql_quote(iso_z(since))]
        if after_id is not None:
            where.append("[System.Id] > %d" % int(after_id))
        extra = (self.cfg.get("query") or "").strip()
        if extra:
            where.append("(%s)" % extra)
        return ("SELECT [System.Id] FROM WorkItems WHERE %s ORDER BY [System.ChangedDate] ASC, [System.Id] ASC"
                % " AND ".join(where))

    def _wiql(self, since, after_id=None):
        res = self._post("_apis/wit/wiql", {"timePrecision": "true", "$top": str(self.max_events),
                                            "api-version": API_VERSION},
                         {"query": self.build_wiql(since, after_id)})
        return [int(w["id"]) for w in (res.get("workItems") or []) if "id" in w]

    def _batch(self, ids):
        out = []
        for i in range(0, len(ids), BATCH_MAX):
            res = self._post("_apis/wit/workitemsbatch", {"api-version": API_VERSION},
                             {"ids": ids[i:i + BATCH_MAX], "fields": list(FIELDS), "errorPolicy": "omit"})
            out.extend(v for v in (res.get("value") or []) if v and v.get("fields"))
        return out

    def poll(self, cursor):
        self._retry_after = None
        h = self.state["health"]
        try:
            events, new_cursor = self._poll(cursor)
        except SourceError as e:
            h["lastError"] = str(e)
            h["failStreak"] = int(h.get("failStreak") or 0) + 1
            raise
        h["lastOk"] = iso_z(self.clock())
        h["lastError"] = None
        h["failStreak"] = 0
        return events, new_cursor, self._retry_after

    def _poll(self, cursor):
        cursor = dict(cursor or {})
        if cursor.get("since"):
            since = parse_iso(cursor["since"])
        else:
            since = self.clock() - datetime.timedelta(seconds=int(self.cfg.get("backfillSec") or 0))
        seen = {str(k): int(v) for k, v in (cursor.get("seen") or {}).items()}
        floor = since.replace(microsecond=0)

        ids = self._wiql(floor)
        items = self._batch(ids) if ids else []
        fresh = self._fresh(items, since, seen)
        if not fresh and len(ids) >= self.max_events:
            # A full page that is all already-seen items at the cursor second: page past them by id once.
            ids = self._wiql(floor, after_id=max(ids))
            items = self._batch(ids) if ids else []
            fresh = self._fresh(items, since, seen)

        fresh.sort(key=lambda w: (parse_iso(w["fields"]["System.ChangedDate"]), int(w["id"])))
        events = []
        new_since, new_seen = since, dict(seen)
        for w in fresh:
            changed = parse_iso(w["fields"]["System.ChangedDate"])
            events.extend(self.map_item(w))
            if changed > new_since:
                new_since, new_seen = changed, {}
            new_seen[str(w["id"])] = int(w.get("rev") or w["fields"].get("System.Rev") or 0)
            if len(events) >= self.max_events:
                break
        self._trim_memory()
        new_cursor = {"since": _iso_full(new_since), "seen": new_seen}
        return events[: self.max_events], new_cursor

    @staticmethod
    def _fresh(items, since, seen):
        out = []
        for w in items:
            cd = w["fields"].get("System.ChangedDate")
            if not cd:
                continue
            changed = parse_iso(cd)
            rev = int(w.get("rev") or w["fields"].get("System.Rev") or 0)
            if changed < since:
                continue
            if changed == since and seen.get(str(w["id"])) == rev:
                continue
            out.append(w)
        return out

    # -- mapping -------------------------------------------------------------------------------------------------
    def _is_me(self, ident):
        if not ident:
            return False
        if ident.get("id") and ident["id"].lower() in self.me_ids:
            return True
        if ident.get("unique") and ident["unique"].lower() in self.me_ids:
            return True
        return bool(ident.get("name")) and ident["name"].lower() in self.me_names

    def _mentions_me(self, history_html):
        if not history_html:
            return False
        for gid in _MENTION_ID.findall(history_html):
            if gid.lower() in self.me_ids:
                return True
        text = html_text(history_html).lower()
        return any(("@" + n) in text for n in self.me_names)

    def web_url(self, wid):
        return "%s/%s/_workitems/edit/%d" % (self.base, urllib.parse.quote(self.project), int(wid))

    def map_item(self, w, prev=None):
        """Events for one work item, diffed against the remembered previous values (or `prev`, built from a service
        hook's oldValues). Updates the memory."""
        f = w.get("fields") or {}
        wid = int(w.get("id") or f.get("System.Id"))
        rev = int(w.get("rev") or f.get("System.Rev") or 0)
        at_dt = parse_iso(f["System.ChangedDate"])
        at = iso_z(at_dt)
        ref = "%s-%d" % (self.project, wid)
        actor = identity(f.get("System.ChangedBy"))
        assignee = identity(f.get("System.AssignedTo"))
        state_now = f.get("System.State") or ""
        comments = int(f.get("System.CommentCount") or 0)
        mem = self.state["items"].get(str(wid))
        if mem and int(mem.get("rev") or 0) >= rev:
            return []  # nothing newer than what was already mapped (a webhook got here first)
        if prev is None:
            prev = mem
        if prev and int(prev.get("rev") or 0) >= rev:
            return []  # nothing newer than what was already mapped (a webhook got here first)
        base_fields = {"id": wid, "rev": rev, "workItemType": f.get("System.WorkItemType") or "",
                       "state": state_now, "assignedTo": assignee["name"]}

        def ev(change, type_, quote, extra=None, tags=None):
            d = dict(base_fields)
            d.update(extra or {})
            return Event(eid=make_eid(self.kind, self.org, ref, at, change), source=self.cfg.get("name", "ado"),
                         sourceKind=self.kind, at=at, type=type_, ref=ref, url=self.web_url(wid),
                         actor=actor["name"], me=self._is_me(actor), title=f.get("System.Title") or "",
                         quote=cap_quote(quote, self.quote_chars), fields=d, tags=list(tags or []))

        out = []
        assigned_tags = ["assigned-to-me"] if self._is_me(assignee) else []
        if prev is None and rev <= 1:
            out.append(ev("created", "workitem.created",
                          "%s created: %s" % (f.get("System.WorkItemType") or "Work item", f.get("System.Title") or ""),
                          tags=assigned_tags))
        elif prev is not None:
            if (prev.get("state") or "") != state_now:
                out.append(ev("state", "workitem.transition", "State: %s -> %s" % (prev.get("state"), state_now),
                              {"from": prev.get("state"), "to": state_now}))
            if (prev.get("assignee") or "") != (assignee["unique"] or assignee["name"]):
                out.append(ev("assignee", "workitem.assigned",
                              "Assigned to: %s -> %s" % (prev.get("assigneeName") or "(none)",
                                                         assignee["name"] or "(none)"),
                              {"from": prev.get("assigneeName") or "", "to": assignee["name"]}, assigned_tags))
            if comments > int(prev.get("comments") or 0):
                hist = f.get("System.History") or ""
                out.append(ev("comment", "workitem.comment", html_text(hist), {"comments": comments},
                              ["mention"] if self._mentions_me(hist) else []))
        if not out:
            hist = f.get("System.History") or ""
            tags = ["mention"] if hist and self._mentions_me(hist) else []
            out.append(ev("updated", "workitem.updated", html_text(hist) or "Updated: %s" % (f.get("System.Title") or ""),
                          tags=tags))
        self.state["items"][str(wid)] = {"rev": rev, "state": state_now, "assignee": assignee["unique"] or assignee["name"],
                                         "assigneeName": assignee["name"], "comments": comments, "changed": at}
        return out

    def _trim_memory(self):
        items = self.state["items"]
        if len(items) > ITEM_MEMORY:
            for k in sorted(items, key=lambda k: items[k].get("changed") or "")[: len(items) - ITEM_MEMORY]:
                del items[k]

    # -- health and webhooks -------------------------------------------------------------------------------------
    def health(self):
        h = self.state["health"]
        return {"lastOk": h.get("lastOk"), "lastError": h.get("lastError"), "failStreak": int(h.get("failStreak") or 0),
                "rateRemaining": h.get("rateRemaining")}

    def accept_webhook(self, headers, body):
        """Normalize a service hook (workitem.created / .updated / .commented) into Events. NORMALIZE ONLY: the
        receiver already checked the basic auth, and this never reads creds. Previous values come from the payload
        ({oldValue, newValue} pairs in resource.fields), not from memory, so an empty-state instance maps a hook to the
        same events and eids as the poll."""
        try:
            doc = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except ValueError:
            raise SourceError("ado webhook: body is not JSON", status=400)
        if not isinstance(doc, dict):
            raise SourceError("ado webhook: body is not an object", status=400)
        etype = str(doc.get("eventType", ""))
        if not etype.startswith("workitem."):
            return []
        res = doc.get("resource") or {}
        rev = res.get("revision") if isinstance(res.get("revision"), dict) else res  # updated hooks nest it
        raw = rev.get("fields") or {}
        # workitem.updated puts {oldValue, newValue} pairs in resource.fields and the full values in
        # resource.revision.fields; take the current (new) value either way.
        fields = {k: (v.get("newValue") if isinstance(v, dict) and "newValue" in v else v) for k, v in raw.items()}
        pairs = {k: v for k, v in (res.get("fields") or {}).items()
                 if isinstance(v, dict) and ("newValue" in v or "oldValue" in v)}
        for k, v in pairs.items():
            fields.setdefault(k, v.get("newValue"))
        wid = rev.get("id") if rev is not res else (res.get("workItemId") or res.get("id"))
        wid = fields.get("System.Id") or wid
        if not wid or not fields.get("System.ChangedDate"):
            return []
        rev_no = int(rev.get("rev") or fields.get("System.Rev") or 0)
        item = {"id": int(wid), "rev": rev_no, "fields": fields}
        prev = None  # no oldValues (flat form, created): fall back to this instance's memory, if any
        if rev_no > 1 and (pairs or etype == "workitem.commented"):
            def old(name):  # a changed field with no oldValue was empty before
                return pairs[name].get("oldValue") if name in pairs else fields.get(name)
            before = identity(old("System.AssignedTo"))
            comments = int(old("System.CommentCount") or 0)
            if etype == "workitem.commented" and "System.CommentCount" not in pairs:
                comments = max(0, int(fields.get("System.CommentCount") or 0) - 1)
            prev = {"rev": rev_no - 1, "state": old("System.State") or "",
                    "assignee": before["unique"] or before["name"], "assigneeName": before["name"],
                    "comments": comments}
        return self.map_item(item, prev=prev)


def _num(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _iso_full(d):
    """Cursor form: keep microseconds so `>=` plus `seen` stays exact."""
    s = d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return s.rstrip("0").rstrip(".") + "Z"
