# Spec Delta

## Purpose

LEARN rebuilds from the real sources (Q702 a): git hosts, trackers and call decisions, read through WATCH's adapter
registry with one shared config whose credentials are references only.

## ADDED Requirements

### Requirement: One shared sources config, references only
LEARN SHALL read `$FLEET_STATE/sources.json`, else `watch.json`. A credential SHALL be an `env:`, `keychain:` or
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
`retry_after` honoured). An event whose text looks like a secret SHALL be dropped.

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
