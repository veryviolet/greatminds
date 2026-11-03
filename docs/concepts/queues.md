# Queues

Queues are directories under `.greatminds/`. A task's directory is its state.
Moving a task file from one queue to another transfers ownership to the next
role.

Common product queues:

- `feature_inbox/`: unplanned product work.
- `feature_plan/`: planner-owned work with a plan being written or routed.
- `feature_dev/`: backend implementation.
- `feature_ui_dev/`: UI implementation.
- `feature_docs/`: documentation implementation.
- `feature_test/`: code ready for tester validation.
- `feature_docs_review/`: docs ready for fresh-reader review.
- `feature_review/`: final architectural review.
- `feature_blocked/`: tasks parked on explicit dependencies.
- `verified/`: approved work. This is normally a final state, but
  `ARCHITECT-REVIEWER` may roll it back when later evidence shows the work is
  wrong or invalid.
- `archive/`: abandoned, superseded, or withdrawn work.

A verified task can leave `verified/` only through an explicit reviewer
rollback. The reviewer appends a `rollback` block with a non-empty `reason`,
then moves the task either to `archive/` when the work has been reverted or to
`feature_review/` when it needs another review/fix cycle.

Inline flags such as `ready_for_implementation: true` are evidence. They do not
handoff ownership. Only the queue move does that.

## Mutation rule

Queue-assigned agents and task-bound conversations submit typed decisions through
`greatminds run submit`. The daemon checks their assigned task revision, role,
evidence and transition gates before changing task files. Direct `task mv` and
`task append-block` writes are rejected for these managed runs.

A standalone planner conversation uses `task new`, `plan`, `task append-block`
and `task mv` to submit authenticated planning requests. The daemon owns their
application and recovery; see [planning a feature](../recipes/planning-a-feature.md).

An operator outside a managed run can use the role-scoped task CLI directly:

```bash
greatminds task append-block implementation --id TASK_ID --field ready_for_test=true
greatminds task mv TASK_ID feature_test
```

The direct CLI validates role permissions, required readiness fields, schema
shape, journal entries, intents, and heartbeat side effects. Managed result and
planning operations share the domain gates and record SYSTEM application
provenance. Pending operations hold competing task mutations and claims.

Use `greatminds task show`, `greatminds task list`, and
`greatminds task paths` for state inspection. Do not substitute directory
listings for CLI checks when deciding ownership or readiness.
