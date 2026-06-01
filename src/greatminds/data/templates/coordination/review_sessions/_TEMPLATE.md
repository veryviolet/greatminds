# review_sessions template (scenario B)

A review session is a coordination artifact for scenario B (intensive review
on a live stand). ARCHITECT-PLANNER creates it; EXPLORER reads it and
appends iteration notes; ARCHITECT-PLANNER fast-triages filed bugs.

The session file lives in `coordination/review_sessions/<id>.md` until the
review is concluded, then is moved to `coordination/archive/`.

---
id: <seq>-<short-slug>
stream: review_session
opened_by: architect-planner-agent
opened_at: <ISO-8601 UTC>
mode: B
target_functionality: <one-line description of what is being reviewed>
stand_target:
  hosts:
    - <host>
  profile: full-deploy
  commit: <sha or current-working-tree>
scenarios:
  # 0331: EXPLORER surface is BLACK-BOX (deployed CLI + REST/browser on
  # published URLs) ONLY — no ssh, no host filesystem, no docker, no
  # kill/logout host-lifecycle steps. Route host-destructive lifecycle
  # validation to STAND-KEEPER, not EXPLORER (schema.roles.EXPLORER
  # review_session_surface / host_destructive_validation_owner).
  - <scenario 1: black-box step — what to try via CLI/REST, what to expect>
  - <scenario 2: ...>
status: open | concluded
---

## Goal
What this review session is investigating and why.

## Stand readiness
- Required stand_done/<id> reference once STAND-KEEPER confirms.
- `stand.status` snapshot at session open.

## Scenarios
Detailed walk-throughs of each scenario including starting state, expected
behavior, and known-good baselines.

## Iteration log
(EXPLORER appends one section per pass.)

### Iteration 1 — <ISO-8601 UTC>
- Scenarios run:
- Bugs filed:
  - `feature_inbox/<id>.md`
- Notes:

### Iteration N — …

## Conclusion (ARCHITECT-PLANNER)
- Verified scenarios.
- Open bugs still on the way to verified/.
- Decision: archive session, schedule follow-up review, or extend with new
  scenarios.
