"""WATCH source: GitHub (phase 2, Q86). Spec: docs/design/four-desks.md 3.1, 3.2, 3.5, 3.6; contract
lib/fleet_watch_contract.py.

Read-only. Per watched repo it polls GET /repos/{owner}/{repo}/events with If-None-Match (a 304 makes no events and,
when authenticated, costs no rate limit), and with a token also GET /notifications with If-Modified-Since. It obeys
X-Poll-Interval (returned as next_poll_sec, never lower than the server asks) and backs off on 429, on 403 with the
rate limit spent, and on 5xx (SourceError.retry_after: Retry-After, else X-RateLimit-Reset, else exponential with
jitter capped at BACKOFF_CAP_SEC).

Cursor (JSON, saved by the engine): {"repos": {"owner/repo": {"etag", "lastAt", "ids": [...]}},
"notifications": {"lastModified"}}. Events-API ids are not monotonic (push ids and issue ids come from different
counters and one page mixes them, seen live 2026-11-23), so the cursor keeps a bounded set of recent ids and a new
event is one whose id is not in the set. Self.state holds only failStreak and health.

eid: make_eid("github", <source name>, ref, <object timestamp or id>, <change kind>), built from fields a webhook
payload carries too (issue/PR created_at, closed_at, merged_at; comment, review and release ids; push head SHA),
never from the events-API id, so accept_webhook plus the next poll dedupe to one event.

cfg keys: repos (required), name, api (default https://api.github.com), notifications (default: on when a token is
present), backfill (default false: the first poll primes the cursor and emits nothing), maxEventsPerPoll,
me ({"id": login, "names": [...]}, or a login string; injected by the engine, see LANDER-NOTES-fd-p2-github.md).
creds: {"token": ...} optional (unauthenticated works for public repos at 60 req/h). Webhooks: no creds here.
"""
import http.client
import json
import random
import re
import sys
import os
import time
import urllib.error
import urllib.request

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from fleet_watch_contract import (  # noqa: E402
    BACKOFF_CAP_SEC, DEFAULTS, HTTP_TIMEOUT_SEC, Event, Source, SourceError, cap_quote, make_eid)

API = "https://api.github.com"
USER_AGENT = "fleet-watch"
ONCE = "once"  # eid time part for once-only transitions
SEEN_IDS_MAX = 500  # the events API serves at most 300 events per repo, so 500 ids always cover one window
PAGE_MAX = 100
PAGES_MAX = 3
BACKOFF_BASE_SEC = 60
# notification reasons the /events feed of a watched repo already reports (review_requested is not in /events)
EVENTS_COVER = ("mention", "team_mention", "assign")


def urllib_http(method, url, headers, timeout):
    """Default HTTP layer: (status, headers {lowercase: value}, body bytes). Never raises for an HTTP status;
    network errors raise OSError (urllib.error.URLError is one)."""
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:  # 304, 4xx, 5xx arrive here
        try:
            body = e.read() if e.fp else b""
        finally:
            e.close()
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body


def _h(headers, name):
    """Case-insensitive header lookup."""
    name = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _next_link(headers):
    link = _h(headers, "link") or ""
    m = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return m.group(1) if m else None


class GitHubSource(Source):
    kind = "github"

    # injection points for tests (constructor stays exactly (cfg, creds, state), as the contract says)
    http = staticmethod(urllib_http)
    clock = staticmethod(time.time)
    jitter = staticmethod(random.random)

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self.name = cfg.get("name") or "github"
        self.api = (cfg.get("api") or API).rstrip("/")
        self.repos = list(cfg.get("repos") or [])
        self.max_events = int(cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"])
        self.quote_chars = int(cfg.get("quoteChars") or DEFAULTS["quoteChars"])
        me = cfg.get("me")
        if isinstance(me, str):
            me = {"id": me}
        me = me if isinstance(me, dict) else {}
        self.me_id = (me.get("id") or "").lower()
        self.me_names = {n.lower() for n in (me.get("names") or []) if isinstance(n, str)}
        if self.me_id:
            self.me_names.add(self.me_id)
        notif = cfg.get("notifications")
        self.notifications = bool(self.creds.get("token")) if notif is None else bool(notif and self.creds.get("token"))
        self._health = {"lastOk": self.state.get("lastOk"), "lastError": self.state.get("lastError"),
                        "failStreak": int(self.state.get("failStreak") or 0), "rateRemaining": None}

    # --- HTTP ------------------------------------------------------------------------------------------------------
    def _headers(self, extra=None):
        h = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        tok = self.creds.get("token")
        if tok:
            h["Authorization"] = "Bearer " + tok
        h.update(extra or {})
        return h

    def _get(self, url, extra=None):
        try:
            status, headers, body = self.http("GET", url, self._headers(extra), HTTP_TIMEOUT_SEC)
        except (OSError, http.client.HTTPException) as e:  # network, timeout, bad status line; no secret in the text
            raise SourceError("github: %s: %s" % (self._path(url), type(e).__name__), retry_after=self._backoff())
        rem = _int(_h(headers, "x-ratelimit-remaining"))
        if rem is not None:
            self._health["rateRemaining"] = rem
        if status in (200, 304):
            return status, headers, body
        retry = _int(_h(headers, "retry-after"))
        limited = status == 429 or (status == 403 and (rem == 0 or retry is not None))
        if limited and retry is None:
            reset = _int(_h(headers, "x-ratelimit-reset"))
            if reset is not None:
                retry = max(1, reset - int(self.clock()))
        if retry is None and (limited or status >= 500):
            retry = self._backoff()
        if retry is not None:
            retry = min(BACKOFF_CAP_SEC, max(retry, 1))
        raise SourceError("github: %s: HTTP %d%s" % (self._path(url), status, " (rate limited)" if limited else ""),
                          retry_after=retry, status=status)

    def _json_list(self, url, body):
        try:
            v = json.loads((body or b"").decode("utf-8") or "[]")
        except ValueError:
            raise SourceError("github: %s: response is not JSON" % self._path(url), retry_after=self._backoff())
        if not isinstance(v, list):
            raise SourceError("github: %s: response is not a list" % self._path(url), retry_after=self._backoff())
        return v

    def _path(self, url):
        return url[len(self.api):] if url.startswith(self.api) else url

    def _backoff(self):
        """Exponential with jitter, capped: 60, 120, 240 ... s times 0.5..1.0, by the current fail streak."""
        n = self._health["failStreak"]
        base = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** min(n, 16)))
        return max(1, int(base * (0.5 + 0.5 * self.jitter())))

    # --- poll --------------------------------------------------------------------------------------------------------
    def poll(self, cursor):
        cur = json.loads(json.dumps(cursor)) if isinstance(cursor, dict) else {}
        repos_cur = cur.setdefault("repos", {})
        events, interval, errors, budget = [], None, [], self.max_events
        for repo in self.repos:
            if budget <= 0:
                break  # the rest wait for the next poll, cursors untouched
            try:
                got, iv = self._poll_repo(repo, repos_cur.setdefault(repo, {}), budget)
            except SourceError as e:
                errors.append(e)
                continue
            events.extend(got)
            budget -= len(got)
            interval = max(interval or 0, iv or 0) or interval
        if self.notifications and budget > 0:
            try:
                got, iv = self._poll_notifications(cur.setdefault("notifications", {}), budget)
                events.extend(got)
                interval = max(interval or 0, iv or 0) or interval
            except SourceError as e:
                errors.append(e)
        attempted = len(self.repos) + (1 if self.notifications else 0)
        if errors and len(errors) >= attempted:
            self._fail(errors)
            raise SourceError(str(errors[0]), retry_after=max((e.retry_after or 0) for e in errors) or None,
                              status=errors[0].status)
        if errors:  # partial: keep what worked, wait as long as the slowest failure asks
            self._health["lastError"] = str(errors[0])
            interval = max([interval or 0] + [e.retry_after or 0 for e in errors]) or None
        else:
            self._health["lastError"] = None
        self._ok()
        return events, cur, interval

    def _ok(self):
        self._health["lastOk"] = _iso(self.clock())
        self._health["failStreak"] = 0
        self.state.update({"lastOk": self._health["lastOk"], "lastError": self._health["lastError"], "failStreak": 0})

    def _fail(self, errors):
        self._health["failStreak"] += 1
        self._health["lastError"] = str(errors[0])
        self.state.update({"lastError": self._health["lastError"], "failStreak": self._health["failStreak"]})

    def health(self):
        return dict(self._health)

    def _poll_repo(self, repo, rc, budget):
        first = "etag" not in rc and "ids" not in rc
        url = "%s/repos/%s/events?per_page=%d" % (self.api, repo, PAGE_MAX)
        extra = {"If-None-Match": rc["etag"]} if rc.get("etag") else {}
        status, headers, body = self._get(url, extra)
        interval = _int(_h(headers, "x-poll-interval"))
        if status == 304:
            return [], interval
        seen = set(rc.get("ids") or [])
        raw = self._json_list(url, body)
        fresh = [e for e in raw if str(e.get("id")) not in seen]
        nxt, pages = _next_link(headers), 1
        all_new = len(fresh) == len(raw)
        # a page that was entirely new may hide more new events behind it: follow Link while that holds (the API
        # serves at most 3 pages of 100); the budget only limits what is emitted, never what is fetched
        while nxt and raw and all_new and not first and pages < PAGES_MAX:
            _, h2, b2 = self._get(nxt)
            pages += 1
            raw = self._json_list(nxt, b2)
            page = [e for e in raw if str(e.get("id")) not in seen]
            fresh.extend(page)
            all_new = len(page) == len(raw)
            nxt = _next_link(h2)
        fresh.sort(key=lambda e: (e.get("created_at") or "", str(e.get("id"))))  # oldest first
        if first and not self.cfg.get("backfill"):
            emit, truncated = [], False  # prime: remember everything, emit nothing
            keep = fresh
        else:
            truncated = len(fresh) > budget
            emit = fresh[:budget]
            keep = emit
        ids = [str(e.get("id")) for e in reversed(keep)] + list(rc.get("ids") or [])
        rc["ids"] = ids[:SEEN_IDS_MAX]
        if keep:
            rc["lastAt"] = max([rc.get("lastAt") or ""] + [e.get("created_at") or "" for e in keep])
        etag = _h(headers, "etag")
        if etag and not truncated:
            rc["etag"] = etag
        else:  # unemitted events remain: the next poll must refetch, not get a 304
            rc.pop("etag", None)
            rc.setdefault("ids", [])
        out = []
        for e in emit:
            ev = self.map_event(e)
            if ev is not None:
                out.append(ev)
        return out, interval

    def _poll_notifications(self, nc, budget):
        url = "%s/notifications?per_page=%d" % (self.api, min(PAGE_MAX, budget))
        extra = {"If-Modified-Since": nc["lastModified"]} if nc.get("lastModified") else {}
        status, headers, body = self._get(url, extra)
        interval = _int(_h(headers, "x-poll-interval"))
        if status == 304:
            return [], interval
        lm = _h(headers, "last-modified")
        if lm:
            nc["lastModified"] = lm
        out = []
        for n in self._json_list(url, body)[:budget]:
            repo = (n.get("repository") or {}).get("full_name")
            if repo in self.repos and n.get("reason") in EVENTS_COVER:
                continue  # the repo's own event feed already carries this one; one ask, not two
            ev = self.map_notification(n)
            if ev is not None:
                out.append(ev)
        return out, interval

    # --- mapping -----------------------------------------------------------------------------------------------------
    def _is_me(self, login):
        return bool(login) and login.lower() in self.me_names

    def _mentions_me(self, text):
        return bool(self.me_id) and re.search(r"(?<![\w-])@%s\b" % re.escape(self.me_id), text or "", re.I) is not None

    def _event(self, repo, ref, at, type_, actor, ts, change, url="", title="", quote="", fields=None, tags=None):
        tags = list(tags or [])
        if self._mentions_me(quote) and "mention" not in tags:
            tags.append("mention")
        return Event(eid=make_eid("github", self.name, ref, ts or at, change), source=self.name, sourceKind="github",
                     at=at, type=type_, ref=ref, url=url, actor=actor or "", me=self._is_me(actor), title=title or "",
                     quote=cap_quote(quote or "", self.quote_chars), fields=fields or {}, tags=tags)

    def map_event(self, e):
        """One events-API row (or a webhook normalized into that shape) -> Event, or None to skip."""
        t = e.get("type") or ""
        p = e.get("payload") or {}
        repo = (e.get("repo") or {}).get("name") or ""
        actor = (e.get("actor") or {}).get("login") or ""
        at = e.get("created_at") or ""
        action = p.get("action") or ""
        html = "https://github.com/" + repo

        if t in ("IssuesEvent", "PullRequestEvent"):
            is_pr = t == "PullRequestEvent"
            o = p.get("pull_request" if is_pr else "issue") or {}
            num = o.get("number") or p.get("number")
            ref = "%s#%s" % (repo, num)
            pre = "pr" if is_pr else "issue"
            verb = action  # the events API says "merged"; a webhook says "closed" with merged: true
            if is_pr and action == "closed" and (o.get("merged") or o.get("merged_at")):
                verb = "merged"
            # opened and merged happen once per issue/PR, so their eid needs no timestamp: the events feed may trim
            # the payload (merged_at absent) and the webhook still dedupes with it
            ts = ONCE if verb in ("opened", "merged") else (
                o.get("closed_at") if verb == "closed" else None) or o.get("updated_at") or at
            tags, fields = [], {"action": action, "state": o.get("state")}
            if action == "assigned":
                who = (p.get("assignee") or {}).get("login")
                fields["assignee"] = who
                if self._is_me(who):
                    tags.append("assigned-to-me")
            if action == "review_requested":
                who = (p.get("requested_reviewer") or {}).get("login")
                fields["reviewer"] = who
                if self._is_me(who):
                    tags.append("review-requested")
            type_ = change = "%s.%s" % (pre, verb)
            if action in ("assigned", "review_requested", "labeled", "unlabeled"):
                change += ":" + str(fields.get("assignee") or fields.get("reviewer") or
                                    (p.get("label") or {}).get("name") or "")
            if is_pr:
                fields.update({"base": (o.get("base") or {}).get("ref"), "head": (o.get("head") or {}).get("ref")})
            quote = o.get("body") if action == "opened" else (o.get("title") or "")
            return self._event(repo, ref, at, type_, actor, ts, change, url=o.get("html_url") or "%s/%s/%s" % (
                html, "pull" if is_pr else "issues", num), title=o.get("title"), quote=quote, fields=fields, tags=tags)

        if t in ("IssueCommentEvent", "PullRequestReviewCommentEvent", "CommitCommentEvent"):
            c = p.get("comment") or {}
            if t == "CommitCommentEvent":
                ref, title, kindp = "%s@%s" % (repo, (c.get("commit_id") or "")[:12]), "", "commit"
            else:
                o = p.get("issue") or p.get("pull_request") or {}
                is_pr = t == "PullRequestReviewCommentEvent" or bool(o.get("pull_request"))
                ref, title, kindp = "%s#%s" % (repo, o.get("number")), o.get("title"), "pr" if is_pr else "issue"
            ts = c.get("updated_at") if action == "edited" else c.get("created_at")
            return self._event(repo, ref, at, "%s.comment" % kindp, actor, ts or at,
                               "comment.%s:%s" % (c.get("id"), action or "created"), url=c.get("html_url") or "",
                               title=title, quote=c.get("body"), fields={"action": action or "created"})

        if t == "PullRequestReviewEvent":
            r, o = p.get("review") or {}, p.get("pull_request") or {}
            state = (r.get("state") or "").lower()
            ref = "%s#%s" % (repo, o.get("number"))
            return self._event(repo, ref, at, "pr.review.%s" % (state or action), actor, r.get("submitted_at") or at,
                               "review.%s" % r.get("id"), url=r.get("html_url") or o.get("html_url") or "",
                               title=o.get("title"), quote=r.get("body") or state, fields={"state": state})

        if t == "ReleaseEvent":
            r = p.get("release") or {}
            tag = r.get("tag_name") or ""
            # "released" so score_for counts it as a state change (STATE_CHANGE_TYPES)
            type_ = "repo.released" if action == "published" else "release.%s" % action
            return self._event(repo, "%s@%s" % (repo, tag), at, type_, actor, r.get("published_at") or at,
                               "release.%s:%s" % (r.get("id"), action), url=r.get("html_url") or "",
                               title=r.get("name") or tag, quote=r.get("body") or tag, fields={"tag": tag})

        if t == "PushEvent":
            branch = re.sub(r"^refs/heads/", "", p.get("ref") or "")
            head = p.get("head") or p.get("after") or ""
            commits = p.get("commits") or []
            msg = (commits[-1].get("message") if commits else "") or ""
            return self._event(repo, "%s@%s" % (repo, branch), at, "push", actor, head, "push:%s" % branch,
                               url="%s/commit/%s" % (html, head) if head else html, title=branch,
                               quote=msg or "pushed %s to %s" % (head[:12], branch),
                               fields={"branch": branch, "head": head, "before": p.get("before")})

        if t in ("CreateEvent", "DeleteEvent"):
            rt, name = p.get("ref_type") or "", p.get("ref") or ""
            verb = "create" if t == "CreateEvent" else "delete"
            ref = "%s@%s" % (repo, name) if name else repo
            return self._event(repo, ref, at, "%s.%s" % (verb, rt), actor, name or at, "%s.%s" % (verb, rt),
                               url=html, title=name or repo, quote="%s %s %s" % (verb, rt, name),
                               fields={"refType": rt})

        # anything else (fork, star, public, member, wiki): keyed by time, type and actor, never the events-API id
        short = re.sub(r"Event$", "", t).lower() or "event"
        return self._event(repo, repo, at, "repo.%s" % short, actor, at, "%s:%s" % (short, actor), url=html,
                           title=repo, quote=("%s %s" % (short, action)).strip(), fields={"action": action})

    def map_notification(self, n):
        subj, repo = n.get("subject") or {}, ((n.get("repository") or {}).get("full_name") or "")
        reason = n.get("reason") or ""
        m = re.search(r"/(?:pulls|issues)/(\d+)$", subj.get("url") or "")
        ref = "%s#%s" % (repo, m.group(1)) if m else repo
        tags = {"review_requested": ["review-requested"], "mention": ["mention"], "team_mention": ["mention"],
                "assign": ["assigned-to-me"]}.get(reason, [])
        url = ""
        if m:
            url = "https://github.com/%s/%s/%s" % (repo, "pull" if "/pulls/" in subj["url"] else "issues", m.group(1))
        return self._event(repo, ref, n.get("updated_at") or "", "notification.%s" % reason, "", n.get("updated_at"),
                           "notify.%s" % reason, url=url, title=subj.get("title"), quote=subj.get("title"),
                           fields={"reason": reason, "subjectType": subj.get("type")}, tags=tags)

    # --- webhooks (phase 6) ------------------------------------------------------------------------------------------
    WEBHOOK_TYPES = {"issues": "IssuesEvent", "pull_request": "PullRequestEvent", "issue_comment": "IssueCommentEvent",
                     "pull_request_review": "PullRequestReviewEvent",
                     "pull_request_review_comment": "PullRequestReviewCommentEvent",
                     "commit_comment": "CommitCommentEvent", "release": "ReleaseEvent", "push": "PushEvent",
                     "create": "CreateEvent", "delete": "DeleteEvent"}

    def accept_webhook(self, headers, body):
        """Normalize one delivery through the same mapping as poll(), so both give the same eid. Normalize ONLY: the
        receiver (watch_sources/webhook.py, signature "github") verifies X-Hub-Signature-256 before this runs and
        builds the adapter with empty creds (ROUTE ruling)."""
        name = _h(headers, "x-github-event") or ""
        t = self.WEBHOOK_TYPES.get(name)
        if not t:
            return []
        try:
            p = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except ValueError:
            raise SourceError("github webhook: body is not JSON", status=400)
        if not isinstance(p, dict):
            raise SourceError("github webhook: body is not an object", status=400)
        repo = (p.get("repository") or {}).get("full_name") or ""
        if self.repos and repo not in self.repos:
            return []
        if t == "PushEvent":
            p = dict(p, head=p.get("after"))
        if t == "ReleaseEvent" and p.get("action") != "published":
            return []  # created/released/edited fire too; /events reports published only
        o = (p.get("pull_request") or p.get("issue") or p.get("comment") or p.get("review") or p.get("release") or
             p.get("head_commit") or {})
        at = (o.get("submitted_at") or o.get("updated_at") or o.get("published_at") or o.get("timestamp") or
              _iso(self.clock()))
        ev = self.map_event({"type": t, "payload": p, "repo": {"name": repo},
                             "actor": {"login": (p.get("sender") or {}).get("login")}, "created_at": at})
        return [ev] if ev else []


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

