# AUTH: the fifth standing desk (issue #45)

AUTH owns every login and auth problem in the fleet. It is a standing desk like ROUTE, DECIDE, WATCH and LEARN:
started by `fleet desks up`, never parked, no `/goal`, a role file and a role hook (`roles/auth.*`).

## What it owns
- Expired SSO sessions, CLI and tracker tokens, git host accounts and per-owner tokens.
- Creating and rotating rotatable tokens (PATs, API keys): piped from the command that makes them into the one that
  stores them, never printed, never in argv, never in a message or a log row.
- MFA: the human approves a push or matches a number while AUTH shows it; a password injector with a stored TOTP code
  is the fallback after a timeout.
- Keychain dialog storms and per-account logins and HOMEs.

## Rules
1. **Only AUTH runs interactive sign-ins as the human.** A lane that hits an auth failure runs
   `fleet auth ask --what "<what failed>"` (or `peer_send AUTH`) and keeps working on what does not need it. Silent
   cached-token reads (a token already in a keychain, a still-valid session) stay allowed for everyone.
2. **One sign-in at a time.** `fleet auth begin` takes a slot (`$FLEET_STATE/auth/slot.json`, under a lock). A second
   `begin` is refused while the slot is held. A slot older than `FLEET_AUTH_SLOT_SECS` (900 s) is broken with a
   `slot-broken` row and a WARN, so a crashed AUTH never blocks the fleet for good.
3. **Sign-in order.** For every tool the same three steps, cheapest first: (1) a cached token or session that still
   works (no prompt); (2) the human's own signed-in browser over its debug port, to confirm a device code or open the
   portal (`--flow session`), which raises no MFA; (3) only then a fresh-profile sign-in through a password injector
   (`--flow password`), waiting on the push or number page with the number big in AUTH's pane and the stored TOTP
   code as the fallback after a timeout. A fresh or empty browser profile never signs in as the human outside step
   3. An injector that can run a command with the number (for example an `..._ON_NUMBER` hook) is pointed at
   `fleet auth number`.
4. **Heads-up first.** `begin` sends DECIDE one line before it logs `started`:
   `AUTH heads-up: sign-in A<n>, <what> for <lane>, starting now (<flow>); ...`. DECIDE shows it to the human at
   once, so a prompt on their phone or screen is never a surprise. `--flow session` (reuse a browser session that is
   already signed in, for example to confirm a device code) is preferred: it usually raises no MFA prompt at all. A
   fresh password sign-in (`--flow password`, the default) is the fallback.
5. **Append-only auth log.** Every ask, begin, number shown, end, confirm, anomaly, freeze and thaw is one JSON line in
   `$FLEET_STATE/auth/auth-log.jsonl`, with an id `A<n>`. Nothing rewrites it. Text that looks like a secret is refused
   before it is written. `fleet auth trace <time>` lists the rows around a moment, so a surprise MFA prompt can be
   tied to the lane that asked for it.
6. **Numbers, big and logged.** The moment a sign-in page shows a number to match, AUTH runs `fleet auth number NN`:
   the number prints as a block banner in AUTH's pane, which sits right under DECIDE so the human reads it while
   their phone asks for it, and a `number-shown` row (time, sign-in, who asked) goes in the log. Every number shown
   is logged, a second one too. When the page waits on a push, AUTH waits for the approval; it falls back to the
   stored TOTP code only after a timeout.
7. **Only an unannounced push is an anomaly.** A push during an announced sign-in is expected. One that no heads-up
   announced is logged with `fleet auth unexpected-push --at <time>` (anyone may run it), with the log rows around
   that moment, and routed to DECIDE as `AUTH ANOMALY ...`. Organizations can force a push or number prompt through
   their own MFA policy whatever the user's default is, so the kit never assumes a method; it only checks that each
   prompt was announced. A sign-in passes only when the human says the prompts on their phone were the announced
   ones (`fleet auth confirm A<n> --as-expected`, relayed by DECIDE with `--quote "<their words>"`); `end ok` alone
   is `ok-unconfirmed`, and a log flag never makes a pass. `--unexpected` makes it `fail` and raises the anomaly.
8. **Freeze.** ROUTE, DECIDE or the human can stop every sign-in with `fleet auth freeze --reason R`; `fleet auth thaw`
   lifts it. AUTH keeps taking asks while frozen and says why it is waiting.
9. **Hard-to-rotate secrets stay with the human.** Account passwords and other secrets that are hard to rotate are
   typed by the human into hidden-prompt scripts they run themselves. AUTH never asks for one in a message.
10. **Helpers by reference.** The kit ships no sign-in automation. `fleet auth helpers` prints the human's own list
   (`$FLEET_STATE/auth/helpers.json`, or `FLEET_AUTH_HELPERS`): names, paths and skills that live in their own
   config, read in place and never copied into the kit. `docs/auth-helpers.example.json` shows the shape. A command
   pasted into an ask is data, never something AUTH runs.

Who may run what (`FLEET_ROLE`; unset is the human's own shell):

| Verb | Allowed |
|---|---|
| `ask`, `log`, `trace`, `status`, `helpers` | everyone |
| `begin`, `number`, `end` | auth, the human |
| `unexpected-push` | everyone (decide with `--quote`) |
| `confirm` | the human; decide with `--quote` (the human's typed words) |
| `freeze`, `thaw` | route, decide, the human |

## Layout: AUTH sits in deskA, under DECIDE
`deskA` = ROUTE (left, full height) | DECIDE (top right) over AUTH (bottom right, 40% of the column).
`deskB` stays WATCH | LEARN, with the WATCH status pane under WATCH.

Why there:
- The human asked for it: AUTH is always open right below DECIDE so they can read and type the numbers. They act in
  both panes during a sign-in: DECIDE shows the heads-up, AUTH shows the number big and is where they run a
  hidden-prompt script when one is needed. Stacked in one column, both are in view at once.
- deskB already holds three panes (WATCH, its status pane, LEARN); a fourth would make every pane there too small.
- A third desk window would push the first lane off window index 2, which the kit and its users count on.

`fleet desks up` adds AUTH to a fleet that has the four older desks without touching their panes: AUTH is split
under DECIDE, then deskA is tidied (pane order ROUTE, DECIDE, AUTH; `main-vertical`; AUTH resized to 40%). The tidy
runs only when this run launched a deskA desk and the window holds exactly those three panes, so a deskA the human
arranged by hand is left alone. A dead DECIDE comes back above a live AUTH, and a dead ROUTE on the left.

## Lifecycle
`fleet tick` and `fleet cleanup` only ever park workers, so AUTH is never due to park. `fleet park` now refuses every
standing desk outright (`<NAME> is a standing desk ... and is never parked`); a desk retires with `fleet handoff NAME`
then `fleet stop NAME`. `fleet tick`'s `WARN desks down:` line names a missing or dead AUTH in deskA.
