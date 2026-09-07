# Architecture Overview

Greatminds combines a filesystem task workflow with a durable ACP execution
runtime. Queue location describes a task's workflow state. The daemon owns agent
sessions, admits eligible work and invokes shared deterministic domain services.

- The effective schema defines roles, queues, transitions and evidence gates.
- `coordination/execution.yaml` maps role bindings to ACP agent manifests.
- `.greatminds/` holds task queues, workflow journals and runtime state.
- `.greatminds/.runtime/` records run identities, pinned contracts, admission
  decisions, results, commands and recovery information.
- Agents perform work and submit typed results. Domain services validate the
  assignment, revision and evidence before applying a transition.

An ACP prompt ending is an execution outcome. Task verification requires an
accepted domain result and the schema's evidence gates. The daemon also performs
mechanical dependency readiness, blocked-task resumption and reconciliation
without spending an LLM turn on polling.

## Recovery boundaries

Queues remain inspectable while the daemon is down. They do not describe every
in-flight operation: run records, transition intents, journals, command receipts
and the deployment ledger provide additional recovery evidence. Interrupted work
with uncertain side effects requires explicit resolution; restarting the daemon
does not automatically replay it.

Use `greatminds run status`, `greatminds run events`, `greatminds wake-check` and
`greatminds watchdog` to inspect the combined state. See the
[execution contract](execution-contract.md) for atomicity, idempotency and
workspace limits, and the [operations runbook](../operations/runbook.md) for
operator actions.

## Contract sources

The installed package schema is authoritative. An explicit
`GREATMINDS_CANON_DIR` can select an alternate canon. The project file
`.greatminds/schema.yaml` is a diagnostic mirror, not a policy override. Setup
creates a missing mirror and preserves an existing one.

`greatminds project schema --json` reports the source, version, SHA-256 identity,
document and mirror status. `greatminds project schema --check` exits 2 for a
missing, unreadable or different mirror. Review differences against
`greatminds project schema` before replacing the mirror.

Runs retain pinned schema and execution contracts. Restart the daemon after a
package/canon update; do not infer that an in-flight run has adopted new policy.
When schema and prose disagree on mechanics, the effective schema wins.
