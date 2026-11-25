# Spec Delta

## Purpose

Work subjects (repos, PRs, goals, features) form a tree. Lessons and briefs roll a topic up to its subtree, and
the pane shows that tree as text.

## ADDED Requirements

### Requirement: Entities with typed edges
`fleet learn entity add <id> --type T --definition D --quote Q --source S [--part-of P] [--ships-in X]
[--blocked-by B] [--decided-by Q] [--owner O]` SHALL record an entity in `entities.jsonl`. Edge types are only
part-of, ships-in, blocked-by, decided-by and owner. The definition SHALL be backed by the quote, as for what lines.

#### Scenario: Add and list
- **WHEN** `fleet learn entity add acme/widget --type repo --definition "the widget service" --quote "the widget service owns uploads" --source git:acme/widget` runs
- **THEN** `fleet learn entity list` shows `acme/widget` with type `repo`

#### Scenario: Unknown edge refused
- **WHEN** `--edge likes:X` or an unknown edge type is given
- **THEN** it exits 1 naming the five edge types

### Requirement: Entities are derived plus journaled
Entities named by row topics SHALL be derived at sync and rebuild; entities from `entity add` SHALL be journaled in
`inputs.jsonl`, so `entities.jsonl` is always regenerable.

#### Scenario: Topic becomes an entity
- **WHEN** a row has topic `acme/widget#7` and no entity was added
- **THEN** after sync, `entity list` shows `acme/widget#7` with type `pr` and part-of `acme/widget`

### Requirement: Topic rolls up to its subtree
A topic filter in lesson, session, brief and due SHALL match rows on the topic or on any entity whose part-of chain
reaches it.

#### Scenario: Repo topic includes its PRs
- **WHEN** a lesson runs for `--topic acme/widget` and a row is on `acme/widget#7`
- **THEN** that row is in the lesson

### Requirement: Text tree
`fleet learn entity tree [ROOT]` SHALL print the part-of tree with indentation and each node's row count.

#### Scenario: Tree output
- **WHEN** `acme/widget#7` is part of `acme/widget`
- **THEN** `entity tree` prints `acme/widget` with `  acme/widget#7` indented under it
