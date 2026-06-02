# Queues

Queues are directories under `coordination/`. A task's directory is its state.
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
  `ARCHITECT-REVIEWER` may roll it back when post-verify evidence shows the
  work is wrong or obsolete.
- `archive/`: abandoned, superseded, or withdrawn work.

A verified task can leave `verified/` only through an explicit reviewer
rollback. The reviewer appends a `rollback` block with a non-empty `reason`,
then moves the task either to `archive/` when the work has been reverted or to
`feature_review/` when it needs another review/fix cycle.

Inline flags such as `ready_for_implementation: true` are evidence. They do not
handoff ownership. Only the queue move does that.

## Mutation rule

Agents mutate task state with the CLI:

```bash
greatminds task append-block implementation --id TASK_ID --field ready_for_test=true
greatminds task mv TASK_ID feature_test
```

The CLI validates role permissions, required readiness fields, schema shape,
journal entries, intents, and heartbeat side effects.

Use `greatminds task show`, `greatminds task list`, and
`greatminds task paths` for state inspection. Do not substitute directory
listings for CLI checks when deciding ownership or readiness.
