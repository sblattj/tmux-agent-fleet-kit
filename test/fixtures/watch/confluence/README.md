# Confluence fixtures

These recorded responses are written by hand for `test/test_watch_confluence.py`. None was captured from a live
server, and the test makes no network call. Each file has the form `{"status", "headers", "body"}`. The body follows
the vendor's documented response shape. Names are ACME, PROJ-1 and acme.example.com only.

## Where each shape comes from

| What | Source | Status |
|---|---|---|
| Endpoint `GET /wiki/rest/api/content/search` (operationId `searchContentByCQL`), with params `cql` (required), `cqlcontext`, `cursor`, `limit` (default 25) and `expand`. `start` is not listed for this operation. | https://developer.atlassian.com/cloud/confluence/swagger.v3.json | DOC |
| Pagination: "When additional results are available, returns `next` and `prev` URLs", e.g. `"next": "/rest/api/content/search?cql=type=page&limit=25&cursor=raNDoMsTRiNg"` (a relative path with no `/wiki`, which the adapter joins to the configured base) | same, operation description | DOC (example only; `_links.next` is not a typed schema field) |
| `ContentArray`: `results`, `start`, `limit`, `size`, `_links` | same, schema `ContentArray` | DOC |
| `Version`: `by` (User), `when` (date-time, **nullable**), `number`, `message`, `minorEdit` | same, schema `Version` | DOC |
| `User`: `type`, `accountId`, `displayName`, `publicName` | same, schema `User` | DOC |
| `ContentHistory`: `latest`, `createdDate`, `createdBy` (User) | same, schema `ContentHistory` | DOC |
| `Content.container`: "Container for content. This can be either a space (containing a page or blogpost) or a page/blog post (containing an attachment or comment)." | same, schema `Container` | DOC (the expand text says "`container` returns the space that the content is in", so the adapter falls back when the container is a space) |
| Expand values `version`, `history`, `container`, `space`, `body.storage` | same file, but read from the parameter `contentExpandWithSubExpandLimit`. This operation uses `contentExpand`, whose list was not read. | UNVERIFIED for this operation (every value is listed in the sibling parameter) |
| Inside a comment's `container`: `id`, `type`, `title`, and `history.createdBy` (used for the `my-page` tag) | the docs list no `container.*` expand form, so these keys come from memory | UNVERIFIED (when they are missing, `ref` falls back to the comment id and `my-page` is not set) |
| `Content._links.webui` | the same schema has `_links` as an untyped `GenericLinks` | UNVERIFIED (when it is missing, the adapter leaves `url` empty) |
| CQL field `lastmodified`, with operators `= != > >= < <=` and date formats `"yyyy/MM/dd HH:mm"`, `"yyyy-MM-dd HH:mm"`, `"yyyy/MM/dd"`, `"yyyy-MM-dd"` | https://developer.atlassian.com/cloud/confluence/cql-fields/ | DOC |
| "The search results will be relative to your configured time zone (which is by default the Confluence server's time zone)" | https://developer.atlassian.com/cloud/confluence/cql-fields/ | DOC (hence the adapter's `timeZone` and `overlapSec` settings) |
| `space in ("3C", "4C")`, `type` values `page`, `blogpost`, `comment`, `attachment` | https://developer.atlassian.com/cloud/confluence/advanced-searching-using-cql/ | DOC |
| `ORDER BY lastmodified` | the same page: "Not all fields support Ordering." Its examples order only by `created` and `title`. | UNVERIFIED (the adapter sends it, but its cursor never depends on server order) |
| Storage-format mention `<ri:user ri:account-id="..."/>` | Confluence storage format | UNVERIFIED (from memory; the adapter only reads it to add the `mention` tag) |
| Data Center: `GET /rest/api/content/search?cql=...` | https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/ | DOC for the path. DC pagination is UNVERIFIED. |
| `Retry-After` on 429, `X-RateLimit-Remaining` | https://developer.atlassian.com/cloud/confluence/rate-limiting/ | UNVERIFIED (the page was not fetched for this build; the adapter treats both headers as optional) |
| Max `limit` | swagger says only "this may be restricted by fixed system limits" | UNVERIFIED (the adapter asks for 50 per page by default) |

## Files

| File | Used for |
|---|---|
| `poll1-page1.json`, `poll1-page2.json` | first poll over two pages: page created, page edited on my page, comment that mentions me, my own blog post, a row with `version.when: null` |
| `poll2.json` | second poll: a row already seen at the cursor instant, a new row at the same instant, a new comment |
| `truncated-unordered.json`, `truncated-ordered.json` | `maxEventsPerPoll` stops the poll with a `next` link unread |
| `next-other-host.json` | a `next` link that names another host (`cdn.acme.example.com`) |
| `overlap-page1.json`, `overlap-page2.json` | page 1 holds only rows older than the cursor (the overlap re-read), page 2 the one new change |
| `resume-page2.json` | the page behind the `next` link of `truncated-*.json` |
| `stale-cursor.json` | 400 for a saved `next` cursor the server no longer takes |
| `empty.json` | no changes |
| `rate-limited.json`, `server-error.json` | 429 with `Retry-After`, and a 503 |
