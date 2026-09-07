# Roles

Roles divide responsibility across the workflow. Queue ownership and allowed
transitions come from the effective installed schema. The project schema file
is a diagnostic mirror.

| Role | Responsibility |
| --- | --- |
| `ARCHITECT-PLANNER` | Intake, triage, planning and routing |
| `DEVELOPER` | Backend implementation |
| `UI-DEVELOPER` | UI implementation |
| `LIVE-DEVELOPER` | User-paced implementation and live stand iteration |
| `TECHNICAL-WRITER` | Documentation implementation |
| `TESTER` | Validation and test evidence |
| `READER` | Documentation review from a fresh reader's perspective |
| `ARCHITECT-REVIEWER` | Final review and commit policy |
| `EXPLORER` | Exploratory review and bug filing |
| `MAINTAINER` | Infrastructure diagnosis and operations that require judgment |
| `USER` | User-originated input and feedback |

Bind a role to an ACP agent in `coordination/execution.yaml`. Harness, model,
workspace, permissions and scheduling are separate choices. The same harness
can serve several roles when its capabilities satisfy their bindings; sharing a
harness does not combine those roles' authority or bypass review gates.

Use `scheduling: queue` to admit eligible workflow tasks automatically and
`on-demand` for operator-paced work. Mechanical readiness, blocked-task
resumption, startup backoff and runtime reconciliation belong to daemon code.
They do not require a self-polling MAINTAINER conversation.

Inspect runs and holds through `greatminds run status` and `greatminds watchdog`.
Runtime state and durable outcomes provide execution evidence; a heartbeat or
stream of tokens alone does not demonstrate task progress.

See [Agent Manifests and Role Bindings](codex-profiles.md) for configuration and
[Lifecycle Model](../architecture/lifecycle.md) for execution behavior.
