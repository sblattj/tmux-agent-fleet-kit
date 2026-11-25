# learn-desk-role Specification

## Purpose

The LEARN role text tells the desk to teach before it tests, curate what and why from sources, and keep its hands
off the human's lessons and grades.

## Requirements

### Requirement: Role names the teach-first flow
`roles/learn.md` SHALL say that new rows are taught as lessons before their cards come due, and name `lesson`,
`session`, `digest`, `brief`, `curate`, `entity` and `rebuild`.

#### Scenario: Role text lists the verbs
- **WHEN** a test greps `roles/learn.md`
- **THEN** each of those verbs appears

### Requirement: Role forbids taking lessons or grading
The role SHALL say LEARN never marks lessons seen, never takes the quiz and never writes reviews; those are the
human's.

#### Scenario: Role text forbids review
- **WHEN** a test greps `roles/learn.md`
- **THEN** it does not list `review` among LEARN's write verbs and says LEARN never marks seen

### Requirement: Role keeps everything re-derivable
The role SHALL say LEARN writes only through verbs that journal or derive, so `fleet learn rebuild` can regenerate
all of its state.

#### Scenario: Role mentions rebuild
- **WHEN** a test greps `roles/learn.md`
- **THEN** it names `fleet learn rebuild` and re-derivable
