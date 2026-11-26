# learn-rebuild Specification

## Purpose

Everything LEARN holds is re-derivable from its sources; only the human's own results (reviews and seen records)
are kept as the record (Q703 b). One command regenerates the rest.

## Requirements

### Requirement: Rebuild regenerates derived state from sources
`fleet learn rebuild` SHALL regenerate `ledger.jsonl`, `entities.jsonl`, `cards.jsonl`, `proof.json` and the sync
cursors from the decisions log, WATCH events, achieved goal files and the configured sources (`sources.json`, else
`watch.json`), and print counts. Messages LEARN was handed (`add`, `confirm`, `drop`, `curate`) are not a source
and SHALL NOT be kept; ROUTE's done lines come back only from the lanes' goal files.

#### Scenario: Rebuild after the derived files are deleted
- **WHEN** `ledger.jsonl`, `cards.jsonl`, `entities.jsonl`, `proof.json` and `cursors.json` are deleted and
  `fleet learn rebuild` runs
- **THEN** the live rows (by source key), entity links, due cards and proof equal those before

#### Scenario: A manual row does not survive
- **WHEN** a row was added with `fleet learn add` and seen, then `fleet learn rebuild` runs
- **THEN** the row is gone and rebuild prints `orphaned 0 review(s), 1 seen`

### Requirement: Human results re-attach by source key
Reviews and seen records SHALL carry the row's source key and card key; rebuild SHALL re-attach them to the
rebuilt rows and cards, replay the reviews through the scheduler to restore each card's schedule, and report orphans
whose source is gone. `reviews.jsonl` and `seen.jsonl` SHALL be left byte-identical.

#### Scenario: Proof survives a rebuild
- **WHEN** a row was learned before rebuild
- **THEN** it is learned after rebuild, its cards have the same due times, and `fleet learn proof --check` passes

### Requirement: A failing source aborts the rebuild
Rebuild SHALL poll every source into memory before it moves any file. When a source fails it SHALL exit 1 with
`...; nothing changed`, leave every file byte-identical and create no `rebuild-prev/`.

#### Scenario: Linear is down
- **WHEN** a configured source raises during the poll
- **THEN** rebuild exits 1 and the learn directory is unchanged

### Requirement: Rebuild is human or LEARN only and never touches another state dir
`fleet learn rebuild` SHALL write only under `$FLEET_STATE/learn/`, keep the previous derived files in
`learn/rebuild-prev/`, and SHALL be refused under any role other than `learn`.

#### Scenario: Worker role refused
- **WHEN** `FLEET_ROLE=worker fleet learn rebuild` runs
- **THEN** it exits 4 and nothing changes
