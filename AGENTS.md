# Working on this repo

- Issue first: every change gets an issue on this repo before the commit, and the commit references it (`fixes #N` or `refs #N`). Scrub it: no employer or customer names, people, ticket ids, account ids, hostnames, URLs or local paths. See README "Contributing: issue first".
- A change that landed without an issue gets a backfilled one, closed with a link to its commit.
- Run the tests the change touches, and `test/test_privacy.sh`, before landing.
