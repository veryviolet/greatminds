# TESTER tests-block snippet — copy into feature file before mv feature_test/X → feature_review/X. Append-only.

# On test_result=fail|partial: append the same block but omit `ready_for_review: true` and instead
# add a `## Tests (tester) — bugs found` section listing what failed; then mv feature_test/X → feature_dev/X.

# Stand gate (new): if plan.stand_required: true, run greatminds gate-check <task-id> BEFORE this mv.
# The result MUST appear as gate_check_result/at/commit below. ARCHITECT-REVIEWER refuses
# to approve any stand-required task without gate_check_result: pass.

---
tests:
  closed_by: tester-agent
  closed_at: <ISO-8601 UTC>
  base_commit: <short sha at test time>
  test_files:
    - <project-defined; e.g. tests/unit/...>
    - <project-defined; e.g. tests/integration/...>
    # or for scope: ui:
    # - <e.g. ui/src/lib/__tests__/...>
  test_command: <exact command, e.g. <TEST_RUNNER_BACKEND> tests/unit/feature_x -q>
  test_result: pass | fail | partial
  stand_evidence: null | {
    reproduction_steps: <exact live-stand reproduction/probe steps>,
    observed_without_fix: <pre-fix or reported live behavior>,
    observed_with_fix: <post-fix live behavior>,
    lease_id: <stand lease id>,
    result: pass | fail | partial,
    commit: <tested implementation commit>,
    tester_observations: <TESTER's own functional probe output>
  }
  gate_check_result: pass | fail | missing | n/a   # n/a only if plan.stand_required: false
  gate_check_at: <ISO-8601 UTC of greatminds gate-check invocation>
  gate_check_commit: <short sha verified against stand evidence; null if n/a>
  ready_for_review: true       # ONLY on test_result=pass AND gate_check_result in [pass, n/a]
---

## Tests (tester)
What is covered. Edge cases tested. Anything intentionally not covered (with reason).
If `plan.stand_required: true`, include lease-backed `stand_evidence` from
the active lease: lease id, checked commit/worktree, host/profile, the
before/after live behavior, and TESTER-owned product checks with
URLs/commands/screenshots/logs and caveats. Do not pass with stale, blocked,
wrong-host/profile, unrelated readiness evidence, or a missing lease id.

The gate_check_* fields above record the verified greatminds gate-check run. If gate
returned `fail` or `missing`, do NOT set ready_for_review: true; either
re-run/repair the lease evidence or move the task to feature_blocked/ with
explicit dependencies.

## Test run
```
<paste of relevant output, e.g. "12 passed in 1.5s">
```
