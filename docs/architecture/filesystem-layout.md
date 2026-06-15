# Filesystem Layout

A configured project separates editable project configuration from runtime
state:

```text
coordination/
  PROJECT.md
  coord.yaml
  mcp.local.json
  plugins.local/
  stand-profiles.yaml
  stand-profiles/

.greatminds/
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
  .stand/
  inbox/
  intent/
  journal.ndjson
  heartbeat.<role>
```

The task file path is meaningful. For example, a product task in
`.greatminds/feature_docs/` is owned by `TECHNICAL-WRITER`; the same file in
`.greatminds/feature_docs_review/` is owned by `READER`.

Per-task implementation code lives outside `.greatminds/`, under
`.worktrees/<task-id>/`. When a task enters an implementer queue,
`greatminds task mv` creates a git worktree on `task/<task-id>` and the
implementer works from that checkout. When REVIEWER moves the task to
`verified/`, greatminds merges the branch back with `--no-ff`; archiving removes
the worktree.

## Runtime artifacts

- `journal.ndjson`: append-only transition log.
- `intent/`: short-lived files written before moves and cleared after moves.
- `inbox/`: role mailboxes.
- `heartbeat.*`: role liveness files.
- `.stand/state.yaml`: singleton stand state, active lease, FIFO queue, and
  recent transition history.
- `.worktrees/<task-id>/`: in-flight code for a task. Operators inspect
  worktrees directly.
