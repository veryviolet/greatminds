# E2E Testing

For stand-required work, the test path has two evidence streams:

1. Stand readiness from the coordd lease deploy, recorded through the lease state.
2. Product verification from `TESTER`, recorded in the task's `tests` block.

The tester runs the appropriate checks, then runs:

```bash
greatminds gate-check TASK_ID
```

The result is recorded in the tests block. A passing gate means TESTER's
stand evidence names the task, lease, worktree, and implementation commit. A
missing or failing gate keeps the task out of final review.

## Typical flow

```text
feature_dev -> feature_test -> feature_review -> verified
```

If tests fail, `TESTER` records the failure and returns the task to the
implementation queue. If the task is blocked on a named dependency, the owner
parks it in `feature_blocked/` with a blocked block.

The coordd deploy path does not run product acceptance tests. It deploys or
recovers the stand, marks the lease ready after deploy evidence exists, and
leaves functional probes to TESTER or EXPLORER.
