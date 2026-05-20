---
name: re-verify-loop
description: Use when SK has redeployed the stand after a fix landed, and EXPLORER needs to re-verify the previously-failing scenarios. Covers tracking what passed vs failed before, idempotent re-probing, and when a finding can be marked resolved. Trigger on "re-verify", "after redeploy", "fix landed", "regression check", "scenario re-run", "EXPLORER follow-up".
---

# Re-verify loop

When a bugfix from EXPLORER's previous session lands in verified/ and
SK redeploys, EXPLORER re-runs the relevant scenarios to confirm the
fix actually works on the live stand. Different from a fresh
exploration session: you're checking specific previously-reported
behaviour, and you must be careful not to introduce false positives
("looks fixed" when it's still subtly broken).

## Trigger

A re-verify is justified when:
- A bugfix task EXPLORER filed (or any related task) is now in
  verified/.
- SK has produced a stand_done at the SHA containing the fix.
- The review_sessions/<id>.md is still open (not archived).

If the original review_sessions task was closed before the fix
landed, that's PLANNER's call to reopen it (or create a new
review-session task referencing the fix).

## Tracking scenarios

In your review_sessions/<id>.md (your editable working session
file), maintain a status table:

```markdown
## Scenarios — status

| # | Scenario | First-pass status | Bug filed | Re-verify status |
|---|---|---|---|---|
| 1 | Happy: create item via UI | PASS | — | — |
| 2 | Validation: empty name via API | FAIL | 0193 (filed) | PENDING (awaiting fix) |
| 3 | Auth: cross-user edit | FAIL | 0194 (filed) | PASS (verified at stand_done/0211, fix landed in dc51b23) |
| 4 | Edge: rapid double-click create | inconsistent | 0195 (filed) | PARTIAL (less frequent now; underlying race not fully fixed) |
```

Re-verify status:
- **PENDING** — fix not yet landed / deployed
- **PASS** — re-verified; original symptom no longer reproducible
- **PARTIAL** — partially better; original symptom less severe but
  still present in some form
- **FAIL** — fix didn't address the original symptom

## Idempotent re-probing

Repeat the EXACT reproduction steps from the original bug filing.
Same data values, same sequence, same prior state.

For each step:
1. Set up the prior state (login, navigate, prerequisite data)
2. Execute the action
3. Observe the result
4. Compare to original Expected vs Actual

The fix is verified only if:
- Original Actual no longer occurs
- New behaviour matches original Expected
- No new defect introduced by the change

## Subtle pitfalls

### "Looks fixed" because the path changed

A bug might be "fixed" by accident — the codepath that exhibited the
bug is no longer reached. Verify the test data actually hits the new
code by:
- Trying the original path
- Trying a path that should ALSO hit the same defect (if the defect
  was in a shared validator, multiple endpoints / forms should now
  behave correctly)

If only one entry point is fixed and others still exhibit the
original behaviour: PARTIAL.

### Cache / state leak

The fix might require a fresh-DB or fresh-session to be visible. If
re-verifying without the bugfix-pass cleanup leads to confusing
results:
- File `bin/stand request` for a fresh-DB if your scenario justifies
  it.
- Otherwise note in re-verify result: "verified with previously-
  existing test data; may behave differently on greenfield state".

### Re-verifying a UI bug — visual evidence

If the bug was visual (layout, color, state), capture a screenshot
of the fixed state alongside the original bug's screenshot. Include
both in your re-verify notes. This catches the "looks fixed in this
viewport, broken in another" case.

## When to mark "verified" in the bug task

Once your re-verify PASSes:
- DO NOT touch the bug task directly (it's verified/ — terminal).
- Update your review_sessions/<id>.md re-verify status table.
- If desired, file an inbox info to PLANNER summarizing: "0193's
  fix verified on stand at SHA <X>".

## What if re-verify fails

Status FAIL or PARTIAL after a "fix":

1. **Confirm reproduction is faithful.** Did you exactly repeat the
   original steps? Different data or a different user role can
   silently mask.
2. **Check the implementation's tests.** TESTER's tests block in the
   verified bug task may have tested something different from what
   you originally reported. Note the gap in your re-verify.
3. **File a follow-up.** New bug task (don't reopen verified ones —
   those are immutable history). Title:
   `<area>: <original-bug-id>'s fix landed but symptom persists in <variant>`.
   Reference the original bug-id in body.

This isn't a failure of EXPLORER — it's exactly what re-verify is
for. Catching incomplete fixes is high-value.

## Idle case — scenario passed first time, still passes

For scenarios that were never failing, you don't have to re-verify
every cycle. Focus re-verify effort on what was actually broken.
That said: if SK redeployed with a substantial set of changes (not
just a one-bug fix), do a quick happy-path pass to catch regressions.
A bug that was working before but breaks after the fix is a
regression — file it like any other find.

## Closing the session

When all bug-filed scenarios are PASS (or have follow-up tasks
filed), EXPLORER can close the review_sessions task. AR-or-PLANNER
moves review_sessions/<id>.md to archive/ (review_sessions has its
own archive transition).

If the review session uncovered enough material for a follow-up
session (more scenarios, different audience), file a new
review_sessions task with PLANNER linking back to this one.

## Don't

- Don't claim a re-verify PASS without actually executing the
  reproduction. Walking through it mentally is not enough.
- Don't downplay PARTIAL results to PASS. PARTIAL is honest;
  pretending PASS sets up a worse re-verify down the line.
- Don't re-verify your own findings only. If the redeploy touched
  shared code, sanity-check adjacent flows you didn't originally
  flag — silent regressions are the worst kind.

**Tokens used:** STAND_URL_A, STAND_URL_B (PROJECT.env).
