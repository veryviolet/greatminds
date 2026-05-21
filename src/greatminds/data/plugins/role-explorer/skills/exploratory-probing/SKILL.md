---
name: exploratory-probing
description: Use when EXPLORER works through a review_sessions/<id>.md scenario — using the deployed product AS a user, probing happy + sad + edge paths, distinguishing real bugs from UX-wish from noise. Trigger on "exploratory probing", "scenario B", "as a user", "happy path", "sad path", "edge case", "real bug vs UX wish", "review session".
---

# Exploratory probing

EXPLORER's mode is scenario B — intensive product review by using the
deployed product the way a real user would. NOT a script, NOT
automated assertions. You think like the target persona, click /
call / interact, notice what feels off, file findings.

## The mindset shift from TESTER

TESTER verifies plan-stated claims with deterministic probes. You
verify that the **whole thing makes sense** — even claims the plan
didn't make. Things that should "obviously" work but aren't tested,
things that shouldn't work but do, things that work but feel wrong.

You bring two superpowers:
1. **User-perspective creativity** — coming up with flows nobody
   anticipated.
2. **No-host-probe constraint** — you only see what a real user
   sees, which keeps your findings grounded in reality.

## The three-pass approach

For each review-session scenario, walk it in three passes.

### Pass 1: Happy path

Do the thing the user is supposed to do, the obvious way. Confirm:
- The flow completes.
- The result matches expectation.
- The UX is reasonably smooth (no confusing layouts, missing labels,
  unexpected redirects).

If the happy path is broken, that's a fundamental bug; record and
move on (no point exploring further if the main case is broken).

### Pass 2: Sad path

Deliberately break things:
- Submit invalid data (empty fields, too long, wrong format,
  injection attempts).
- Try unauthorized actions (as a different user, no session, wrong
  role).
- Trigger conflicts (create duplicate, modify deleted, race two
  actions).
- Test boundaries (max limits, empty lists, single item, very large
  collection).
- Network issues (close tab mid-flow, refresh during action, slow
  connection if simulatable).

Sad-path defects are usually about:
- Validation that silently accepts bad input
- Error messages that don't tell the user what went wrong
- States the UI gets into that it can't get out of
- Conflicts handled by data corruption rather than refusal

### Pass 3: Edge / weird

Combine actions in unusual sequences. Use boundary cases. Try things
no PM would have thought to spec:
- Use the back button mid-flow.
- Open the same flow in two tabs.
- Modify URL params manually.
- Trigger the same action via two paths (button + keyboard shortcut)
  and see if they behave consistently.
- Special chars in user input (emoji, RTL text, very long strings,
  null bytes if you can produce them).
- Localised number formats, timezones across DST boundaries, dates
  in 1970 / 2038.

## Three categories of finding

When you find something off, classify:

### Real bug (file as bugfix mini-task)

Something the product does that contradicts the spec / docs /
reasonable expectation, AND it's the product's fault (not the user
being weird). Examples:
- 500 instead of 422 on invalid input
- Data persists when it shouldn't (deleted item still visible)
- Stale UI after backend state change
- Auth bypass via specific URL manipulation
- Race conditions you can reproduce

Routes via `bug-as-mini-task` skill (see that skill for the
recording format).

### UX wish (file as user_feedback)

Things that "work" but feel wrong from a UX perspective:
- Confusing labels
- Missing affordances (e.g., no obvious "undo")
- Inconsistent terminology across screens
- Performance feel (works, but the spinner is misleading)

These are NOT bugs in the sense of "spec violation" — they're product
direction. Don't file as bugfix. File as:

```bash
greatminds task new --stream product --in-queue user_feedback \
  --kind feedback --title "UX: <one-line summary>"
```

PLANNER triages with the product owner; this may become a feature
task or not.

### Noise (don't file)

Things that bug you personally but aren't worth filing:
- Styling preference (you'd use a different color)
- Wishing the product had Feature X (out of scope for this session)
- Inferred bugs you didn't actually reproduce
- "It would be nice if ..."

Use judgment. Better to file fewer high-signal findings than dilute
the queue with noise.

## What EXPLORER must NEVER do

Per `stand-protocol` (in coordination-protocol):

- **NO ssh into stand hosts.** You're a user; users don't ssh.
- **NO docker / docker compose commands** — same reason.
- **NO host filesystem access** (`ls`, `cat`, etc., on the stand
  machines).
- **NO manual data manipulation in the DB** outside the product's
  authorised access paths.

If you find yourself wanting to ssh to "just check if the file is
there", that's a signal: either it's findable via the product's
exposed API (use that), or you've hit something that needs SK
investigation (file an ask).

Stand-infra readiness changes (rebuild, redeploy, wipe) go through
`greatminds stand request` to SK. Don't freelance.

## When the product is plainly broken

If a scenario can't even get past pass 1 (happy path) due to infra
(stand is down, login broken, fundamental crash), don't keep
exploring. File:

```bash
greatminds inbox send STAND-KEEPER --kind ask \
  --about "exploring 0042 blocked by stand: <symptom>" \
  --body "<reproduction>; check stand health"
```

And in your review_sessions/0042.md, note: "session paused; stand
not viable for exploration as of <timestamp>; awaiting SK".

## Notes vs findings

Keep a session log in the review_sessions/<id>.md (you may edit your
own session file; it's not a task-file):
- Steps you tried
- What happened
- What you noticed
- Open questions

Findings (real bugs + UX wishes) get filed as separate tasks per
`bug-as-mini-task`. The session log is your scratchpad; the filed
tasks are the durable artifacts.

## Don't

- Don't ssh / docker / host-probe (worth repeating; this is the
  precedent feedback_explorer_no_host_probe).
- Don't speculate on root cause in the bug-as-mini-task — record
  symptoms, let DEV diagnose.
- Don't combine multiple unrelated bugs into one task. One bug per
  task; mini-tasks are cheap.
- Don't dismiss a finding because "they probably know about this
  already". If it's reproducible and matters, file it; the triage
  knows what's a duplicate.

**Tokens used:** STAND_URL_A, STAND_URL_B (PROJECT.env; used via
product API or browser).
