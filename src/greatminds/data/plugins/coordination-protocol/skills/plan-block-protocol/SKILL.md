---
name: plan-block-protocol
description: Use when triaging a task into feature_plan, writing or reading a plan block, choosing scope routing (backend / ui / docs), setting mode (A=full feature, B=intensive review, C=fast UI), declaring stand_required, or deciding between parallel and serialised sub-tasks. Trigger on "plan block", "bin/plan", "feature_plan", "scope routing", "mode A/B/C", "depends_on", "audit_only", "stand_required", "ready_for_implementation".
---

# Plan-block protocol

A plan block authorises and parameterises implementation work. It is
written by ARCHITECT-PLANNER (or the one-shot `bin/plan` wrapper) and
read by every downstream role — implementers, TESTER, READER, REVIEWER.

## Required fields

```yaml
- kind: plan
  by_role: ARCHITECT-PLANNER
  at: <ISO timestamp>
  base_commit: <full git rev-parse HEAD — 40 chars>
  assignee_role: DEVELOPER | UI-DEVELOPER | TECHNICAL-WRITER | READER
  scope: backend | ui | docs
  mode: A | B | C
  plan_kind: full | bugfix
  stand_required: true | false
  stand_reason: "<one-line reason if stand_required=true>"
  audit_only: true | false       # docs-only; routes straight to feature_docs_review
  ready_for_implementation: true | false
  body: |
    <plan prose>
```

## Field semantics

- **base_commit**: full `git rev-parse HEAD` at plan-write time. NOT
  `head -c 12` — that may be all-digit and trip strict-YAML coercion
  to int (0377 incident). NOT a stale constant from another task.
- **mode**:
  - `A` — full feature: planner → multi-scope sub-tasks → tests →
    review → verified. Standard pipeline.
  - `B` — intensive review: EXPLORER probes a stand-deployed product
    as a user; bugs file as bugfix mini-tasks.
  - `C` — fast UI: UI-DEVELOPER in chat-mode against vite dev server;
    minimal ceremony.
- **stand_required**: set true if verification needs a deployed stand
  (anything mutating a live AC, anything multi-host, anything DB-state).
  When true, **stand_reason** must explain why (one line). Implementer
  files a `stand_request` to STAND-KEEPER before declaring
  `ready_for_test`.
- **audit_only**: docs scope only. Routes the task directly to
  `feature_docs_review/` (READER's queue) bypassing WRITER. READER
  audits reality-vs-docs; findings → separate WRITER task spawned by
  PLANNER later. See `audit-path-vs-post-write-path` skill (role-reader).
- **ready_for_implementation**: gate for `mv → feature_dev` (etc.).
  Until true, task stays in `feature_plan/`.

## Scope → queue routing

| scope | target queue | assignee_role |
|---|---|---|
| `backend` | `feature_dev/` | DEVELOPER |
| `ui` | `feature_ui_dev/` | UI-DEVELOPER |
| `docs` | `feature_docs/` | TECHNICAL-WRITER |
| `docs` + `audit_only=true` | `feature_docs_review/` | READER |

`bin/plan` is the one-shot wrapper that does triage → feature_plan →
append plan-block → route in a single command:

```bash
bin/plan <task-id> \
  --scope backend --assignee-role DEVELOPER \
  --base-commit "$(git rev-parse HEAD)" \
  --plan-kind full --mode A \
  --stand-required true --stand-reason "verifies cascade across two nodes" \
  --body "<plan prose>"
```

## Serialize same-module tasks

If ≥2 candidate sub-tasks likely touch the same core file (e.g., two
parallel feature_dev tasks both editing `app/datasources/manager.py`),
**do not** dispatch in parallel. Chain them via `depends_on`:
- Task A → `feature_dev/`
- Task B → blocked block referencing `verified/<task-A-id>.yaml` →
  `feature_blocked/` (NOT `feature_dev/`)

Wake-up: when Task A reaches `verified/`, AR runs `bin/wake_check` and
moves Task B from `feature_blocked/` → `feature_dev/`.

Mistake mode (0396/0397 precedent): parallel-dispatch of same-module
tasks → implementers fight over the same file, declared_files
overlap, AR's commit-by-paths fails. ARCH_PLANNER.md item 7 codifies
the chain-rule.

## Live-mutating verify owner

For `stand_required: true` plans whose body involves UI/lifecycle/CRUD
mutations on the live stand, the plan body MUST contain the literal
line:

```
live-mutating verify owner: STAND-KEEPER | EXPLORER
```

TESTER never owns mutating live verification — see `stand-protocol`.

**Tokens used:** none directly; plans reference project-specific paths
through PROJECT.md tokens (PROJECT_ROOT, STAND_HOST_A, etc.).
