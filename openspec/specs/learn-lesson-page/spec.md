# learn-lesson-page Specification

## Purpose

The LEARN page is a face on the same verbs: a Lesson tab shows the topic's tree, terms and context, and one button
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
