# Jira recorded fixtures

Hand-written from Atlassian's public API docs. No live call made them. Every host is `acme.example.com`, every
project is `PROJ`, and every person is an `*_Example` name with a made-up `acct-*` or `JIRAUSER*` id.

`test/test_watch_jira.py` serves these through a fake transport. `routes.json` in each folder maps a GET (its path,
plus the query keys listed, where `null` means the key is absent) to a response file, status and headers. The test
also records every URL it was asked for, which is how it checks the poll query.

| Folder | Shape |
|---|---|
| `cloud/` | Jira Cloud, REST v3: `/myself`, enhanced search `/search/jql` (2 pages, `nextPageToken`), and `/issue/PROJ-2/changelog` for a truncated embedded changelog. ADF comment bodies. |
| `server/` | Jira Data Center / Server, REST v2: `/myself`, `/search` (2 pages, `startAt`/`total`). Wiki-markup comment bodies. |
| `webhook/` | A `jira:issue_updated` delivery for the PROJ-1 status change that `cloud/` also returns from the poll. |

## Sources

Checked against the docs (DOC). Quotes came from the raw pages or the published OpenAPI files:

- Enhanced search `GET /rest/api/3/search/jql`: the params `jql`, `nextPageToken`, `maxResults`, `fields` and
  `expand=changelog` ("a list of recent updates to an issue, sorted by date, starting from the most recent"), and
  the response `issues[]`, `nextPageToken`, `isLast`. The old `GET /rest/api/3/search` is deprecated.
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/ (OpenAPI:
  https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json)
- `GET /rest/api/3/issue/{issueIdOrKey}/changelog`: the params `startAt` and `maxResults`, the response
  `values[]` and `isLast`, sorted oldest first. Each entry has `id`, `author`, `created` and `items[]`, and each item
  has `field`, `fieldId`, `from`, `fromString`, `to` and `toString`. Same OpenAPI file.
- `GET /rest/api/3/myself`: it returns `accountId` and `timeZone` ("the time zone specified in the user's profile").
  `name` and `key` are no longer on Cloud. Same OpenAPI file.
- The REST datetime form `"2014-03-19T18:45:42.967+0000"`. Same OpenAPI file.
- Data Center REST v2 `GET /rest/api/2/search`: the params `jql`, `startAt`, `maxResults`, `fields` and `expand`,
  and the response `startAt`, `maxResults`, `total`, `issues`. The changelog has `startAt`, `maxResults`, `total`
  and `histories`. `GET /rest/api/2/myself` returns `name`, `key`, `displayName` and `timeZone`.
  https://developer.atlassian.com/server/jira/platform/jira_software_dc_10000_swagger.v3.json (linked from
  https://developer.atlassian.com/server/jira/platform/rest/v10000/)
- The ADF mention node `{"type": "mention", "attrs": {"id", "text", "accessLevel"}}`, where `text` includes the
  leading @. https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/mention/
- JQL relative dates, `"-<n>m"` and friends (`w`, `d`, `h`, `m`), measured back from the current time. The
  adapter polls with `updated >= "-<minutes since cursor + 15>m"`, so no time zone enters the query. Absolute
  literals (`"yyyy-MM-dd HH:mm"`) would be read "relative to your configured time zone", which the adapter cannot
  know for sure. https://support.atlassian.com/jira-software-cloud/docs/jql-fields/
- Webhook secret (checked by the receiver, not the adapter): an `X-Hub-Signature` header "formatted as `method=signature`", for example `sha256=<hex HMAC of
  the body>`. The payload carries `webhookEvent`, `timestamp`, `user`, `issue`, and `changelog` (on
  `jira:issue_updated` only). https://developer.atlassian.com/cloud/jira/platform/webhooks/
- Rate limits: a 429 with `Retry-After`, and `X-RateLimit-Remaining` ("the remaining request capacity within the
  current rate-limit window"). https://developer.atlassian.com/cloud/jira/platform/rate-limiting/

Not confirmed in a doc page (UNVERIFIED; check them against a real instance):

- How far apart this host's clock and Jira's may drift. The 15 min overlap covers skew up to that; the refetched
  overlap is dropped by the cursor check and the engine's eid dedupe. `test_mismatched_zone_still_emits_each_change_exactly_once`
  runs a fake server 10 min fast in a zone that differs from `/myself`.
- The webhook `timestamp` unit (epoch ms), `changelog.id` / `changelog.items[]`, and the top-level `comment` key.
- Data Center wiki-markup mentions `[~username]` and `[~accountid:...]`.
