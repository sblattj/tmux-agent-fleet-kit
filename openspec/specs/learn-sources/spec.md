# learn-sources Specification

## Purpose

LEARN rebuilds from the real sources (Q702 a): git hosts, trackers and call decisions, read through WATCH's adapter
registry with one shared config whose credentials are references only.

## Requirements

### Requirement: One shared sources config, references only
LEARN SHALL read `$FLEET_STATE/sources.json`, else `watch.json`. WATCH SHALL read `sources.json` when it has no
`watch.json` of its own, so one file names the sources for both desks. A credential SHALL be an `env:`, `keychain:` or
`cmd:` reference; a literal SHALL be refused, and `fleet learn sources` SHALL exit 1 on one. With no config, LEARN
polls nothing.

#### Scenario: Literal token refused
- **WHEN** a source has `"token": "abc123"`
- **THEN** `fleet learn sources` exits 1 naming the source and the poll does not run

#### Scenario: No config
- **WHEN** neither file exists
- **THEN** sync and rebuild read only the local files and make no network call

### Requirement: Adapters through WATCH's registry
Sources SHALL be polled through WATCH's adapter registry, which SHALL include `whosaid` (call decisions from a local
workspace, through the `whosaid` CLI, bounded by a timeout) and `linear` (GraphQL, read-only, paged by cursor, with
`retry_after` honoured). Every polled event SHALL go through WATCH's own `normalize`, `has_secret` and `drop`
filters (`match_drop`), then `cap`: an event with a secret, or one a drop filter matches, SHALL be dropped and counted.
The whosaid adapter SHALL always pass `--engine local`, so a workspace whose `whosaid.toml` names a cloud summarizer
never sends a transcript off the machine; the test fake SHALL refuse any other engine.

#### Scenario: Local engine only
- **WHEN** the whosaid adapter runs `whosaid decisions`
- **THEN** the argv carries `--engine local`, and the fake exits non-zero without it

### Requirement: Each source is drained
A sync or rebuild SHALL poll each source again from its new cursor until the source has nothing more, at most 50
polls. The whosaid adapter SHALL stop a poll only after a whole meeting, so no decision is cut by a per-poll cap. A
rebuild that hits the bound SHALL fail closed; a sync SHALL report the source as drain-capped and resume next time.

#### Scenario: More meetings than one poll takes
- **WHEN** the workspace holds 60 meetings and a poll reads at most 50
- **THEN** one rebuild brings rows for all 60, and the next sync adds none

#### Scenario: whosaid decisions arrive heard
- **WHEN** the whosaid adapter reports a decision from a call
- **THEN** its row is `heard`, its lesson line reads "not decided yet", and it gets no cards

#### Scenario: Linear links
- **WHEN** a Linear issue has a project and a blocking issue
- **THEN** its entity has part-of the project and blocked-by the blocker

### Requirement: Tests use fakes only
The adapter tests SHALL use recorded fixtures and fake transports: no live network, no real org, repo or person
names. `docs/learn-sources.example.json` SHALL be generic and refs-only.

#### Scenario: Example config validates
- **WHEN** the example config is checked
- **THEN** it passes and names only example hosts
