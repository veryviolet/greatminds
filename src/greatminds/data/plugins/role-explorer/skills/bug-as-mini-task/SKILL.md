---
name: bug-as-mini-task
description: Use when EXPLORER files a finding from an exploratory session as a plan_kind=bugfix mini-task in feature_inbox — title format, scope identification, reproduction steps, expected vs actual, when to add live-mutating verify owner declaration. Trigger on "bug file", "bugfix mini-task", "plan_kind bugfix", "reproduction steps", "file finding", "explorer finding".
---

# Bug-as-mini-task

The output of EXPLORER's exploration is **filed bugs** — concise,
reproducible, scoped mini-tasks that PLANNER can triage rapidly and
implementers can fix without further interrogation.

## File via `bin/task new`

```bash
bin/task new \
  --stream product \
  --in-queue feature_inbox \
  --kind bugfix \
  --scope <backend|ui|docs> \
  --title "<short title — see below>"
```

Then PLANNER triages, plans (often a small one-line plan_kind: bugfix
plan), and routes to feature_dev / feature_ui_dev / feature_docs.

## Title format

Compact, specific, scannable. The pattern:

```
<area>: <symptom> when <condition>
```

Examples:
- `items: POST returns 500 when name is empty (expected 422)`
- `projects UI: delete button stays clickable after delete (page caches stale state)`
- `auth: PATCH /me allows changing other users' email when target id passed in body`

Avoid:
- Vague titles ("bug in items endpoint") — PLANNER can't triage fast.
- Solution-suggesting titles ("add validation for empty name") — that's
  the implementer's call. State the symptom.

## Body — reproduction first, observation second

After `bin/task new` creates the task, append a triage-ready bug body
via the task's initial block (kind: feedback or kind: bugfix-intake,
depending on schema). Body structure:

```yaml
body: |
  ## Reproduction

  1. Launch the deployed product at ${STAND_URL_A}
  2. POST /items with body {"name": "", "price": 10}
  3. Observe response

  ## Expected

  422 Unprocessable Entity with detail field naming 'name' as missing
  or empty. Per docs/reference/api.md section "POST /items" validation.

  ## Actual

  500 Internal Server Error with body:
  ```
  {"detail": "Internal server error"}
  ```

  Backend logs (if I had access — I don't, but TESTER/SK should
  reproduce):
  - Expected log line "validation failed: name required"
  - Likely actual: IntegrityError on NULL violation, hint that
    validation isn't reached.

  ## Scope

  Backend: app/api/items.py:create_item (best guess; PLANNER /
  DEV will confirm).

  ## Severity

  Medium — error message is not actionable for end user; data
  isn't corrupted; flow has alternate path (provide valid name).

  ## Notes

  Reproduced 3 times. Did not reproduce with other invalid fields
  (price negative correctly returns 422). Specific to name=="" /
  name missing.
```

## Reproduction steps — atomic

Steps must:
- Start from a known state ("after fresh login" or "from the items
  page")
- Each be one action ("click X", "POST /Y", "press Z")
- End with an observable result
- Include exact data values used (not "some invalid input"; show
  the literal payload)

If reproduction requires specific prior state (existing items, a
specific user role), include the setup as numbered prior steps.

## Expected vs Actual

- **Expected:** what should happen per docs / spec / common sense.
  Cite the source if applicable.
- **Actual:** what you actually observed. Be specific — error
  messages verbatim, status codes, exact text shown.

Don't editorialize ("it didn't work right"). Show the contrast in
specifics.

## Scope identification

A best-guess scope (backend / ui / docs) for routing. PLANNER will
confirm. Hints:
- 5xx error from API → backend
- 4xx error with wrong detail / wrong status → could be either; lean
  backend
- UI behaves wrong but the API is fine → ui
- Docs describe X but product does Y (and product is correct) → docs
- Docs describe X correctly but product does Y → backend or ui per
  what's broken

If unclear, write `scope: ?` in body; PLANNER decides.

## Severity

- **high** — primary user flow blocked; data corruption / loss;
  security (auth bypass, data leak).
- **medium** — user can still get done what they want via workaround;
  data isn't lost; defect is annoying not blocking.
- **low** — cosmetic, edge case, doesn't affect normal use.

Use the rubric honestly. Inflating severity erodes your credibility.

## live-mutating verify owner — when to declare

If the bug's reproduction itself involves mutating state (create →
delete → check absence), AND the fix will need to be verified on the
stand:

Add to body:

```yaml
live-mutating verify owner: EXPLORER
```

(Or STAND-KEEPER if more appropriate for the specific flow.)

This is the literal line `stand-protocol` mandates for any
stand_required UI/lifecycle/CRUD plan. PLANNER will likely set
stand_required: true in the bugfix plan; this line tells TESTER /
SK / EXPLORER who owns the live verification.

## Don't

- Don't file with a fix suggestion as the title. Symptom in title,
  not solution.
- Don't lump multiple bugs in one task. One bug, one task. Lumping
  makes triage worse and partial fixes possible (which is worse
  than no fix).
- Don't speculate at root cause in the reproduction. State what you
  did and what you saw; the body can have a "possible cause"
  paragraph with explicit "guessing" framing, but don't poison the
  symptom write-up.
- Don't file without verifying you can reproduce. If you observed
  it once and can't repro, note it in your session log; come back
  if it recurs.

**Tokens used:** STAND_URL_A, STAND_URL_B (PROJECT.env, used in
reproduction steps).
