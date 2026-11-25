# Spec Delta

## Purpose

A new ledger row is due as a lesson first; its quiz cards come due only after the human has seen that lesson, and
the proof clock starts at that first look.

## ADDED Requirements

### Requirement: Cards are due only after their lesson was seen
`due()`, `fleet learn due`, `fleet learn quiz` and the page quiz SHALL list a card only when its row has a seen
record and the card's scheduled due time has passed.

#### Scenario: New row has no due cards
- **WHEN** a confirmed row is added and no lesson has been shown
- **THEN** `fleet learn due` prints `0 due` and `fleet learn lessons` lists the row

#### Scenario: Seen row's cards come due
- **WHEN** the human runs `fleet learn lesson` for that row
- **THEN** `fleet learn due` lists its cards

### Requirement: Lessons due are listed
`fleet learn lessons [--topic T] [--json]` SHALL list teachable live rows with no seen record, by topic, and
`fleet learn quiz` with nothing due SHALL say how many lessons are waiting.

#### Scenario: Quiz points at waiting lessons
- **WHEN** `fleet learn quiz` runs with no due cards and 2 unseen rows
- **THEN** it prints `2 lesson(s) waiting` and the `fleet learn lesson` command

### Requirement: First exposure starts the proof clock
A row's first exposure SHALL be the earlier of its first seen record and its first review. Learned still needs a
generative pass and a checkable pass with the latest pass at least 24 h after first exposure.

#### Scenario: Same-session pass is not learned
- **WHEN** a row is seen and both cards pass within the same hour
- **THEN** proof shows `learned: false` and `firstSeen` equals the seen time

#### Scenario: Pass 25 h after the lesson is learned
- **WHEN** the cards pass 25 h after the seen time with no fail since
- **THEN** proof shows `learned: true`

### Requirement: Proof cache stays equal to a rebuild
Writing a seen record SHALL refresh `proof.json`, so `fleet learn proof --check` passes after a lesson.

#### Scenario: Check after lesson
- **WHEN** a lesson is shown and `fleet learn proof --check` runs
- **THEN** it prints `OK proof.json matches a rebuild`
