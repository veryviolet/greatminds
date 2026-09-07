# Daemon and agents

Every managed agent uses the common ACP stdio client. Manifests in
`coordination/execution.yaml` declare executable argv, tested version labels,
environment references and capability requirements. Role bindings choose an
agent, workspace, model/mode, permission policy, scheduling and concurrency.
The [execution contract](execution-contract.md) specifies those fields.

The installed schema defines task queues and gates; `.greatminds/schema.yaml`
is a generated mirror. `greatminds project schema` prints the effective schema.
Runs pin schema/configuration identities so changed contracts cannot silently
reinterpret an in-flight result.

## Daemon ownership

`greatminds coordd` loads the execution contract and reconciles durable state.
Queue-scheduled bindings receive concrete task revisions. Claims are persisted
before launching the ACP process. A task cannot be independently claimed while
an active run or unresolved domain operation holds it.

The ACP client negotiates capabilities, initializes or loads a compatible
session, sends the compiled prompt, receives updates and handles supported
callbacks. Required authentication or operator input is represented as a waiting
state. Permission policy is explicit; missing permission is not automatic approval.
Cancellation is bounded and can escalate to process-group termination.

The daemon and shared domain services own mechanically decidable operations:

- Validate and reconcile typed result receipts, rejecting stale or wrong-run work.
- Resume blocked tasks when declared dependencies and readiness gates permit it.
- Record configured command execution and validate evidence freshness.
- Schedule authorized stand operations and reconcile durable deployment records.
- Recover recorded operations without replaying uncertain non-idempotent effects.

ACP prompt completion is distinct from a verified workflow transition. Completed unchanged revisions remain held. Startup transport failures before
prompt dispatch have bounded exponential retries under the
[execution contract](execution-contract.md#bounded-startup-retries).
`run retry RUN_ID` permits an explicit further attempt after its cause is
resolved. Startup delays are shared by account; provider rate-limit handling
and broader no-progress policies remain tracked in the
[modernization plan](modernization-plan.md).

## Conversations and frontends

`greatminds chat create BINDING_ID` creates a daemon-owned conversation. `chat talk`
handles user messages, streamed output and permission choices. Reconnecting reads
durable events without replaying completed prompts. Task-bound conversations
claim the actual task revision; standalone conversations do not create fake tasks.

Tmux hosts daemon/operator surfaces. VS Code and Cursor IDE targets generate ACP
workspace process tasks, and the VS Code extension calls the same CLI interfaces.
Native agent TUIs, synthetic keystrokes, sleeping-child signals and vendor-native
app-server drivers are not execution transports in Greatminds.

## Operator diagnosis

```bash
greatminds project execution
greatminds daemon doctor --json
greatminds agent status
greatminds run status
greatminds run events --follow
greatminds wake-check --json
greatminds watchdog
```

Configuration checks are static. Run observations distinguish authentication/input
waits, lifecycle failures and process identity/liveness. A live PID alone does
not demonstrate task progress. Dependency inspection uses the daemon's shared
maintenance service and never moves files itself.

`run pause` stops new dispatch while allowing active runs to finish. Use explicit
cancellation when active work must stop. Systemd supervision is optional; the
[runbook](../operations/runbook.md) documents service installation, configured
environment layering and updates. Runtime observations do not require a model
turn or a cloud service.
