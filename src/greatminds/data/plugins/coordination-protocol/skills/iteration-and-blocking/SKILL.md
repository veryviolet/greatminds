---
name: iteration-and-blocking
description: Use when a task bounces back from TESTER with fail/partial outcome, when a task needs to wait on another (depends_on chain), or when blocked tasks are ready to wake. Covers iteration block fields, the back-from-tester path, blocked-block format with the terminal-queue rule (verified/ paths only, never queue paths), and bin/wake_check workflow. Trigger on "iteration block", "blocked", "depends_on", "feature_blocked", "wake_check", "back from tester", "dependency".
---

# Iteration and blocking

Two distinct mechanisms, both protocol-level:

1. **Iteration** — implementer fixes what TESTER rejected, re-submits.
2. **Blocking** — task can't proceed because it depends on another
   not-yet-verified task; parks in `feature_blocked/` until woken.

## Iteration after TESTER bounce

When TESTER appends a `tests` block with `outcome: fail` or `partial`,
the task moves back to the implementer's queue. Implementer must:

```yaml
- kind: iteration
  by_role: DEVELOPER | UI-DEVELOPER | TECHNICAL-WRITER
  at: <ISO>
  responds_to: tests       # block_kind being responded to
  base_commit: <NEW git rev-parse HEAD after fix>
  declared_files: [...]    # updated set if fix added/removed files
  ready_for_test: true
  body: |
    What TESTER reported failing, what the actual cause was, what
    code changed, why the fix addresses the root cause not the symptom.
```

Then `bin/task mv <id> feature_test --reason "re-test after fix"`.

Multiple iterations stack. If you find yourself on iteration ≥3 on
the same task without progress, **stop** and either:
- File `bin/inbox send ARCHITECT-PLANNER --kind ask` describing why the
  plan-block assumption seems wrong, or
- File `bin/inbox send MAINTAINER --kind ask` if the issue is
  schema/tooling.

Do not silently keep iterating — that's how 8-iteration death spirals
happen.

## Blocking on a dependency

If your task literally cannot proceed without another finishing,
append a `blocked` block and mv to `feature_blocked/`:

```yaml
- kind: blocked
  by_role: <your role>
  at: <ISO>
  dependencies:
    - verified/0123-other-task-slug.yaml
  resume_to: feature_dev    # where the task should go when woken
  body: |
    Need API contract X from <other-task-id> before serialiser can
    speak to it. <one-sentence concrete data dependency>
```

```bash
bin/task append-block blocked --id <id> \
  --field dependencies=verified/0123-other-task-slug.yaml \
  --field resume_to=feature_dev \
  --body "..."
bin/task mv <id> feature_blocked --reason "depends on 0123"
```

## CRITICAL: terminal-queue rule for dependencies

`dependencies:` entries MUST be paths in **terminal** queues — exactly
`verified/<id>.yaml`. Never `feature_dev/<id>.yaml` or any active
queue.

Why (feedback 0410 incident): `bin/wake_check` resolves the literal
path. If you wrote `feature_dev/<id>.yaml` and the dependency
subsequently progresses (e.g., DEV → TESTER), the file moves out of
`feature_dev/` to `feature_test/`, then `feature_review/`, then
`verified/`. Your blocked-block's dependency path becomes stale and
`bin/wake_check` reports `(missing)` — a permanent jam. Only AR can
manually unstick by rewriting the path.

So: always reference dependencies in their **final, terminal** form:
`verified/<id>.yaml`. The dep WILL eventually land there if work
proceeds; until then, your task waits.

## Wake_check workflow (REVIEWER)

ARCHITECT-REVIEWER runs `bin/wake_check` at each tick start:

```bash
bin/wake_check
# Lists blocked tasks whose dependencies are ALL satisfied
# (their verified/<id>.yaml files exist).
```

For each ready task, AR moves it to `resume_to`:

```bash
bin/task mv <blocked-id> <resume_to> --reason "deps verified — woken"
```

This is one of the AR-only responsibilities. If wake_check reports
`(missing)` paths, that's a stale terminal-rule violation — escalate
to MAINTAINER for manual unstick.

**Tokens used:** none directly.
