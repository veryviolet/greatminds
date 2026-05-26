# Filesystem Layout

A configured project contains a `coordination/` directory. Typical contents:

```text
coordination/
  PROJECT.md
  feature_inbox/
  feature_plan/
  feature_dev/
  feature_ui_dev/
  feature_docs/
  feature_test/
  feature_docs_review/
  feature_review/
  feature_blocked/
  verified/
  archive/
  stand_requests/
  stand_wip/
  stand_done/
  inbox/
  intent/
  journal.ndjson
  heartbeat.<role>
  stand.status
```

The task file path is meaningful. For example, a product task in
`coordination/feature_docs/` is owned by `TECHNICAL-WRITER`; the same file in
`coordination/feature_docs_review/` is owned by `READER`.

Per-task implementation code lives outside `coordination/`, under
`.worktrees/<task-id>/`. When a task enters an implementer queue,
`greatminds task mv` creates a git worktree on `task/<task-id>` and the
implementer works from that checkout. When REVIEWER moves the task to
`verified/`, greatminds merges the branch back with `--no-ff`; archiving removes
the worktree.

## Runtime artifacts

- `journal.ndjson`: append-only transition log.
- `intent/`: short-lived files written before moves and removed after moves.
- `inbox/`: role mailboxes.
- `heartbeat.*`: role liveness files.
- `stand.status`: current stand summary.
- `.worktrees/<task-id>/`: in-flight code for a task. This replaces the older
  file-lock and lock-release model; operators should inspect worktrees instead
  of lock files.
