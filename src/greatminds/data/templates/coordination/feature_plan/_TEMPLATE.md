# architect plan block snippet

Append before moving `feature_inbox/X.md -> feature_plan/X.md`.

New required fields (this refactor):
- `plan_kind: full | bugfix` — `bugfix` allows a one-line plan for fast
  EXPLORER-found bugs in scenario B.
- `mode: A | B | C` — which scenario this task belongs to (see schema.yaml
  scenarios). Affects which roles claim it.

---
plan:
  written_by: architect-planner-agent
  written_at: <ISO-8601 UTC>
  base_commit: <short sha at plan time>
  assignee_role: DEVELOPER | UI-DEVELOPER | TECHNICAL-WRITER | STAND-KEEPER
  plan_kind: full | bugfix
  mode: A | B | C
  stand_required: true | false
  stand_reason: <why deployed stand validation is or is not required>
  estimated_files:
    - <path or pattern>
  risks:
    - <risk>
  ready_for_implementation: true
---

## Plan (architect)
Detailed sequence of work for the assigned role.
For `plan_kind: bugfix`, a single line is acceptable, e.g. "fix as described in
Background; reuse pattern from <verified/Y.md>".

1. ...

## Files to create / modify
- `<path>`: description.

## Verification / handoff
- <tests/docs build/stand request expected>
- If `stand_required: true`, specify the expected `stand_requests/` /
  `stand_done/` evidence (with matching `evidence_for: [<this-task-id>]`)
  before TESTER/READER may pass.
