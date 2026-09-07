# Lifecycle Model

A role defines responsibility and workflow authority. A binding defines how that
role runs. Configure bindings in `coordination/execution.yaml`; every binding
uses the common ACP client regardless of harness.

## Admission and turns

| Binding scheduling | Source of work | Between turns |
| --- | --- | --- |
| `queue` | Eligible tasks in the role's queues, subject to admission and evidence gates | Daemon observes state; no agent polling turn is required |
| `on-demand` | Explicit operator conversation input | Conversation persists; queued messages are consumed by the daemon |

Interactive conversations have durable IDs and ordered user messages. CLI,
tmux and IDE frontends attach to daemon-owned conversations. Detaching a
frontend does not submit a new prompt or cancel the current turn.

For task execution, the daemon atomically claims a task revision, pins contracts,
opens or loads a compatible ACP session, applies selected model/mode settings,
and submits context. ACP permission requests follow the binding policy. An
operator decision can place the run in `waiting_input`; authentication problems
can produce `waiting_auth`. Cancellation requests receive bounded cleanup.

Session reuse is governed by the binding's session policy and negotiated agent
capabilities. Changing harnesses does not transfer their private conversation
history; the assigned task and pinned contract supply portable context.

## Deterministic supervision

The daemon handles queue observation, dependency readiness, admission limits,
startup backoff and result reconciliation in code. Role metadata such as
`driven` or `interactive` in the schema does not select a native
transport, tmux wake sequence or provider-specific timer loop.

Recognized pre-prompt startup failures can retry within configured budgets.
Account backoff is shared across bindings with the same configured account.
Completed background turns without workflow progress are bounded by
`max_no_progress_turns`, which defaults to one. Token output and task metadata
changes do not count as workflow advancement.

A completed prompt does not verify a task. Typed results pass through the domain
service; command and deployment effects retain their own evidence and recovery
requirements. Unknown post-prompt failures are not automatically retried.

See the [execution contract](execution-contract.md) for precise retry eligibility,
session compatibility, permission and recovery behavior.
