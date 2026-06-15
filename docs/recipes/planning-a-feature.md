# Planning a Feature

Feature work starts as product intake:

```bash
greatminds task new \
  --stream product \
  --kind feature \
  --scope backend \
  --title "Add a daemon status summary"
```

`ARCHITECT-PLANNER` owns triage and the plan block. A plan records:

- base commit
- assignee role
- stand requirement and reason
- mode (`A`, `B`, or `C`)
- plan kind
- readiness for implementation

After the plan is ready, the planner moves the task into the queue owned by the
assignee role. Implementers should not read from or move tasks out of
`feature_plan/`.

## Scope routing

- `scope: backend` -> `feature_dev/`
- `scope: ui` -> `feature_ui_dev/`
- `scope: docs` -> `feature_docs/`

Review and handback paths are defined in the packaged schema copied to
`.greatminds/schema.yaml`.
