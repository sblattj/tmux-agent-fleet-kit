# learn-entities Specification

## Purpose

Work subjects (repos, PRs, tickets, goals, features) form a graph with typed links (Q697 a). Lessons and briefs roll
a topic up to its subtree, and the pane shows the part-of tree as text. Every entity and link is rebuilt from the
sources.

## Requirements

### Requirement: Entities with typed links from the sources
`entities.jsonl` SHALL hold one entity per work subject with a type and links of only these types: part-of,
ships-in, blocked-by, decided-by and owner. Entities SHALL be derived at sync and rebuild from row topics and from
the links the adapters report (a Linear issue's project and blockers, a PR's repo, a call decision's meeting).

#### Scenario: Topic becomes an entity
- **WHEN** a row has topic `acme/widget#7`
- **THEN** after sync, `fleet learn entity list` shows `acme/widget#7` with type `pr` and part-of `acme/widget`

#### Scenario: Links come back after a rebuild
- **WHEN** the derived files are deleted and `fleet learn rebuild` runs
- **THEN** `fleet learn entity list --json` equals the list before

### Requirement: Curated part-of
`fleet learn curate <lid> --part-of E` SHALL place a row's topic under E. Curation is a message, not a source, so a
rebuild drops it.

#### Scenario: Curate then rebuild
- **WHEN** a row is curated with `--part-of` and `fleet learn rebuild` runs
- **THEN** the curated link is gone

### Requirement: Topic rolls up to its subtree
A topic filter in lesson, session, brief and due SHALL match rows on the topic or on any entity whose part-of chain
reaches it.

#### Scenario: Repo topic includes its PRs
- **WHEN** a lesson runs for `--topic acme/widget` and a row is on `acme/widget#7`
- **THEN** that row is in the lesson

### Requirement: Text tree
`fleet learn entity tree [ROOT]` SHALL print the part-of tree with indentation, each node with its type.

#### Scenario: Tree output
- **WHEN** `acme/widget#7` is part of `acme/widget`
- **THEN** `entity tree` prints `acme/widget (repo)` with `  acme/widget#7` indented under it
