# Bitbucket Cloud fixtures

Hand-written responses for `test/test_watch_bitbucket.py`. Nothing here was recorded from a live server. Names,
hosts and ids are invented: workspace `acme`, repo `widget`, users `Alice_Example`, `Bob_Example`,
`Human_Example` (the configured `me`, id `acct-me`) and `acme-ci[bot]`, hosts `api.acme.example.com` and
`acme.example.com`, ticket `PROJ-1`.

## Sources the shapes follow

| Shape | Doc |
|---|---|
| PR list: `state` repeats, default OPEN only, `q` filter, `sort`, envelope `pagelen` / `page` / `values` / `next` | OpenAPI spec `https://dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json`, path `/repositories/{workspace}/{repo_slug}/pullrequests` (get), schema `paginated_pullrequests`; rendered at https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/#api-repositories-workspace-repo-slug-pullrequests-get |
| Pull request object: `id`, `title`, `summary.raw`, `state` (OPEN, MERGED, DECLINED, SUPERSEDED), `author`, `source` / `destination` (`branch.name`, `commit.hash`), `merge_commit.hash`, `closed_by`, `reason`, `created_on`, `updated_on`, `links.html.href` | same spec, schema `pullrequest`, `pullrequest_endpoint` |
| `reviewers` and `participants` only on the by-id URL ("For performance reasons, the API only includes this list on a pull request's `self` URL") | same spec, schema `pullrequest`, properties `reviewers`, `participants`; schema `participant` |
| Activity log entries `comment`, `update`, `approval`, each with `pull_request` | same spec, path `/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/activity` (get) description examples |
| Branch with `target` commit (`hash`, `date`, `message`, `author.raw`, `author.user`) | same spec, path `/repositories/{workspace}/{repo_slug}/refs/branches/{name}`, schemas `branch`, `ref`, `commit`; push payload sample below |
| Filter syntax `updated_on >= 2015-10-04T14:00:00-07:00` (datetimes unquoted, second precision in every example), `sort=-field` | https://developer.atlassian.com/cloud/bitbucket/rest/intro/#filtering (text also embedded in the spec above) |
| Webhook headers `X-Event-Key`, `X-Hook-UUID`, `X-Request-UUID`, `X-Attempt-Number`; event keys `pullrequest:created`, `:updated`, `:approved`, `:changes_request_created`, `:fulfilled`, `:rejected`, `:comment_created`, `repo:push`; payload keys `actor`, `pullrequest`, `repository`, `approval {date, user}`, `changes_request {date, user}`, `comment`, `push.changes[].new.target`; `pullrequest:updated` is only `{actor, pullrequest, repository}` (no `changes` block, unlike `repo:updated` and `issue:updated`) | https://support.atlassian.com/bitbucket-cloud/docs/event-payloads/ |
| Webhook signature `X-Hub-Signature: sha256=<hex HMAC of the raw body>`, and the test vector (secret `It's a Secret to Everybody`, payload `Hello World!`) used in `test_verify_signature` | https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/ |
| Rate limits (1,000 per hour per user on `/2.0/repositories/*`, scaled for some tokens), `X-RateLimit-Limit`, `X-RateLimit-Resource`, `X-RateLimit-NearLimit`; no remaining-count header | https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/ |

## UNVERIFIED (not shown in any doc page read)

- The activity entry key for a change request. The docs list "request changes" among activity entries but give no
  sample; the fixtures use `changes_request {date, user}`, the webhook payload's key.
- The raw text of a mention in a comment. The adapter treats `@{<account id>}`, `@<id>` and `@<display name>` as a
  mention of `me`.
- The headers of a 429. The rate-limit page does not mention `Retry-After`; the adapter honors it when present.
- That a comment, approval or change request moves the PR's `updated_on`. The poll reads the activity log only for
  PRs the `updated_on` filter returns; if activity alone does not move it, those entries surface on the PR's next
  real update (or by webhook).
- That the activity log is newest first. Paging stops at the first entry older than the cursor (the doc examples
  are single entries and do not show the order).
- Whether `fields=+values.reviewers` would add reviewers to the list response. The adapter does not rely on it and
  reads open PRs by id.

## Scenario

- `seed/`: the first poll of a repo with no cursor. Newest PR #10 sets `since`; branch `main` head is recorded.
  No events.
- `poll1/`: PR #9 sits in the same second as `since` but earlier (the second-precision `q` returns it; the
  adapter's full-precision check skips it). PR #10 approved, then merged. PR #11 opened with `me` as reviewer and a
  comment mentioning `me`. PR #12 (by `me`, page 2 via `next`) gets a change request, a bot comment and a deleted
  comment. Branch `main` moves.
- `poll2/`: PR #12 at exactly `since` with its id in the cursor (skipped). PR #11 gets a new head commit. PR #10
  is edited after the merge (same merge commit, so the same eid).
- `webhook/`: the same changes as poll1, as webhook payloads, so their eids can be compared.
