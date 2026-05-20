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
  stand_evidence: null | <stand_done/Y.md readiness plus checked host/profile/commit summary>
  ready_for_review: true       # ONLY on test_result=pass; omit on fail|partial
---

## Tests (tester)
What is covered. Edge cases tested. Anything intentionally not covered (with reason).
If `plan.stand_required: true`, include the matching `stand_done/` id, checked
commit/working tree and hosts/profile, then record TESTER-owned product checks
with URLs/commands/screenshots/logs and caveats. Do not pass with old, blocked,
wrong-host/profile, or unrelated stand readiness evidence.

## Test run
```
<paste of relevant output, e.g. "12 passed in 1.5s">
```
