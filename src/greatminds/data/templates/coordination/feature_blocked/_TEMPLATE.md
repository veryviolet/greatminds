# feature_blocked block snippet

Append before moving the task INTO `feature_blocked/`. The current owner (the
role that decided it cannot make progress) is responsible for writing this
block, then performing the mv. After parking, the task is owned by
ARCHITECT-REVIEWER for wake-up via bin/wake_check.

Format for `dependencies` is strict: each entry must be `<queue>/<task-id>.md`
where `<queue>` is one of the queues listed in schema.yaml and `<task-id>` is
a real file id (no slug-only form). bin/wake_check validates this format and
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
    - stand_done/<seq>-<slug>.md
  resume_to: feature_dev | feature_ui_dev | feature_docs | feature_test | feature_docs_review | feature_review
---

## Blocked (current owner)
- Why this task is blocked and what would unblock it.
- Optional: estimated effort to resume after dependencies land.
