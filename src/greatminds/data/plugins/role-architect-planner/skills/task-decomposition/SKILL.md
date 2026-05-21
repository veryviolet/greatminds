---
name: task-decomposition
description: Use when ARCHITECT-PLANNER is breaking a user-feedback request or feature inbox item into parent + scoped sub-tasks ready for implementer queues. Covers when to split vs keep monolithic, when to parallelise vs serialise (depends_on chains on same module), how to size sub-tasks, and how to write a parent that REVIEWER can later assemble. Trigger on "decompose feature", "parent task", "sub-tasks", "scope split", "serialize vs parallel", "task sizing", "feature breakdown".
---

# Task decomposition

PLANNER turns a USER ask (or an inbox feature request, or a READER-found
gap) into a parent task + one-or-more child sub-tasks, each routed to
the right scope's queue. Good decomposition is the highest-leverage
PLANNER action — bad decomposition wastes downstream effort.

## When to split

Split a feature into sub-tasks when:
- It crosses scope boundaries (backend + ui + docs) — each scope needs
  its own claim by its dedicated role.
- It's large enough that one implementer would file mid-iteration
  blocks ("partial — backend done, UI pending"). Split pre-emptively
  instead.
- Different parts have different risk profiles (one stand-required,
  one not) — separate so the unrequired one isn't blocked on the
  required one's stand readiness.

Keep monolithic when:
- One scope, one file, one logical change. Don't artificially split
  a 50-line refactor into "rename function" + "update callers" — let
  one implementer do it in one block.
- Strong cross-coupling: the parts only make sense atomically (a
  protocol change that adds a new message type AND its handler).

## Parent + children pattern

```
parent task (lives in feature_inbox → feature_plan → feature_review eventually)
  ├─ sub-task A (scope: backend, queue: feature_dev)
  ├─ sub-task B (scope: ui,      queue: feature_ui_dev)
  └─ sub-task C (scope: docs,    queue: feature_docs)
```

Parent's plan-block body lists the children explicitly:

```yaml
body: |
  Parent: implement feature X.
  Children:
  - 0042-backend-X-api-endpoint (scope: backend)
  - 0043-ui-X-page-and-form     (scope: ui)
  - 0044-docs-X-user-guide      (scope: docs)
  Children should be processed in parallel (no inter-child dependencies).
  Parent is verified by REVIEWER when ALL children reach verified/.
```

Children are created via `greatminds task new` and routed via `greatminds plan` each.
The parent stays in `feature_review/` (or `feature_blocked/` if it
should wait) until all children are verified.

## Serialize vs parallel — same-module heuristic

Two child sub-tasks should be **serialised** (one depends on the other
via blocked-block) if they likely touch the **same core file(s)**.
Otherwise dispatch in parallel.

Concrete heuristic:
- Both children's expected `declared_files` overlap → serialise.
- Cross-scope (one backend, one UI) → parallel by default; they touch
  different trees.
- Same scope, different files → parallel.
- Same scope, same files → serialise (B blocks on A's verified/).

Precedent 0396/0397: two parallel feature_dev tasks both edited the
same `manager.py` → implementer fights, declared_files overlap, AR
commit-by-paths fails. ARCH_PLANNER.md item 7 codified the same-module
serialisation rule. Apply pre-emptively.

## Sub-task sizing

Aim for: a sub-task is one tick's work for a competent implementer.
Roughly:
- 1-5 files in declared_files
- One conceptual change (an endpoint, a component, a doc section)
- One scope, one role
- Verifiable in one tests-block from TESTER

If a sub-task would have to be split mid-implementation into 2+
implementations, it's too big — split before writing the plan. Common
signal: you can't write a one-sentence `summary` field of what it does.

If a sub-task is so small the implementer's overhead (filing block,
mv, TESTER claim) dominates, fold it into a bigger one. Common
signal: declared_files would have just one file with a one-line change.

## Audit-only docs sub-tasks

For docs scope where the goal is "find out if docs match reality"
(not "write specific changes"), use audit-only path: route directly
to feature_docs_review (READER), skipping WRITER:

```bash
greatminds plan <task-id> --scope docs --assignee-role READER --audit-only \
  --base-commit "$(git rev-parse HEAD)" \
  --plan-kind full --mode A \
  --stand-required false \
  --body "Audit current docs vs deployed reality for area X. Findings → reader_review block. Separate WRITER task will be spawned if changes are needed."
```

READER produces a reader_review block; PLANNER reads that AFTER it's
verified, then spawns a separate WRITER task with concrete edits.
Don't try to "predict" findings in advance.

## Writing the parent

Parent plan-block body must give REVIEWER enough to validate that the
verified children collectively satisfy the parent intent. Include:

- One-paragraph user-facing description of the feature.
- The list of children with their scopes.
- Any architectural decisions / ADRs the children should follow.
- Explicit success criteria ("user can do X end-to-end" or "endpoint
  Y returns Z under conditions W"). REVIEWER uses this in
  evidence-chain-verification.

**Tokens used:** none.
