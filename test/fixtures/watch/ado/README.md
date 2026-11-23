# Azure DevOps fixtures

Hand-written from Microsoft's public REST and WIQL docs. Nothing here was recorded from a live server. Names, ids and
hosts are made up: organization/collection `ACME`, project `PROJ` (work item 1 is `PROJ-1`), host `acme.example.com`
(Azure DevOps Server style base `https://acme.example.com/tfs/ACME`).

Each `pollN-*.json` file is one HTTP exchange: `request` (the exact method, URL and JSON body the adapter must send),
then `status`, `headers` and `body` (the answer). `test/test_watch_ado.py` replays them in order and fails if the
adapter sends anything else.

| File | What it shows |
|---|---|
| `poll1-wiql.json`, `poll1-batch.json` | first poll, no cursor: WIQL returns ids 1, 2, 3; the batch get returns their fields |
| `poll2-wiql.json`, `poll2-batch.json` | second poll from the cursor: PROJ-3 again at the same rev (skipped), PROJ-2 Active -> Resolved, PROJ-1 reassigned and commented |
| `poll3-wiql.json` | nothing changed: the cursor stays put |
| `poll4-batch.json` | catch-up poll after a service hook already delivered PROJ-2 rev 7 |
| `throttled-429.json` | 429 with `Retry-After` and `X-RateLimit-*` headers |
| `delayed-200-retry-after.json` | a 200 that still carries `Retry-After` |
| `signin-203.json` | a bad PAT answered with a 203 HTML sign-in page |
| `hook-workitem-updated.json` | a `workitem.updated` service hook body |

## Sources

- WIQL REST call (POST `.../_apis/wit/wiql`, `timePrecision` and `$top` parameters, response `workItems[{id, url}]`):
  https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/wiql/query-by-wiql?view=azure-devops-rest-7.1
- WIQL syntax ("The API only returns work item IDs"; DateTime literals are in the local time zone unless one is given;
  ISO 8601 with `Z` is valid in any locale; `@today` is local midnight):
  https://learn.microsoft.com/en-us/azure/devops/boards/queries/wiql-syntax
- Get Work Items Batch (POST `.../_apis/wit/workitemsbatch`, body `ids`, `fields`, `errorPolicy`, at most 200 ids,
  response `{count, value[{id, rev, fields, url}]}`):
  https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/work-items/get-work-items-batch?view=azure-devops-rest-7.1
- Rate limits (429, `Retry-After` in seconds, `X-RateLimit-Resource|Delay|Limit|Remaining|Reset`, `Retry-After` can
  come on a 200): https://learn.microsoft.com/en-us/azure/devops/integrate/concepts/rate-limits
- PAT in the Authorization header (Basic, base64 of `:` + PAT):
  https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate
- Service hook webhooks (basic auth over HTTPS; no HMAC signature is documented):
  https://learn.microsoft.com/en-us/azure/devops/service-hooks/services/webhooks
- Service hook events (`workitem.updated`, publisher `tfs`, `resource`, `resourceVersion`):
  https://learn.microsoft.com/en-us/azure/devops/service-hooks/events
- Work item field reference (System.ChangedDate, System.CommentCount, System.History):
  https://learn.microsoft.com/en-us/azure/devops/boards/work-items/guidance/work-item-field

## Not in the docs (UNVERIFIED)

- The shape of identity fields (`System.AssignedTo`, `System.ChangedBy`) in a batch answer: the batch sample shows
  none. These fixtures use the IdentityRef object form `{displayName, uniqueName, id}`; the adapter also takes the
  older string form `Name <unique>`, which the service hook sample uses.
- The 203 sign-in page for a bad PAT is widely reported but not on the pages above.
- `hook-workitem-updated.json` puts `{oldValue, newValue}` pairs in `resource.fields` and the full revision in
  `resource.revision.fields`. The events page sample checked for this build showed flat current values in
  `resource.fields`; the adapter accepts both, and the test covers both.
- The REST page names the parameter `timePrecision`; the syntax page talks about `dayPrecision`. The adapter sends
  `timePrecision=true`, as the REST page lists it.
