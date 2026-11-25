# learn-session Specification

## Purpose

One command takes the human through a topic: the lesson, then a quiz on only that topic's cards, then a one-line
result. The first exposure is never graded as learned.

## Requirements

### Requirement: Session runs lesson, then topic quiz, then result
`fleet learn session --topic T` SHALL print T's lesson (recording seen), run the quiz on only the cards whose rows
are on T, and end with one line `session T: N lesson(s), M card(s), P pass, F fail, K learned`.

#### Scenario: Session on a new topic
- **WHEN** the human runs a session on a topic with one unseen confirmed row and answers both cards right
- **THEN** the output shows the lesson, then two quiz cards, then `session T: 1 lesson(s), 2 card(s), 2 pass, 0 fail, 0 learned`

#### Scenario: Other topics are not quizzed
- **WHEN** another topic has due cards
- **THEN** the session asks none of them

### Requirement: First exposure is never graded as learned
Passes in the same session as a row's first lesson SHALL be recorded but SHALL NOT make the row learned.

#### Scenario: Learned count stays zero on first exposure
- **WHEN** every card of a new row passes in its first session
- **THEN** the result line says `0 learned` and proof shows the row as not learned

### Requirement: Session is human only
`fleet learn session` SHALL be refused with exit 4 under any `FLEET_ROLE`.

#### Scenario: Role refused
- **WHEN** `FLEET_ROLE=learn fleet learn session --topic T` runs
- **THEN** it exits 4 and writes nothing
