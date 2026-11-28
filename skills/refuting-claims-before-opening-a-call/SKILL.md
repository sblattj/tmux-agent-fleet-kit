---
name: refuting-claims-before-opening-a-call
description: "Before decision_open on a call that asserts facts, have one read-only subagent try to refute each claim with a command or a quote, then open with verified[]; a refuted claim means fix the call, since decision_open refuses it. Triggers on: open a decision, ask the human, decision_open, claims lint flagged my options, refused: claim(s) refuted, verified field, check my premises before asking, is this call still true."
---

# refuting-claims-before-opening-a-call

A call that rests on a false premise wastes the human's answer. You have the context and the sources, so check your own claims before the call goes out, not a day later.

## When
Only when the call asserts facts: the claims lint flags an option (it says "no X", "only", "already", "one step", or names a count), or you wrote `claims` on the call. Pure preference calls ("A or B, which do you like") skip this.

## Do this
1. Write the call's `claims`: `[{id: "c1", text, source, options: [letters], refs}]`.
2. Spawn **one read-only subagent** (Claude Code: an `Explore` or `lean` agent told not to edit; other CLIs: do it yourself, read-only). Give it the claim list and this brief:
   > Try to refute each claim. For each, run one command or find one quote that would show it false. Report per claim: `claim` (its id), `how` (the exact command or quote, with file:line or URL), `result`: `refuted` if the evidence contradicts it, `held` if the evidence supports it, `unchecked` if you could not check it. Do not edit anything.
3. Any `refuted`: fix the call (correct or drop the claim, reword the options) and run the check again on what changed. Do not open it as is; `decision_open` refuses it with `refused: N claim(s) refuted ...` and nothing reaches the desk.
4. Open with the results: `decision_open {question, options, claims, verified: [{claim: "c1", how, result}]}`. Anything left `unchecked` shows on the human's card as `unchecked: <text>`, so they know which premises nobody confirmed.

## Rules
- One subagent per call, read-only. A fleet-wide verifier would see less than you and cost more.
- `how` must be something another lane could rerun or look up: a command, `file:line`, a URL or a quote id. "I checked" is not a `how`.
- Never mark a claim `held` without running the check. Use `unchecked` instead.
