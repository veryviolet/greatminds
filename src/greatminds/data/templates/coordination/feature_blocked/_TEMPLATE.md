# feature_blocked block snippet

Append before moving the task INTO `feature_blocked/`.

For ordinary dependency parking, the current owner (the role that decided it
cannot make progress) writes this block and performs the mv.

For USER-requested cancellation, ARCHITECT-PLANNER should run
`greatminds task withdraw <task-id> --reason "<why>"`. That command writes a
withdrawn-class block and moves the task here. ARCHITECT-REVIEWER remains the
only role that can archive the parked task.

After parking, the task is owned by ARCHITECT-REVIEWER for wake-up via
greatminds wake-check.

Format for `dependencies` is strict: each entry must be `<queue>/<task-id>.md`
where `<queue>` is one of the queues listed in schema.yaml and `<task-id>` is
a real file id (no slug-only form). greatminds wake-check validates this format and
rejects free-form strings.

`resume_to` must be a queue name from schema.yaml that the originating role
owns or writes to. When all dependencies physically exist, ARCHITECT-REVIEWER
moves the task back to `resume_to`.

---
blocked:
  blocked_by: developer-agent | ui-developer-agent | tester-agent | reader-agent | technical-writer-agent | architect-planner-agent | architect-reviewer-agent
  blocked_at: <ISO-8601 UTC>
  reason: <short concrete reason this task cannot progress>
  dependencies:
    - verified/<seq>-<slug>.md
    - feature_test/<seq>-<slug>.md
  resume_to: feature_dev | feature_ui_dev | feature_docs | feature_test | feature_docs_review | feature_review
---

## Blocked (current owner)
- Why this task is blocked and what would unblock it.
- Optional: estimated effort to resume after dependencies land.
