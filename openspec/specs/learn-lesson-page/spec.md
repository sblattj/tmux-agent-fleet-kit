# learn-lesson-page Specification

## Purpose

The LEARN page is a superset of the pane (Q698): a Lesson tab shows the topic's tree, terms and context, and one button
marks the lesson seen and starts that topic's quiz.

## Requirements

### Requirement: Lessons API
`GET /api/lessons[?topic=T]` SHALL return the same lesson model the pane prints (terms, context lines with quote and
source, dropped count, the entity tree) for unseen rows, or for all rows on T when T is given.

#### Scenario: Lessons JSON matches the pane
- **WHEN** the page fetches `/api/lessons?topic=T`
- **THEN** its context quotes equal those `fleet learn lesson --preview --topic T --json` returns

### Requirement: Seen API uses the same verb
`POST /api/seen {token, lids}` SHALL record seen through the same function the pane uses, with the page's token,
Host and Origin checks, and return the topic's now-due cards.

#### Scenario: Mark seen and quiz
- **WHEN** the page posts `/api/seen` for a topic's rows
- **THEN** `seen.jsonl` gains those rows and the response lists their due cards

#### Scenario: Missing token refused
- **WHEN** `/api/seen` is posted without the token
- **THEN** it answers 403 and nothing is written

### Requirement: Lesson tab renders a deterministic tree
The Lesson tab SHALL draw the topic's entity tree as SVG with positions computed from the tree alone, so the same
data draws the same picture, and insert text only as text nodes.

#### Scenario: Same data, same SVG
- **WHEN** the tree function runs twice on the same lesson JSON
- **THEN** the SVG strings are equal

### Requirement: The page is a superset of the pane (Q698)
Every `fleet learn` verb SHALL have a page route that does the same thing (lesson for any topic, mark seen under
the human-only guard, quiz, review, session on one topic, and every other verb through `POST /api/cli`, the same
`fleet_learn.main()` the pane runs), plus graphics. The pane SHALL keep a full text version of every verb.

#### Scenario: A new pane verb without a page route
- **WHEN** a verb is added to `fleet learn` with no page route, or the page never calls its route
- **THEN** `TestPaneParity` in test/test_learn_serve.py fails naming it

#### Scenario: Session in the page
- **WHEN** the human starts a session on topic T in the page
- **THEN** the page shows T's lesson, quizzes only T's cards after it is seen, and prints the pane's result line

