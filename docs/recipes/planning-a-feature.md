# Planning a Feature

## From a planner conversation

Open an on-demand **ARCHITECT-PLANNER** conversation in the web workspace or
through `greatminds chat`. Discuss the proposed work first. The planner context
includes the schema's intake discipline: propose, wait for user approval, then
file. The harness follows that instruction; the daemon validates role, task
revision and workflow gates, not the meaning of the user's approval.

The planner can handle several tasks in one conversation. With its assigned run
credentials, `task new`, `plan`, `task append-block` and `task mv` submit durable
requests for the daemon. They wait up to 30 seconds for application and report
the request ID if still pending. Keep the credentials supplied by the supervisor;
do not remove them to bypass the managed path. A task-bound conversation or
queue-assigned planner instead uses `greatminds run submit` for its assigned task.

Feature work starts as product intake. The following commands are issued by the
planner after approval (or an operator with `GREATMINDS_ROLE=ARCHITECT-PLANNER`):

```bash
greatminds task new \
  --stream product \
  --kind feature \
  --scope backend \
  --title "Add a daemon status summary"
```

Use the exact ID returned by creation, and the actual base commit:

```bash
greatminds plan TASK_ID --scope backend --assignee-role DEVELOPER \
  --base-commit BASE_COMMIT --plan-kind full --mode A \
  --stand-required false --stand-reason "Covered by local unit tests" \
  --body "Implement the agreed change and independently validate it."
```

Task scope and plan scope must match. Use `--stop-at plan` to record a plan
without handing it to an implementer. Required worktree setup and readiness
checks still apply when routing.

## Delivery and recovery

Identical convenience-command requests in the same conversation reuse their
receipt, including after a process restart. For intentionally separate requests
with identical contents, use `greatminds run planning --action create|decide
--request-id ID --json JSON`; `decide` requires the exact task ID, its expected
SHA256 revision and a list of typed decision steps. See `run planning --help`.

Inspect a delayed or rejected request with
`greatminds run planning-status REQUEST_ID`. Accepted requests continue through
the daemon even if the originating turn ends. A stopped daemon leaves them
pending until restart; repeating the same request does not create another task.

Multi-step planning is recoverable, not all-or-nothing: completed steps remain
when a later gate rejects the plan. Inspect the receipt and current task, then
correct the remaining evidence/routing with `task append-block` and `task mv`.
An uncertain operation marked `needs_recovery` holds task mutations and claims.
After inspecting and resolving the recorded cause, an operator outside the
managed run can request `run planning-status REQUEST_ID --retry`. The daemon
rechecks the operation before continuing; it does not ask the model to repeat it.

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
