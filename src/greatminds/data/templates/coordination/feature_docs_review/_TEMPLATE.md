# reader review block snippet

Append this block to a docs feature in `feature_docs_review/`.

---
reader:
  reviewed_by: reader-agent
  reviewed_at: <ISO>
  outcome: pass | fail | partial
  docs_checked:
    - <path>
  stand_checked: yes | no
  command_or_flow: <command/flow or "not run">
  ready_for_architect: true | false
---

## Reader review
- <findings>

## Follow-up feedback
- <optional user_feedback file if a product gap was discovered>
