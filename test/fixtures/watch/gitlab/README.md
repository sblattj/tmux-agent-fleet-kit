# Fixtures: WATCH source `gitlab` (GitLab and Gitea)

Hand-written from the vendors' public API docs. Nothing here was recorded from a live server. Every name is made up:
the org `acme`, the repo `acme/widget`, the host `acme.example.com`, users `alice_example`, `bob_example`,
`carol_example` and `human-example` (the "me" id), and the ticket `PROJ-1`.

Each `*_poll*.json` file is `{"clock": <ISO, the injected now>, "exchanges": [{"request": {method, url},
"response": {status, headers, body}}]}`. `test/test_watch_gitlab.py` plays them in order and fails on any request that
is not the next recorded one, so the recorded URL is the poll query under test. Each `*_hook_*.json` file is
`{"headers", "body"}` for `accept_webhook`. The tests compute signatures at run time from a fake secret.

| File | What it shows |
|---|---|
| `gitlab_poll1.json` | First poll (no cursor, 15 min lookback): two pages joined by `x-next-page`; a review request for me, a new MR mentioning me, a merge, a plain update, and the first sight of branch `main` |
| `gitlab_poll2.json` | The next poll from poll 1's cursor: the boundary MRs (same `updated_at`) are skipped. Also a reviewer added, new commits, a close and a push to `main` (a `-08:00` timestamp). `RateLimit-Remaining: 0` plus `RateLimit-Reset` raise the next poll |
| `gitlab_poll3_429.json` | 429 with `Retry-After: 60` |
| `gitlab_hook_merge.json` | Merge Request Hook (`action: merge`) for the MR poll 1 saw merge |
| `gitlab_hook_merge_legacy_ts.json` | The same hook with pre-19.0 `YYYY-MM-DD HH:MM:SS UTC` timestamps and no `merged_at` |
| `gitea_poll1.json` | First Gitea poll: newest first (`sort=recentupdate`); paging stops at the first PR older than the window; a new PR with me as reviewer and a merged PR (`state: closed`, `merged: true`) |
| `gitea_poll2.json` | The next poll: the merged PR at the cursor instant is skipped, a close is found and branch `main` is unchanged |
| `gitea_hook_merged.json` | `pull_request` hook, `action: closed` on a merged PR |

## Doc sources (read 2026-11-22)

GitLab:
- MR list, params (`scope`, `state`, `order_by=updated_at`, `sort`, inclusive `updated_after` in ISO 8601) and fields
  (`iid`, `state` opened/closed/merged/locked, `author.username`, `reviewers[]`, `merge_user` (`merged_by`
  deprecated), `closed_by`, `merged_at`, `closed_at`, `sha`, `web_url`, `draft`):
  https://docs.gitlab.com/api/merge_requests/
- URL-encoded project path (`acme%2Fwidget`), `page`/`per_page` (max 100), `x-next-page`/`x-total` headers:
  https://docs.gitlab.com/api/rest/
- `Authorization: Bearer <token>` is accepted for personal, project and group access tokens:
  https://docs.gitlab.com/api/rest/authentication/
- `RateLimit-Remaining`, `RateLimit-Reset` (Unix time), and `Retry-After` on a 429:
  https://docs.gitlab.com/administration/settings/user_and_ip_rate_limits/ and
  https://docs.gitlab.com/user/gitlab_com/rate_limits/
- Single branch and its `commit` (`id`, `title`, `author_name`, `committed_date` with an offset, `web_url`):
  https://docs.gitlab.com/api/branches/
- Webhooks: `X-Gitlab-Token` (plain secret), `X-Gitlab-Event: Merge Request Hook`, and the Standard Webhooks signature
  (`webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64 HMAC-SHA256 of id.timestamp.body>`, `whsec_`
  key, GitLab 19.0+): https://docs.gitlab.com/user/project/integrations/webhooks/
- MR hook payload (`object_kind`, `user`, `project.path_with_namespace`, `object_attributes.action` open / close /
  reopen / update / merge / ..., `changes.<field>.previous/current`, the `updated_at` format change in 19.0):
  https://docs.gitlab.com/user/project/integrations/webhook_events/

Gitea:
- `GET /repos/{owner}/{repo}/pulls` params (`state` open/closed/all, `sort` incl. `recentupdate`, `page`, `limit`; no
  time filter), `PullRequest` fields (`number`, `state` open/closed, `merged`, `merged_at`, `merged_by`, `user`,
  `requested_reviewers`, `closed_at`, `html_url`, `base.ref`, `head.ref`, `head.sha`, `draft`), and
  `GET /repos/{owner}/{repo}/branches/{branch}` (`commit`: `id`, `message`, `timestamp`, `url`, `author.username`):
  https://gitea.com/swagger.v1.json
- `Authorization: token <token>`, `page`/`limit`, `x-total-count`, `Link`, `MAX_RESPONSE_ITEMS` default 50:
  https://docs.gitea.com/development/api-usage
- Webhooks: `X-Gitea-Signature` = hex HMAC-SHA256 of the raw body, `X-Gitea-Event: pull_request`, actions (opened,
  closed, reopened, synchronized, review_requested, ...), payload `pull_request`, `repository`, `sender`:
  https://docs.gitea.com/usage/webhooks

UNVERIFIED (not shown verbatim in the doc pages read): the `login` key of Gitea's `User` object (used for `user`,
`merged_by`, `requested_reviewers` and `sender`); the full GitLab MR hook `object_attributes` field list (`iid`,
`url`, `last_commit`, `oldrev`) and the `changes.reviewers` shape. Check them against a real payload before relying
on the webhook path.
