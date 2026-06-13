# TESTER tests-block snippet — copy into feature file before mv feature_test/X → feature_review/X. Append-only.

# On test_result=fail|partial: append the same block but omit `ready_for_review: true` and instead
# add a `## Tests (tester) — bugs found` section listing what failed; then mv feature_test/X → feature_dev/X.

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
  ready_for_review: true       # ONLY on test_result=pass; omit on fail|partial
---

## Tests (tester)
What is covered. Edge cases tested. Anything intentionally not covered (with reason).
If `plan.stand_required: true`, include lease-backed `stand_evidence` from
the active lease: lease id, checked commit/worktree, host/profile, the
before/after live behavior, and TESTER-owned product checks with
URLs/commands/screenshots/logs and caveats. Do not pass with old, blocked,
wrong-host/profile, unrelated readiness evidence, or a missing lease id.

## Test run
```
<paste of relevant output, e.g. "12 passed in 1.5s">
```
