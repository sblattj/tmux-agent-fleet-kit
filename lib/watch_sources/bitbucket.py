"""WATCH source: Bitbucket Cloud pull requests and watched branches (docs/design/four-desks.md 3.1, 3.2, 3.5, 3.6).

Poll, per watched repo (cfg["repos"] = ["workspace/repo_slug", ...]):
  1. GET {api}/repositories/{ws}/{slug}/pullrequests
       ?state=OPEN&state=MERGED&state=DECLINED&state=SUPERSEDED   (the default is OPEN only; the param repeats)
       &q=updated_on >= <since, floored to the second>&sort=updated_on&pagelen=50
     following the response's `next` link (same origin only, so the token never leaves the configured API host).
  2. For each changed PR: GET .../pullrequests/{id} when it is OPEN (the list omits reviewers and participants;
     the docs say only the by-id URL carries them), and GET .../pullrequests/{id}/activity (comments, approvals,
     change requests).
  3. For each watched branch (cfg["branches"] = ["main", ...]): GET .../refs/branches/{name}.

Cursor (returned by poll, saved by the engine in watch/cursors.json):
  {"v": 1, "repos": {"acme/widget": {"since": <full-precision updated_on of the last PR processed>,
                                     "ids": [PR ids whose updated_on == since],
                                     "prs": {"12": {"state": "OPEN", "head": "<hash>", "reviewers": [ids],
                                                     "meReviewer": bool}},
                                     "branches": {"main": "<hash>"}}}}
A repo with no cursor yet is seeded (newest updated_on, current branch heads) and makes no events.

Change types: pr.opened, pr.review_requested, pr.pushed, pr.merged, pr.closed (declined or superseded),
pr.approved, pr.changes_requested, pr.comment, pr.updated (a change with nothing else to say), branch.pushed.

eid rule (contract accept_webhook docstring): built only from facts the poll and the webhook both carry and that do
not move on later edits: created_on (opened), merge commit hash (merged), head hash (pushed), approval / change
request date + user, comment id, reviewer id. So a webhook plus the next catch-up poll dedupe to one event.

Stdlib only. Credentials come in `creds` (resolved by the engine); they go only into the Authorization header and
never into an event, the cursor, health or an error message.
"""
import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
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
    RATE_PER_MIN,
    Event,
    Source,
    SourceError,
    cap_quote,
    make_eid,
)

KIND = "bitbucket"
API_DEFAULT = "https://api.bitbucket.org/2.0"
WEB_DEFAULT = "https://bitbucket.org"
PR_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")
PAGELEN = 50
MAX_LIST_PAGES = 10  # per repo per poll; the request budget usually stops sooner
MAX_ACTIVITY_PAGES = 2
USER_AGENT = "fleet-watch"
CURSOR_VERSION = 1


# --- time ------------------------------------------------------------------------------------------------------------
def parse_time(s):
    """ISO 8601 with Z or +HH:MM offset and optional fraction -> aware UTC datetime. Python 3.8 safe."""
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    if "." in s:  # fromisoformat on 3.8 wants exactly 3 or 6 fraction digits
        head, rest = s.split(".", 1)
        i = 0
        while i < len(rest) and rest[i].isdigit():
            i += 1
        frac, tail = rest[:i], rest[i:]
        s = head + "." + (frac + "000000")[:6] + tail
    d = _dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def iso_utc(s):
    """Normalize a vendor timestamp to UTC 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z' (Event.at)."""
    d = parse_time(s)
    if d is None:
        return ""
    out = d.strftime("%Y-%m-%dT%H:%M:%S")
    if d.microsecond:
        out += ".%06d" % d.microsecond
    return out + "Z"


def query_time(s):
    """The `q` filter value: floored to the second, '+00:00' offset (the docs show second-precision datetimes)."""
    return parse_time(s).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- HTTP ------------------------------------------------------------------------------------------------------------
def urllib_get(url, headers, timeout):
    """Default transport: GET -> (status, headers {lower-name: value}, body bytes). Never raises for an HTTP status."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


def _origin(url):
    p = urllib.parse.urlsplit(url)
    return (p.scheme.lower(), p.netloc.lower())


def _retry_after(headers):
    v = (headers or {}).get("retry-after")
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


# --- webhook signature (registered with the loopback receiver, fd-p6-webhook) -----------------------------------------
def verify_bitbucket(secret, headers, body):
    """True when X-Hub-Signature is 'sha256=<hex HMAC-SHA256 of the raw body keyed by the webhook secret>'.
    Bitbucket Cloud docs (manage-webhooks): header X-Hub-Signature, WebSub `method=signature` form, sha256 today."""
    if not secret:
        return False
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    sig = h.get("x-hub-signature") or ""
    if not isinstance(sig, str) or "=" not in sig:
        return False
    method, got = sig.split("=", 1)
    if method.strip().lower() != "sha256":
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    want = hmac.new(key, body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, got.strip().lower())


# --- the Source --------------------------------------------------------------------------------------------------------
class BitbucketSource(Source):
    kind = KIND

    def __init__(self, cfg, creds, state):
        super().__init__(cfg, creds, state)
        self.api = (cfg.get("api") or API_DEFAULT).rstrip("/")
        self.web = (cfg.get("web") or WEB_DEFAULT).rstrip("/")
        self.instance = urllib.parse.urlsplit(self.web).netloc or "bitbucket.org"
        self.repos = [r for r in (cfg.get("repos") or []) if isinstance(r, str) and r.count("/") == 1]
        self.branches = [b for b in (cfg.get("branches") or []) if isinstance(b, str) and b]
        self.max_events = int(cfg.get("maxEventsPerPoll") or DEFAULTS["maxEventsPerPoll"])
        self.poll_sec = int(cfg.get("pollSec") or DEFAULTS["pollSec"])
        self.max_requests = int(cfg.get("maxRequestsPerPoll") or RATE_PER_MIN)
        me = cfg.get("me") or {}
        self.me_id = me.get("id")
        self.me_names = [n for n in (me.get("names") or []) if n]
        self.http = urllib_get  # tests replace this with a recorded-response transport
        self.now = _now_iso
        self._requests = 0
        self._near_limit = False

    # -- identity --------------------------------------------------------------------------------------------------
    def _is_me(self, user):
        if not isinstance(user, dict):
            return False
        if self.me_id and self.me_id in (user.get("account_id"), user.get("uuid"), user.get("nickname")):
            return True
        return bool(self.me_names) and (user.get("display_name") in self.me_names or user.get("nickname") in self.me_names)

    @staticmethod
    def _uid(user):
        user = user or {}
        return user.get("account_id") or user.get("uuid") or user.get("nickname") or ""

    @staticmethod
    def _actor(user):
        user = user or {}
        return user.get("display_name") or user.get("nickname") or ""

    def _mentions_me(self, text):
        text = text or ""
        keys = []
        if self.me_id:
            keys += ["@{%s}" % self.me_id, "@" + self.me_id]
        keys += ["@" + n for n in self.me_names]
        return any(k in text for k in keys)

    # -- HTTP --------------------------------------------------------------------------------------------------------
    def _auth_header(self):
        a = (self.cfg.get("auth") or {}).get("type", "bearer")
        tok = self.creds.get("token")
        if a == "none" or not tok:
            return {}
        if a == "basic":
            raw = ("%s:%s" % (self.creds.get("user") or "", tok)).encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {"Authorization": "Bearer " + tok}

    def _get(self, url):
        if _origin(url) != _origin(self.api):
            raise SourceError("refusing a link outside the configured API origin (%s)" % _origin(url)[1])
        if self._requests >= self.max_requests:
            raise _Budget()
        self._requests += 1
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        headers.update(self._auth_header())
        status, rh, body = self.http(url, headers, HTTP_TIMEOUT_SEC)
        rh = {str(k).lower(): v for k, v in (rh or {}).items()}
        if str(rh.get("x-ratelimit-nearlimit", "")).lower() == "true":
            self._near_limit = True
        path = urllib.parse.urlsplit(url).path
        if status == 429:
            raise SourceError("bitbucket rate limited (429) on %s" % path, retry_after=_retry_after(rh), status=429)
        if status >= 500:
            raise SourceError("bitbucket server error %d on %s" % (status, path), retry_after=_retry_after(rh), status=status)
        if status in (401, 403):
            raise SourceError("bitbucket refused the credentials (%d) on %s" % (status, path), status=status)
        if status != 200:
            raise SourceError("bitbucket returned %d on %s" % (status, path), status=status)
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except ValueError:
            raise SourceError("bitbucket returned non-JSON on %s" % path, status=status)

    def _repo_url(self, repo, *parts):
        ws, slug = repo.split("/", 1)
        segs = [urllib.parse.quote(ws, safe=""), urllib.parse.quote(slug, safe="")]
        segs += [urllib.parse.quote(str(p), safe="") for p in parts]
        return "%s/repositories/%s" % (self.api, "/".join(segs))

    def list_url(self, repo, since=None, newest_first=False):
        """The poll query for one repo (the recorded-fixture test pins it)."""
        params = [("state", s) for s in PR_STATES]
        if since:
            params.append(("q", "updated_on >= %s" % query_time(since)))
        params.append(("sort", "-updated_on" if newest_first else "updated_on"))
        params.append(("pagelen", "1" if newest_first else str(PAGELEN)))
        return self._repo_url(repo, "pullrequests") + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    # -- poll ----------------------------------------------------------------------------------------------------------
    def poll(self, cursor):
        cur = cursor if isinstance(cursor, dict) and cursor.get("v") == CURSOR_VERSION else {"v": CURSOR_VERSION}
        repos_cur = dict(cur.get("repos") or {})
        events = []
        self._requests = 0
        self._near_limit = False
        try:
            for repo in self.repos:
                if len(events) >= self.max_events:
                    break
                rc = repos_cur.get(repo)
                try:
                    if not rc:
                        repos_cur[repo] = self._seed(repo)
                        continue
                    repos_cur[repo] = self._poll_repo(repo, rc, events)
                except _Budget:
                    break
        except SourceError as e:
            self._fail(e)
            raise
        self._ok()
        new_cursor = {"v": CURSOR_VERSION, "repos": repos_cur}
        nxt = 2 * self.poll_sec if self._near_limit else None
        return events, new_cursor, nxt

    def _seed(self, repo):
        data = self._get(self.list_url(repo, newest_first=True))
        vals = data.get("values") or []
        since = vals[0].get("updated_on") if vals else self.now()
        rc = {"since": since, "ids": [vals[0]["id"]] if vals else [], "prs": {}, "branches": {}}
        for b in self.branches:
            tgt = self._branch_target(repo, b)
            if tgt:
                rc["branches"][b] = tgt.get("hash")
        return rc

    def _poll_repo(self, repo, rc, events):
        since = rc.get("since")
        since_dt = parse_time(since)
        at_since = set(rc.get("ids") or [])
        prs = {k: dict(v) for k, v in (rc.get("prs") or {}).items()}
        branches = dict(rc.get("branches") or {})
        new_since, new_ids = since, list(at_since)
        url = self.list_url(repo, since)
        pages = 0
        done = False
        while url and pages < MAX_LIST_PAGES and not done:
            try:
                data = self._get(url)
            except _Budget:
                done = True
                break
            pages += 1
            for pr in data.get("values") or []:
                upd = parse_time(pr.get("updated_on"))
                if upd is None or upd < since_dt or (upd == since_dt and pr.get("id") in at_since):
                    continue  # the q filter is second-precision; this is the full-precision check
                try:
                    evs = self._pr_events(repo, pr, prs, since_dt)
                except _Budget:
                    done = True
                    break
                events.extend(evs)
                if pr.get("updated_on") == new_since or parse_time(new_since) == upd:
                    new_ids.append(pr.get("id"))
                else:
                    new_since, new_ids = pr.get("updated_on"), [pr.get("id")]
                if len(events) >= self.max_events:
                    done = True
                    break
            url = None if done else data.get("next")
        if not done:
            for b in self.branches:
                try:
                    tgt = self._branch_target(repo, b)
                except _Budget:
                    break
                if not tgt:
                    continue
                old = branches.get(b)
                if old and tgt.get("hash") and tgt.get("hash") != old:
                    events.append(self._branch_event(repo, b, tgt))
                branches[b] = tgt.get("hash")
        return {"since": new_since, "ids": sorted(set(new_ids), key=str), "prs": prs, "branches": branches}

    # -- mapping -------------------------------------------------------------------------------------------------------
    def _ref(self, repo, pr_id):
        return "%s#%s" % (repo, pr_id)

    def _pr_url(self, repo, pr):
        href = (((pr.get("links") or {}).get("html") or {}).get("href")) or ""
        return href or "%s/%s/pull-requests/%s" % (self.web, repo, pr.get("id"))

    def _ev(self, repo, pr, type_, eid_part, change, at, user, quote="", fields=None, tags=None):
        ref = self._ref(repo, pr.get("id"))
        mine = self._is_me(pr.get("author"))
        tags = list(tags or [])
        if mine and "my-pr" not in tags:
            tags.append("my-pr")
        f = {"repo": repo, "state": pr.get("state") or "", "branchOwner": self._actor(pr.get("author")),
             "sourceBranch": (((pr.get("source") or {}).get("branch") or {}).get("name")) or "",
             "destinationBranch": (((pr.get("destination") or {}).get("branch") or {}).get("name")) or ""}
        f.update(fields or {})
        return Event(
            eid=make_eid(KIND, self.instance, ref, eid_part, change),
            source=self.cfg.get("name") or KIND, sourceKind=KIND, at=iso_utc(at), type=type_, ref=ref,
            url=self._pr_url(repo, pr), actor=self._actor(user), me=self._is_me(user),
            title=pr.get("title") or "", quote=cap_quote(quote or pr.get("title") or ""), fields=f, tags=tags)

    def _opened(self, repo, pr):
        return self._ev(repo, pr, "pr.opened", pr.get("created_on"), "opened", pr.get("created_on"), pr.get("author"),
                        quote=((pr.get("summary") or {}).get("raw")) or pr.get("title") or "")

    def _review_requested(self, repo, pr):
        return self._ev(repo, pr, "pr.review_requested", "me", "review_requested:%s" % (self.me_id or "me"),
                        pr.get("updated_on"), pr.get("author"), tags=["review-requested"])

    def _pushed(self, repo, pr, old_head):
        head = (((pr.get("source") or {}).get("commit") or {}).get("hash")) or ""
        return self._ev(repo, pr, "pr.pushed", head, "pushed", pr.get("updated_on"), pr.get("author"),
                        fields={"from": old_head or "", "to": head})

    def _closed(self, repo, pr):
        st = pr.get("state")
        if st == "MERGED":
            mc = ((pr.get("merge_commit") or {}).get("hash")) or pr.get("updated_on")
            return self._ev(repo, pr, "pr.merged", mc, "merged", pr.get("updated_on"), pr.get("closed_by") or pr.get("author"),
                            fields={"mergeCommit": ((pr.get("merge_commit") or {}).get("hash")) or ""})
        return self._ev(repo, pr, "pr.closed", st, "closed", pr.get("updated_on"), pr.get("closed_by") or pr.get("author"),
                        quote=pr.get("reason") or pr.get("title") or "", fields={"reason": pr.get("reason") or ""})

    def _activity_event(self, repo, pr, entry):
        """One activity log entry -> Event or None (comment, approval, changes_request; update entries are skipped,
        the PR object already says what changed)."""
        if "comment" in entry:
            c = entry["comment"] or {}
            if c.get("deleted"):
                return None, None
            raw = ((c.get("content") or {}).get("raw")) or ""
            tags = ["mention"] if self._mentions_me(raw) else []
            ev = self._ev(repo, pr, "pr.comment", "comment:%s" % c.get("id"), "comment", c.get("created_on"), c.get("user"),
                          quote=raw, fields={"commentId": c.get("id"), "inline": bool(c.get("inline"))}, tags=tags)
            return ev, c.get("created_on")
        for key, type_ in (("approval", "pr.approved"), ("changes_request", "pr.changes_requested")):
            if key in entry:
                a = entry[key] or {}
                ev = self._ev(repo, pr, type_, a.get("date"), "%s:%s" % (key, self._uid(a.get("user"))), a.get("date"),
                              a.get("user"))
                return ev, a.get("date")
        return None, None

    def _activity(self, repo, pr, since_dt):
        out = []
        url = self._repo_url(repo, "pullrequests", pr.get("id"), "activity")
        pages = 0
        while url and pages < MAX_ACTIVITY_PAGES:
            data = self._get(url)
            pages += 1
            older = False
            for entry in data.get("values") or []:
                ev, when = self._activity_event(repo, pr, entry)
                t = parse_time(when) if when else None
                if t is not None and t <= since_dt:
                    older = True
                    continue
                if ev is not None:
                    out.append(ev)
            url = None if older else data.get("next")
        return out

    def _pr_events(self, repo, pr, prs, since_dt):
        key = str(pr.get("id"))
        prev = prs.get(key)
        full = pr
        if pr.get("state") == "OPEN":
            full = dict(pr)
            full.update(self._get(self._repo_url(repo, "pullrequests", pr.get("id"))))
        evs = []
        created = parse_time(full.get("created_on"))
        if prev is None and created is not None and created >= since_dt:
            evs.append(self._opened(repo, full))
        reviewers = [self._uid(u) for u in (full.get("reviewers") or [])]
        me_reviewer = full.get("state") == "OPEN" and any(self._is_me(u) for u in (full.get("reviewers") or []))
        if me_reviewer and not (prev or {}).get("meReviewer"):
            evs.append(self._review_requested(repo, full))
        head = (((full.get("source") or {}).get("commit") or {}).get("hash")) or ""
        if prev and prev.get("head") and head and head != prev.get("head") and full.get("state") == "OPEN":
            evs.append(self._pushed(repo, full, prev.get("head")))
        if full.get("state") in ("MERGED", "DECLINED", "SUPERSEDED") and (not prev or prev.get("state") != full.get("state")):
            evs.append(self._closed(repo, full))
        evs.extend(self._activity(repo, full, since_dt))
        if not evs:
            evs.append(self._ev(repo, full, "pr.updated", full.get("updated_on"), "updated", full.get("updated_on"),
                                full.get("author")))
        evs.sort(key=lambda e: e.at)
        if full.get("state") == "OPEN":
            prs[key] = {"state": "OPEN", "head": head, "reviewers": reviewers, "meReviewer": bool(me_reviewer)}
        else:
            prs.pop(key, None)  # closed PRs leave the cursor; it stays bounded by the open set
        return evs

    def _branch_target(self, repo, branch):
        data = self._get(self._repo_url(repo, "refs", "branches", branch))
        return data.get("target") or {}

    def _branch_event(self, repo, branch, tgt, actor_user=None):
        ref = "%s@%s" % (repo, branch)
        author = tgt.get("author") or {}
        user = actor_user or author.get("user") or {}
        actor = self._actor(user) or (author.get("raw") or "")
        href = (((tgt.get("links") or {}).get("html") or {}).get("href")) or "%s/%s/commits/%s" % (self.web, repo, tgt.get("hash"))
        return Event(
            eid=make_eid(KIND, self.instance, ref, tgt.get("hash") or "", "pushed"),
            source=self.cfg.get("name") or KIND, sourceKind=KIND, at=iso_utc(tgt.get("date")), type="branch.pushed",
            ref=ref, url=href, actor=actor, me=self._is_me(user), title="%s %s" % (repo, branch),
            quote=cap_quote((tgt.get("message") or "").strip()), fields={"repo": repo, "branch": branch, "to": tgt.get("hash") or ""},
            tags=[])

    # -- health ----------------------------------------------------------------------------------------------------------
    def _h(self):
        return self.state.setdefault("health", {"lastOk": None, "lastError": None, "failStreak": 0})

    def _ok(self):
        h = self._h()
        h["lastOk"] = self.now()
        h["lastError"] = None
        h["failStreak"] = 0

    def _fail(self, err):
        h = self._h()
        h["lastError"] = str(err)
        h["failStreak"] = int(h.get("failStreak") or 0) + 1

    def health(self):
        h = self._h()
        return {"lastOk": h.get("lastOk"), "lastError": h.get("lastError"), "failStreak": int(h.get("failStreak") or 0),
                "rateRemaining": None}  # Bitbucket Cloud sends no remaining count (only X-RateLimit-NearLimit)

    # -- webhook (normalize only; the receiver verifies with verify_bitbucket first) -------------------------------------
    def accept_webhook(self, headers, body):
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        key = h.get("x-event-key") or ""
        try:
            p = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except (ValueError, AttributeError):
            raise SourceError("bitbucket webhook body is not JSON")
        repo = ((p.get("repository") or {}).get("full_name")) or ""
        if repo not in self.repos:
            return []
        if key == "repo:push":
            out = []
            for ch in ((p.get("push") or {}).get("changes") or []):
                new = ch.get("new") or {}
                if new.get("type") == "branch" and new.get("name") in self.branches and new.get("target"):
                    out.append(self._branch_event(repo, new["name"], new["target"], actor_user=p.get("actor")))
            return out
        pr = p.get("pullrequest") or {}
        if not pr.get("id"):
            return []
        if key == "pullrequest:created":
            out = [self._opened(repo, pr)]
            if any(self._is_me(u) for u in (pr.get("reviewers") or [])):
                out.append(self._review_requested(repo, pr))
            return out
        if key in ("pullrequest:fulfilled", "pullrequest:rejected"):
            return [self._closed(repo, pr)]
        if key in ("pullrequest:approved", "pullrequest:changes_request_created"):
            field = "approval" if key.endswith("approved") else "changes_request"
            ev, _ = self._activity_event(repo, pr, {field: p.get(field) or {}})
            return [ev] if ev else []
        if key == "pullrequest:comment_created":
            ev, _ = self._activity_event(repo, pr, {"comment": p.get("comment") or {}})
            return [ev] if ev else []
        if key == "pullrequest:updated":
            if pr.get("state") == "OPEN" and any(self._is_me(u) for u in (pr.get("reviewers") or [])):
                return [self._review_requested(repo, pr)]
            # The payload is {actor, pullrequest, repository} with no "changes" block and no previous head
            # (event-payloads doc, "Pull request > Updated"), and the receiver gives this adapter no state, so a
            # new source commit cannot be told from a title or description edit. Emit nothing: the next poll
            # compares heads and reports pr.pushed or pr.updated, so webhook + poll stays one event.
            return []
        return []


class _Budget(Exception):
    """The per-poll request budget ran out; poll() stops at the last complete PR and keeps the cursor there."""


# --- register the signature scheme with the loopback receiver (route "signature": "bitbucket") ------------------------
def verify_for_receiver(secret, headers, body, route=None):
    """The receiver's verifier shape: raises SourceError(status=401) unless verify_bitbucket passes."""
    if not verify_bitbucket(secret, headers, body):
        raise SourceError("bitbucket signature missing or mismatch", status=401)


try:
    from watch_sources import webhook as _webhook
except ImportError:  # the receiver is optional
    _webhook = None
if _webhook is not None:
    try:
        _webhook.register_verifier("bitbucket", verify_for_receiver)
    except ValueError:  # already registered by an earlier import of this module
        pass
