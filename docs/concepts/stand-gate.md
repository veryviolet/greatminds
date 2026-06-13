# Stand Gate

Some tasks need live-system evidence before final review. A plan marks that
with:

```yaml
stand_required: true
stand_reason: "why live evidence is needed"
```

Stand access uses the lease workflow described in
[Stand Operations](stand-operations.md). `TESTER` requests a lease, waits for
coordd's deploy path to mark it ready, probes the deployed stand, records
lease-based `stand_evidence` in the product task's `tests` block, and runs:

```bash
greatminds gate-check TASK_ID
```

The gate check verifies that tests-block stand evidence exists for the task,
that the result is acceptable, and that the recorded commit matches the
implementation commit. Final review should not approve a stand-required task
without a passing gate result.

Documentation-only tasks usually set `stand_required: false` and rely on local
build or reader review instead.
