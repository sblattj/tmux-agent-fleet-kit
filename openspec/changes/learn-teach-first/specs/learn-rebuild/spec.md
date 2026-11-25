# Spec Delta

## Purpose

Everything LEARN holds is re-derivable from its sources; only the human's own results (reviews and seen records)
are kept as the record. One command regenerates the rest.

## ADDED Requirements

### Requirement: Inputs journal
Every row LEARN gets from a message rather than a source file (`add`, `confirm`, `drop`, `curate`, `entity add`,
`sync --goal`) SHALL be appended to `learn/inputs.jsonl` with its arguments, keyed by source rather than row id.

#### Scenario: Add is journaled
- **WHEN** `fleet learn add --kind done --origin fleet --source route-done:lane-a --quote "..."` runs
- **THEN** `inputs.jsonl` gains one row with verb `add` and those arguments

### Requirement: Rebuild regenerates derived state from sources
`fleet learn rebuild` SHALL regenerate `ledger.jsonl`, `entities.jsonl`, `cards.jsonl`, `proof.json` and the sync
cursors from the decisions log, WATCH events, achieved goal files and the inputs journal, and print counts.

#### Scenario: Rebuild after the derived files are deleted
- **WHEN** `ledger.jsonl`, `cards.jsonl`, `entities.jsonl`, `proof.json` and `cursors.json` are deleted and
  `fleet learn rebuild` runs
- **THEN** the live rows (by source key, with what and why), entities, due cards and proof equal those before

### Requirement: Human results re-attach by source key
Reviews and seen records SHALL carry the row's source key; rebuild SHALL re-attach them to the rebuilt rows and
cards, replay the reviews through the scheduler to restore each card's schedule, and report orphans whose source is
gone.

#### Scenario: Proof survives a rebuild
- **WHEN** a row was learned before rebuild
- **THEN** it is learned after rebuild and its cards have the same due times

#### Scenario: Orphaned review
- **WHEN** a reviewed row's source is gone at rebuild
- **THEN** the review stays in `reviews.jsonl` and rebuild prints `orphaned 1`

### Requirement: Rebuild is human or LEARN only and never touches another state dir
`fleet learn rebuild` SHALL write only under `$FLEET_STATE/learn/`, keep the previous derived files in
`learn/rebuild-prev/`, and SHALL be refused under any role other than `learn`.

#### Scenario: Worker role refused
- **WHEN** `FLEET_ROLE=worker fleet learn rebuild` runs
- **THEN** it exits 4 and nothing changes
