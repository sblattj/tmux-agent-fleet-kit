"""WATCH source `gitlab`: GitLab and Gitea merge/pull requests and watched branches, in one module.

One Source class (kind "gitlab") serves both forges; the source entry's `flavor` picks the API dialect:

  {"name": "acme-forge", "kind": "gitlab", "flavor": "gitlab",      // or "gitea"; default "gitlab"
   "base": "https://acme.example.com", "repos": ["acme/widget"],
   "branches": ["main"],                                            // optional: watched branches (push events)
   // "me" is injected by the engine (source_cfg_with_me): {"names": [...], "id": me.ids[name or kind]}
   "lookbackSec": 900,                                              // optional: first-poll window, default pollSec
   "auth": {"type": "bearer", "token": "env:ACME_FORGE_TOKEN"}}     // read-only token, a reference

Webhooks (optional) are a separate `webhook` route that the receiver verifies; this module registers the verifiers
"gitlab" (X-Gitlab-Token, or Standard Webhooks webhook-signature with a whsec_ token) and "gitea" (X-Gitea-Signature):

  {"name": "forge-hook", "kind": "webhook", "for": "acme-forge", "signature": "gitlab",   // or "gitea"
   "auth": {"type": "hmac", "secret": "env:ACME_FORGE_HOOK_SECRET"}, "pollSec": 60}

Spec: docs/design/four-desks.md 3.1 (interface), 3.2 (poll query, cursor, change types), 3.4 (credentials),
3.5 (webhooks), 3.6 (bounds). API facts and doc URLs: test/fixtures/watch/gitlab/README.md.

Poll query, per watched repo:
  GitLab  GET {base}/api/v4/projects/{url-encoded path}/merge_requests
              ?scope=all&state=all&order_by=updated_at&sort=asc&updated_after=<cursor>&per_page=100&page=N
          (updated_after is inclusive; pages follow the x-next-page header)
  Gitea   GET {base}/api/v1/repos/{owner}/{repo}/pulls?state=all&sort=recentupdate&limit=50&page=N
          (no server-side time filter: newest first, paging stops at the first item older than the cursor)
  Branch  GitLab GET .../projects/{id}/repository/branches/{branch}; Gitea GET .../repos/{o}/{r}/branches/{branch}

Cursor (JSON, persisted by the engine in watch/cursors.json):
  {"v": 1, "repos": {"acme/widget": {"updatedAt": iso, "ids": [numbers updated at that instant],
                                     "known": {"12": {"state", "reviewers", "sha"}}, "branches": {"main": sha}}}}
The next poll asks from `updatedAt` (inclusive) and skips the `ids` already handled at that instant, so the boundary
neither repeats nor loses a change. When maxEventsPerPoll is reached the cursor stops at the last request handled.

Change types: pr.opened, pr.review_requested, pr.pushed, pr.merged, pr.closed, pr.reopened, pr.updated,
branch.pushed. Read-only: every request is a GET. Credentials arrive resolved in `creds` and go only into the
Authorization header (GitLab "Bearer <token>", Gitea "token <token>"); they are never logged or put in an Event.
"""
import base64
import calendar
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from fleet_watch_contract import (  # noqa: E402
    DEFAULTS,
    HTTP_TIMEOUT_SEC,
    POLL_FLOOR_SEC,
    Event,
    Source,
    SourceError,
    cap_quote,
    make_eid,
)

FLAVORS = ("gitlab", "gitea")
GITLAB_PER_PAGE = 100  # GitLab max per_page
GITEA_LIMIT = 50  # Gitea default MAX_RESPONSE_ITEMS
MAX_PAGES = 10  # per repo per poll
KNOWN_MAX = 500  # remembered requests per repo (oldest by updatedAt dropped)
WEBHOOK_TOLERANCE_SEC = 300  # Standard Webhooks timestamp window (GitLab webhook-signature)

_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?\s*(Z|UTC|[+-]\d{2}:?\d{2})?$")


def norm_ts(value):
    """Any forge timestamp -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC, second resolution). '' for empty or unparseable.

    Handles '2026-11-20T10:02:11.520Z', '2026-11-20T03:02:11-07:00' and the pre-19.0 GitLab webhook form
    '2026-11-20 10:02:11 UTC'. Second resolution makes a webhook and a poll of one change share an eid."""
    m = _TS.match((value or "").strip())
    if not m:
        return ""
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    tz = m.group(8) or "Z"
    epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    if tz not in ("Z", "UTC"):
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        epoch -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _epoch(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) if iso else 0


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _first_line(text):
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def urllib_transport(method, url, headers, timeout):
    """Default transport: (status, {lowercased header: value}, body bytes). HTTP error codes are returned, not
    raised; network failures raise OSError."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


class GitLabSource(Source):
    """GitLab (`flavor: gitlab`, REST v4) and Gitea (`flavor: gitea`, REST v1) pull/merge requests."""

    kind = "gitlab"

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self.flavor = (cfg.get("flavor") or "gitlab").lower()
        if self.flavor not in FLAVORS:
            raise SourceError("flavor must be one of %s" % ", ".join(FLAVORS))
        self.base = (cfg.get("base") or "").rstrip("/")
        self.instance = urllib.parse.urlsplit(self.base).netloc or self.base
        self.repos = list(cfg.get("repos") or [])
        self.branches = list(cfg.get("branches") or [])
        me = cfg.get("me") or {}  # injected by the engine: {"names": [...], "id": <forge username> | None}
        if isinstance(me, dict):
            me = list(me.get("names") or []) + ([me["id"]] if me.get("id") else [])
        elif isinstance(me, str):
            me = [me]
        self.me = {str(m).lower() for m in me if m}
        self.max_events = int(cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"])
        self.quote_chars = int(cfg.get("quoteChars") or DEFAULTS["quoteChars"])
        self.lookback = int(cfg.get("lookbackSec") or cfg.get("pollSec") or DEFAULTS["pollSec"])
        self.transport = urllib_transport  # tests replace this with a fixture replayer
        self.clock = time.time  # tests inject a fixed clock
        self.requests = []  # URLs of this poll's GETs (no headers, so no credential)
        h = self.state.get("health") or {}
        self._health = {
            "lastOk": h.get("lastOk"),
            "lastError": h.get("lastError"),
            "failStreak": int(h.get("failStreak") or 0),
            "rateRemaining": h.get("rateRemaining"),
        }
        self._next_poll = None

    # --- HTTP ----------------------------------------------------------------------------------------------------
    def _api(self):
        return self.base + ("/api/v4" if self.flavor == "gitlab" else "/api/v1")

    def _headers(self):
        h = {"Accept": "application/json", "User-Agent": "fleet-watch"}
        tok = self.creds.get("token")
        if tok:
            h["Authorization"] = ("Bearer " if self.flavor == "gitlab" else "token ") + tok
        return h

    def _get(self, path, query=None):
        url = self._api() + path + ("?" + urllib.parse.urlencode(query) if query else "")
        self.requests.append(url)
        try:
            status, headers, body = self.transport("GET", url, self._headers(), HTTP_TIMEOUT_SEC)
        except OSError as e:
            raise SourceError("%s: network error: %s" % (path, type(e).__name__))
        self._note_rate(headers)
        if status == 429 or status >= 500:
            ra = headers.get("retry-after")
            ra = int(ra) if ra and str(ra).isdigit() else None
            raise SourceError("%s: HTTP %d" % (path, status), retry_after=ra, status=status)
        if status == 404:
            raise SourceError("%s: HTTP 404 (repo or branch not found, or no access)" % path, status=404)
        if status >= 400:
            raise SourceError("%s: HTTP %d" % (path, status), status=status)
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body), headers
        except ValueError:
            raise SourceError("%s: response is not JSON" % path, status=status)

    def _note_rate(self, headers):
        rem = headers.get("ratelimit-remaining")
        if rem is not None and str(rem).isdigit():
            self._health["rateRemaining"] = int(rem)
            reset = headers.get("ratelimit-reset")
            if int(rem) == 0 and reset and str(reset).isdigit():
                self._next_poll = max(POLL_FLOOR_SEC, int(reset) - int(self.clock()))

    def _repo_path(self, repo):
        if self.flavor == "gitlab":
            return "/projects/" + urllib.parse.quote(repo, safe="")
        owner, name = repo.split("/", 1)
        return "/repos/%s/%s" % (urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe=""))

    # --- list requests -------------------------------------------------------------------------------------------
    def _list(self, repo, since):
        """Requests updated at or after `since` (ISO), oldest first, as raw vendor dicts."""
        base = self._repo_path(repo)
        out = []
        if self.flavor == "gitlab":
            page = 1
            for _ in range(MAX_PAGES):
                q = [("scope", "all"), ("state", "all"), ("order_by", "updated_at"), ("sort", "asc"),
                     ("updated_after", since), ("per_page", str(GITLAB_PER_PAGE)), ("page", str(page))]
                items, headers = self._get(base + "/merge_requests", q)
                out.extend(items or [])
                nxt = headers.get("x-next-page") or ""
                if not nxt.isdigit():
                    break
                page = int(nxt)
        else:
            page = 1
            for _ in range(MAX_PAGES):
                q = [("state", "all"), ("sort", "recentupdate"), ("limit", str(GITEA_LIMIT)), ("page", str(page))]
                items, _h = self._get(base + "/pulls", q)
                items = items or []
                older = False
                for it in items:
                    if norm_ts(it.get("updated_at")) < since:
                        older = True
                        break
                    out.append(it)
                if older or len(items) < GITEA_LIMIT:
                    break
                page += 1
        out = [it for it in out if norm_ts(it.get("updated_at")) >= since]
        out.sort(key=lambda it: (norm_ts(it.get("updated_at")), self._num(it)))
        return out

    def _num(self, it):
        return int(it.get("iid") if self.flavor == "gitlab" else it.get("number"))

    # --- normalize one vendor request ----------------------------------------------------------------------------
    def _norm(self, repo, it):
        """Vendor MR/PR dict -> a flavor-neutral dict."""
        if self.flavor == "gitlab":
            state = it.get("state") or ""
            merged = state == "merged"
            closed = state in ("closed", "locked")
            merger = (it.get("merge_user") or it.get("merged_by") or {}).get("username", "")
            closer = (it.get("closed_by") or {}).get("username", "")
            user = lambda u: (u or {}).get("username", "")  # noqa: E731
            reviewers = [user(r) for r in it.get("reviewers") or []]
            assignees = [user(a) for a in it.get("assignees") or []]
            author = user(it.get("author"))
            url, body = it.get("web_url", ""), it.get("description") or ""
            target, source, sha = it.get("target_branch", ""), it.get("source_branch", ""), it.get("sha", "")
            sep = "!"
        else:
            merged = bool(it.get("merged"))
            closed = (it.get("state") == "closed") and not merged
            state = "merged" if merged else ("closed" if closed else "open")
            merger = (it.get("merged_by") or {}).get("login", "")
            closer = ""
            user = lambda u: (u or {}).get("login", "")  # noqa: E731
            reviewers = [user(r) for r in it.get("requested_reviewers") or []]
            assignees = [user(a) for a in it.get("assignees") or []]
            author = user(it.get("user"))
            url, body = it.get("html_url", ""), it.get("body") or ""
            target = (it.get("base") or {}).get("ref", "")
            source = (it.get("head") or {}).get("ref", "")
            sha = (it.get("head") or {}).get("sha", "")
            sep = "#"
        num = self._num(it)
        return {
            "repo": repo, "number": num, "ref": "%s%s%d" % (repo, sep, num), "state": state,
            "merged": merged, "closed": closed, "author": author, "merger": merger, "closer": closer,
            "reviewers": [r for r in reviewers if r], "assignees": [a for a in assignees if a],
            "title": it.get("title") or "", "body": body, "url": url, "draft": bool(it.get("draft")),
            "target": target, "source": source, "sha": sha or "",
            "createdAt": norm_ts(it.get("created_at")), "updatedAt": norm_ts(it.get("updated_at")),
            "mergedAt": norm_ts(it.get("merged_at")), "closedAt": norm_ts(it.get("closed_at")),
        }

    def _is_me(self, name):
        return bool(name) and name.lower() in self.me

    def _event(self, pr, change, at, actor, quote, extra_fields=None, extra_tags=()):
        tags = list(extra_tags)
        if self.me:
            if self._is_me(pr["author"]) and not self._is_me(actor):
                tags.append("my-pr")
            if any(self._is_me(a) for a in pr["assignees"]):
                tags.append("assigned-to-me")
            if any(re.search(r"(?<![\w@])@%s\b" % re.escape(m), pr["body"], re.I) for m in self.me):
                tags.append("mention")
        fields = {"repo": pr["repo"], "number": pr["number"], "state": pr["state"], "flavor": self.flavor,
                  "targetBranch": pr["target"], "sourceBranch": pr["source"], "sha": pr["sha"],
                  "draft": pr["draft"], "reviewers": pr["reviewers"]}
        fields.update(extra_fields or {})
        at = at or pr["updatedAt"]
        return Event(
            eid=make_eid(self.kind, self.instance, pr["ref"], at, change),
            source=self.cfg.get("name", ""), sourceKind=self.kind, at=at, type="pr." + change,
            ref=pr["ref"], url=pr["url"], actor=actor or "", me=self._is_me(actor), title=pr["title"],
            quote=cap_quote(quote or pr["title"], self.quote_chars), fields=fields,
            tags=sorted(set(tags)),
        )

    def _changes(self, pr, prev, since):
        """Events for one request given its remembered state (`prev`, None when first seen)."""
        evs = []
        new = prev is None and pr["createdAt"] >= since
        if new:
            evs.append(self._event(pr, "opened", pr["createdAt"], pr["author"], pr["body"] or pr["title"]))
        was_merged = prev is not None and prev.get("state") == "merged"
        was_closed = prev is not None and prev.get("state") == "closed"
        if pr["merged"] and not was_merged and (prev is not None or pr["mergedAt"] >= since):
            evs.append(self._event(pr, "merged", pr["mergedAt"] or pr["updatedAt"], pr["merger"],
                                   "Merged into %s: %s" % (pr["target"], pr["title"]),
                                   {"from": (prev or {}).get("state", "open"), "to": "merged"}))
        elif pr["closed"] and not was_closed and (prev is not None or pr["closedAt"] >= since):
            evs.append(self._event(pr, "closed", pr["closedAt"] or pr["updatedAt"], pr["closer"],
                                   "Closed: %s" % pr["title"],
                                   {"from": (prev or {}).get("state", "open"), "to": "closed"}))
        elif prev is not None and prev.get("state") in ("closed", "merged") and pr["state"] in ("opened", "open"):
            evs.append(self._event(pr, "reopened", pr["updatedAt"], "", "Reopened: %s" % pr["title"],
                                   {"from": prev.get("state"), "to": pr["state"]}))
        before = set(prev.get("reviewers") or []) if prev is not None else set()
        if prev is None and not new:
            added = [r for r in pr["reviewers"] if self._is_me(r)]  # first sight: only my own request is news
        else:
            added = [r for r in pr["reviewers"] if r not in before]
        if added and not pr["merged"] and not pr["closed"]:
            tags = ["review-requested"] if any(self._is_me(r) for r in added) else []
            evs.append(self._event(pr, "review_requested", pr["updatedAt"], "",
                                   "Review requested from %s: %s" % (", ".join(added), pr["title"]),
                                   {"added": added}, tags))
        if (prev is not None and prev.get("sha") and pr["sha"] and prev["sha"] != pr["sha"]
                and not pr["merged"] and not pr["closed"]):
            evs.append(self._event(pr, "pushed", pr["updatedAt"], pr["author"],
                                   "New commits on %s: %s" % (pr["source"], pr["title"]),
                                   {"from": prev["sha"], "to": pr["sha"]}))
        if not evs:
            evs.append(self._event(pr, "updated", pr["updatedAt"], "", pr["title"]))
        return evs

    def _remember(self, pr):
        return {"state": "closed" if pr["closed"] else pr["state"], "reviewers": pr["reviewers"],
                "sha": pr["sha"], "updatedAt": pr["updatedAt"]}

    # --- branches ------------------------------------------------------------------------------------------------
    def _branch(self, repo, branch, prev_sha):
        data, _h = self._get(self._repo_path(repo) + "/repository/branches/" + urllib.parse.quote(branch, safe="")
                             if self.flavor == "gitlab" else
                             self._repo_path(repo) + "/branches/" + urllib.parse.quote(branch, safe=""))
        c = data.get("commit") or {}
        sha = c.get("id") or ""
        if not prev_sha or not sha or sha == prev_sha:
            return sha, None
        if self.flavor == "gitlab":
            at, actor, msg = norm_ts(c.get("committed_date")), c.get("author_name", ""), c.get("title") or c.get("message")
            url = c.get("web_url", "")
        else:
            a = c.get("author") or {}
            at, actor, msg = norm_ts(c.get("timestamp")), a.get("username") or a.get("name", ""), c.get("message")
            url = c.get("url", "")
        ref = "%s@%s" % (repo, branch)
        at = at or _iso(self.clock())
        return sha, Event(
            eid=make_eid(self.kind, self.instance, ref, at, "pushed:" + sha[:12]),
            source=self.cfg.get("name", ""), sourceKind=self.kind, at=at, type="branch.pushed", ref=ref,
            url=url, actor=actor, me=self._is_me(actor), title="%s %s" % (repo, branch),
            quote=cap_quote(_first_line(msg) or sha, self.quote_chars),
            fields={"repo": repo, "branch": branch, "from": prev_sha, "to": sha, "flavor": self.flavor},
        )

    # --- Source interface ----------------------------------------------------------------------------------------
    def poll(self, cursor):
        self.requests = []
        self._next_poll = None
        cur = json.loads(json.dumps(cursor)) if isinstance(cursor, dict) else {}
        cur.setdefault("v", 1)
        repos_cur = cur.setdefault("repos", {})
        default_since = _iso(int(self.clock()) - self.lookback)
        events = []
        try:
            for repo in self.repos:
                rc = repos_cur.setdefault(repo, {})
                since = rc.get("updatedAt") or default_since
                done_ids = set(rc.get("ids") or []) if rc.get("updatedAt") else set()
                known = rc.setdefault("known", {})
                last_at, last_ids = rc.get("updatedAt"), list(rc.get("ids") or [])
                full = False
                for it in self._list(repo, since):
                    pr = self._norm(repo, it)
                    if pr["updatedAt"] == rc.get("updatedAt") and pr["number"] in done_ids:
                        continue
                    evs = self._changes(pr, known.get(str(pr["number"])), since)
                    if events and len(events) + len(evs) > self.max_events:
                        full = True
                        break
                    events.extend(evs)
                    known[str(pr["number"])] = self._remember(pr)
                    if pr["updatedAt"] != last_at:
                        last_at, last_ids = pr["updatedAt"], []
                    last_ids.append(pr["number"])
                if last_at:
                    rc["updatedAt"], rc["ids"] = last_at, sorted(set(last_ids))
                else:
                    rc["updatedAt"], rc["ids"] = default_since, []
                if len(known) > KNOWN_MAX:
                    keep = sorted(known.items(), key=lambda kv: kv[1].get("updatedAt", ""))[-KNOWN_MAX:]
                    rc["known"] = dict(keep)
                if full:
                    break
                bcur = rc.setdefault("branches", {})
                for br in self.branches:
                    if len(events) >= self.max_events:
                        break
                    sha, ev = self._branch(repo, br, bcur.get(br))
                    if sha:
                        bcur[br] = sha
                    if ev:
                        events.append(ev)
        except SourceError as e:
            self._health["lastError"] = str(e)
            self._health["failStreak"] += 1
            self.state["health"] = dict(self._health)
            raise
        self._health.update({"lastOk": _iso(self.clock()), "lastError": None, "failStreak": 0})
        self.state["health"] = dict(self._health)
        return events, cur, self._next_poll

    def health(self):
        return dict(self._health)

    # --- webhooks (phase 6): normalize only; the receiver verifies (verify_gitlab / verify_gitea below) -----------
    def accept_webhook(self, headers, body):
        """Map a verified merge/pull request hook to the same Events (same eids) a poll would produce. Other hook
        kinds return []. Never reads creds: the receiver (lib/watch_sources/webhook.py) already checked the
        signature with the verifier this module registers, and builds this Source with empty creds."""
        raw = body if isinstance(body, (bytes, bytearray)) else (body or "").encode("utf-8")
        try:
            p = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SourceError("webhook body is not JSON", status=400)
        if not isinstance(p, dict):
            raise SourceError("webhook body is not a JSON object", status=400)
        if self.flavor == "gitlab":
            if p.get("object_kind") != "merge_request":
                return []
            repo = (p.get("project") or {}).get("path_with_namespace", "")
            a = dict(p.get("object_attributes") or {})
            a.setdefault("web_url", a.get("url", ""))
            a.setdefault("reviewers", p.get("reviewers") or [])
            a.setdefault("assignees", p.get("assignees") or [])
            a.setdefault("sha", (a.get("last_commit") or {}).get("id", ""))
            action, actor = a.get("action", ""), (p.get("user") or {}).get("username", "")
            changes = p.get("changes") or {}
            pr = self._norm(repo, a)
            if action == "open":
                return [self._event(pr, "opened", pr["createdAt"], actor, pr["body"] or pr["title"])]
            if action == "merge":
                return [self._event(pr, "merged", pr["mergedAt"] or pr["updatedAt"], actor,
                                    "Merged into %s: %s" % (pr["target"], pr["title"]),
                                    {"from": "opened", "to": "merged"})]
            if action == "close":
                return [self._event(pr, "closed", pr["closedAt"] or pr["updatedAt"], actor,
                                    "Closed: %s" % pr["title"], {"from": "opened", "to": "closed"})]
            if action == "reopen":
                return [self._event(pr, "reopened", pr["updatedAt"], actor, "Reopened: %s" % pr["title"],
                                    {"from": "closed", "to": pr["state"]})]
            if "reviewers" in changes:
                prev = {r.get("username") for r in (changes["reviewers"].get("previous") or [])}
                added = [r.get("username") for r in (changes["reviewers"].get("current") or [])
                         if r.get("username") not in prev]
                if added:
                    tags = ["review-requested"] if any(self._is_me(r) for r in added) else []
                    return [self._event(pr, "review_requested", pr["updatedAt"], actor,
                                        "Review requested from %s: %s" % (", ".join(added), pr["title"]),
                                        {"added": added}, tags)]
            if a.get("oldrev"):
                return [self._event(pr, "pushed", pr["updatedAt"], actor,
                                    "New commits on %s: %s" % (pr["source"], pr["title"]),
                                    {"from": a.get("oldrev"), "to": pr["sha"]})]
            return [self._event(pr, "updated", pr["updatedAt"], actor, pr["title"])]
        # gitea
        if "pull_request" not in p:
            return []
        repo = (p.get("repository") or {}).get("full_name", "")
        action, actor = p.get("action", ""), (p.get("sender") or {}).get("login", "")
        pr = self._norm(repo, p["pull_request"])
        if action == "opened":
            return [self._event(pr, "opened", pr["createdAt"], actor, pr["body"] or pr["title"])]
        if action == "closed" and pr["merged"]:
            return [self._event(pr, "merged", pr["mergedAt"] or pr["updatedAt"], actor,
                                "Merged into %s: %s" % (pr["target"], pr["title"]), {"from": "open", "to": "merged"})]
        if action == "closed":
            return [self._event(pr, "closed", pr["closedAt"] or pr["updatedAt"], actor,
                                "Closed: %s" % pr["title"], {"from": "open", "to": "closed"})]
        if action == "reopened":
            return [self._event(pr, "reopened", pr["updatedAt"], actor, "Reopened: %s" % pr["title"],
                                {"from": "closed", "to": "open"})]
        if action == "review_requested":
            who = (p.get("requested_reviewer") or {}).get("login", "")
            added = [who] if who else pr["reviewers"]
            tags = ["review-requested"] if any(self._is_me(r) for r in added) else []
            return [self._event(pr, "review_requested", pr["updatedAt"], actor,
                                "Review requested from %s: %s" % (", ".join(added), pr["title"]),
                                {"added": added}, tags)]
        if action == "synchronized":
            return [self._event(pr, "pushed", pr["updatedAt"], actor,
                                "New commits on %s: %s" % (pr["source"], pr["title"]), {"to": pr["sha"]})]
        return [self._event(pr, "updated", pr["updatedAt"], actor, pr["title"])]


# --- webhook verifiers (ROUTE ruling: the receiver alone verifies; these are registered with it at import) ----------
_clock = time.time  # tests inject a fixed clock for the Standard Webhooks timestamp window


def _hdr(headers, name):
    want = name.lower()
    for k, v in (headers.items() if hasattr(headers, "items") else headers or ()):
        if k.lower() == want:
            return v
    return None


def _raw(body):
    if not isinstance(body, (bytes, bytearray)):
        raise SourceError("signature check needs the raw body bytes", status=500)
    return bytes(body)


def verify_gitlab(secret, headers, body, route=None):
    """GitLab. With a `whsec_` signing token (GitLab 19.0+): Standard Webhooks `webhook-signature`
    (v1,<base64 HMAC-SHA256 of id.timestamp.body>) within WEBHOOK_TOLERANCE_SEC of `webhook-timestamp`. Otherwise
    the plain-text `X-Gitlab-Token`, compared in constant time. Raises SourceError(status=401) on a mismatch."""
    raw = _raw(body)
    if not secret:
        raise SourceError("no webhook secret", status=401)
    if secret.startswith("whsec_"):
        sig, msg_id, ts = (_hdr(headers, h) or "" for h in ("webhook-signature", "webhook-id", "webhook-timestamp"))
        if not sig:
            raise SourceError("webhook-signature missing", status=401)
        if not ts.isdigit() or abs(int(_clock()) - int(ts)) > WEBHOOK_TOLERANCE_SEC:
            raise SourceError("webhook-timestamp outside the window", status=401)
        try:
            key = base64.b64decode(secret[len("whsec_"):], validate=True)
        except ValueError:
            raise SourceError("signing token is not base64 after whsec_", status=500)
        want = base64.b64encode(hmac.new(key, msg_id.encode("utf-8") + b"." + ts.encode("ascii") + b"." + raw,
                                         hashlib.sha256).digest())
        sigs = [x.split(",", 1)[1].encode("ascii", "replace") for x in sig.split() if x.startswith("v1,")]
        if not any(hmac.compare_digest(want, x) for x in sigs):
            raise SourceError("webhook-signature mismatch", status=401)
        return
    got = _hdr(headers, "X-Gitlab-Token") or ""
    if not got or not hmac.compare_digest(got.encode("utf-8"), secret.encode("utf-8")):
        raise SourceError("X-Gitlab-Token mismatch", status=401)


def verify_gitea(secret, headers, body, route=None):
    """Gitea: `X-Gitea-Signature` = hex HMAC-SHA256(secret, raw body), no prefix. Gitea sends the header with an
    empty digest when no secret is set; that is refused. Raises SourceError(status=401) on a mismatch."""
    raw = _raw(body)
    if not secret:
        raise SourceError("no webhook secret", status=401)
    got = (_hdr(headers, "X-Gitea-Signature") or "").strip()
    want = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(want.encode("ascii"), got.encode("ascii", "replace")):
        raise SourceError("X-Gitea-Signature mismatch", status=401)


VERIFIER_NAMES = {"gitlab": verify_gitlab, "gitea": verify_gitea}  # a webhook route's `signature` value


def _register_verifiers():
    """Register with the receiver module: the canonical watch_sources.webhook, plus __main__ when the receiver runs
    as `python3 lib/watch_sources/webhook.py serve` (that copy holds its own VERIFIERS)."""
    mods = []
    try:
        from watch_sources import webhook as _wh
        mods.append(_wh)
    except ImportError:
        pass
    main = sys.modules.get("__main__")
    if (main is not None and os.path.basename(getattr(main, "__file__", "") or "") == "webhook.py"
            and hasattr(main, "register_verifier")):
        mods.append(main)
    for wh in mods:
        for name, fn in VERIFIER_NAMES.items():
            have = wh.VERIFIERS.get(name)
            if have is not None and getattr(have, "__name__", "") == fn.__name__ and "gitlab" in (
                    getattr(have, "__module__", "") or ""):
                wh.VERIFIERS[name] = fn  # a reload or second copy of this module: replace our own earlier copy
                continue
            wh.register_verifier(name, fn)


_register_verifiers()
