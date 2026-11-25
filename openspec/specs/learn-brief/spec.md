# learn-brief Specification

## Purpose

Any desk or lane can ask LEARN for a short grounded brief about a subject, through a pluggable selector, with a
fallback that needs no plugin.

## Requirements

### Requirement: Brief prints grounded lesson lines for a subject
`fleet learn brief --for X [--max N] [--json]` SHALL print the ancestor chain of X, then up to N rows as lesson
lines (quote and source each). It SHALL write nothing and be open to any role.

#### Scenario: Brief under a worker role
- **WHEN** `FLEET_ROLE=worker fleet learn brief --for acme/widget#7` runs
- **THEN** it exits 0, prints the rows on that PR, and `seen.jsonl` is unchanged

### Requirement: Pluggable selector
When `learn.json` has `brief.selector` (a shell command), brief SHALL send it JSON `{for, max, rows}` on stdin under
a timeout (`brief.timeoutSec`, default 10) and use the row ids it prints, one per line, keeping only known ids in
its order.

#### Scenario: Selector picks rows
- **WHEN** the selector prints `L2` then `L9` and only `L2` exists
- **THEN** the brief shows only `L2`

#### Scenario: Selector fails
- **WHEN** the selector exits non-zero or times out
- **THEN** the brief uses the fallback and prints `selector failed, fallback used`

### Requirement: No-plugin fallback
With no selector, brief SHALL pick rows on X or its subtree, newest first; when none match, the N most recent live
rows, marked `no rows on X, most recent shown`.

#### Scenario: Unknown subject
- **WHEN** `brief --for nothing-here` runs
- **THEN** it prints `no rows on nothing-here, most recent shown` and the newest rows
