# Spec Delta

## Purpose

LEARN teaches a ledger row before it quizzes it: a lesson shows the terms first, then each row as an example of
the term in context, every line backed by a verbatim quote and its source.

## ADDED Requirements

### Requirement: Lesson shape is terms first, then examples in context
A lesson for a set of rows SHALL list a Terms part first (each work subject or entity the rows are about, with its
definition) and an In context part second (each row, its verbatim quote and the source it came from).

#### Scenario: Topic lesson prints terms before context
- **WHEN** the human runs `fleet learn lesson --topic acme/widget#7` with two unseen rows on that PR
- **THEN** the output has a `Terms` heading naming `acme/widget#7` before an `In context` heading listing both rows

### Requirement: Every lesson line carries a verbatim quote and its source
Each term, context, what and why line SHALL show a quote that is a verbatim (whitespace and case normalized)
substring of a source quote, and the source as `type:ref`.

#### Scenario: Context line shows quote and source
- **WHEN** a row from `decisions-log:Q12` with quote `a: cap at 3, jittered` is in a lesson
- **THEN** its line shows `"a: cap at 3, jittered"` and `[decisions-log:Q12]`

### Requirement: Lesson lines are verbatim from a source by default (Q696)
LEARN SHALL write a term's definition and a row's example from verbatim source quotes, with no model rewording by
default. A row's `what` or `why` line SHALL appear only when it is a normalized substring of the row's quote;
otherwise it SHALL be dropped and counted.

#### Scenario: Line without a source quote never appears
- **WHEN** a row's `what` is `Uploads now use a CDN` and its quote is `a: cap at 3, jittered`
- **THEN** the lesson omits that line and reports `1 line dropped (not backed by its quote)`

#### Scenario: Reworded line dropped by default
- **WHEN** a row's `what` is `Retries are capped | quote: cap at 3` and rewording is not allowed
- **THEN** the lesson omits that line

### Requirement: Rewording is opt-in and still quote-backed
With `learn.json` `{"lesson": {"allowRewording": true}}`, a reworded line SHALL be shown only when it carries `| quote: <words>`
and those words are a normalized substring of the row's quote.

#### Scenario: Reworded line kept when allowed
- **WHEN** the same row is taught with `allowRewording: true`
- **THEN** the lesson shows `what: Retries are capped` with `"cap at 3"` and the row's source

#### Scenario: Verbatim what line kept
- **WHEN** a row's `what` is `cap at 3` and its quote is `a: cap at 3, jittered`
- **THEN** the lesson shows `what: cap at 3` with the row's source

### Requirement: Heard rows show as not decided yet
A row with status `heard` SHALL appear in a lesson marked `not decided yet`, never as a decision.

#### Scenario: Heard row in a lesson
- **WHEN** a heard row is in a lesson
- **THEN** its line contains `not decided yet` and it gets no quiz card

### Requirement: Lesson records the rows as seen
`fleet learn lesson` SHALL append one seen record per shown row to `learn/seen.jsonl` with the time and the row's
source key, and print which rows were marked seen. `--preview` SHALL show the same lesson and record nothing.

#### Scenario: Lesson marks seen
- **WHEN** the human runs `fleet learn lesson --topic T` with one unseen row on T
- **THEN** `seen.jsonl` gains one row for it and a second run prints `no new lessons`

#### Scenario: Preview records nothing
- **WHEN** `fleet learn lesson --preview` runs
- **THEN** `seen.jsonl` is unchanged

### Requirement: Only the human takes lessons and quizzes
Recording seen (`lesson`, `session`, `digest`, the page's seen call), `quiz` and `review` SHALL be refused with exit 4
when `FLEET_ROLE` is set. `--preview` and `brief` stay open to any role.

#### Scenario: LEARN role cannot mark seen
- **WHEN** `FLEET_ROLE=learn fleet learn lesson` runs
- **THEN** it exits 4 with `FAIL role learn can't` and writes nothing

#### Scenario: LEARN role cannot review
- **WHEN** `FLEET_ROLE=learn fleet learn review C1 --rating good` runs
- **THEN** it exits 4 and `reviews.jsonl` is unchanged

### Requirement: LEARN curates a row's what and why
`fleet learn curate <lid> [--what W] [--why Y] [--part-of E]` SHALL append a new version of the row with those
fields, journaled as an input so a rebuild keeps it.

#### Scenario: Curate adds what
- **WHEN** `fleet learn curate L1 --what "cap at 3"` runs
- **THEN** `fleet learn show L1 --json` has that `what` and the next lesson shows it

### Requirement: Ledger kind lesson is renamed done
The ledger kind for a goal done line SHALL be `done`. `--kind lesson` SHALL be refused with a message naming `done`.

#### Scenario: Old kind refused
- **WHEN** `fleet learn add --kind lesson ...` runs
- **THEN** it exits 1 and the message names `done`
