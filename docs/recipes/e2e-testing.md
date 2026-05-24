# E2E Testing

For stand-required work, the test path has two evidence streams:

1. Stand evidence from `STAND-KEEPER`.
2. Test evidence from `TESTER`.

The tester runs the appropriate checks, then runs:

```bash
greatminds gate-check TASK_ID
```

The result is recorded in the tests block. A passing gate means the stand
evidence matches the task and commit. A missing or failing gate keeps the task
out of final review.

## Typical flow

```text
feature_dev -> feature_test -> feature_review -> verified
```

If tests fail, `TESTER` records the failure and returns the task to the
implementation queue. If the task is blocked on a named dependency, the owner
parks it in `feature_blocked/` with a blocked block.
