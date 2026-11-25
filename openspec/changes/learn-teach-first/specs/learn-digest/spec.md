# Spec Delta

## Purpose

The human catches up on what happened since they last looked: a digest teaches the rows newer than a cursor.

## ADDED Requirements

### Requirement: Digest teaches rows since a time
`fleet learn digest [--since WHEN]` SHALL show the lesson for live rows created after WHEN (ISO time, `Nd` or `Nh`),
defaulting to the `digest` cursor in `cursors.json`, else 7 days, and record them as seen.

#### Scenario: Since two days
- **WHEN** `digest --since 2d` runs with one row from yesterday and one from last week
- **THEN** only yesterday's row is in the digest

### Requirement: Digest advances its cursor
After a digest the `digest` cursor SHALL be the digest time; `--preview` SHALL leave the cursor and seen unchanged.

#### Scenario: Second digest is empty
- **WHEN** digest runs twice with no new rows between
- **THEN** the second prints `nothing new since` and the cursor time

### Requirement: Digest is human only unless preview
`fleet learn digest` without `--preview` SHALL be refused with exit 4 under any `FLEET_ROLE`.

#### Scenario: Role preview allowed
- **WHEN** `FLEET_ROLE=learn fleet learn digest --preview` runs
- **THEN** it exits 0 and changes nothing
