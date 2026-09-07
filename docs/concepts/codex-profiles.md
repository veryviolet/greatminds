# Agent manifests and role bindings

Configure all harnesses through `coordination/execution.yaml`. An agent manifest
identifies the ACP executable, arguments, version labels, environment references
and capability requirements. A role binding selects that manifest and defines
the role, workspace, scheduling, model, mode and permission policy.

One agent can serve several roles, and several bindings can use different agents
for the same role. The daemon uses the same ACP lifecycle for each binding and
builds context from the assigned task and pinned schema. Role-specific review
and evidence gates still apply when two roles share a harness.

## Model, mode and permissions

Set model and mode on the binding when needed. The ACP runtime checks support
and applies those selections to the session. Unsupported selections produce an
explicit failure; they do not silently choose another model or transport.

Use `permission: ask` for explicit operator choices. A background turn needing a
choice enters a waiting state. Harness-side permissions and workspace access
also matter: ACP orchestration is not an operating-system sandbox.

## Authentication and environment

Install and authenticate the harness or adapter using its own supported
procedure. Declare required environment references in the manifest and supply
the values through the daemon's configured environment. Credentials do not
belong in task context or execution YAML.

Greatminds setup does not generate per-role Codex homes, inject native app-server
instructions, or copy machine authentication. A configured account name groups
concurrency and startup backoff within this project; it does not select or infer
a provider identity.

See the [first project guide](../getting-started/first-project.md) for a minimal
manifest, the [execution contract](../architecture/execution-contract.md) for
configuration and recovery rules, and the
[compatibility matrix](../architecture/acp-compatibility.md) for the scope of
actual harness validation.

## Explicit account limit signals

An optional manifest `account_limit_errors` array maps documented, exact ACP
application error codes to shared admission holds. It is empty by default. For
example, **only if your adapter documents these example codes**:

```yaml
account_limit_errors:
  - code: 42901
    kind: rate_limit
    cooldown_seconds: 60
  - code: 42902
    kind: quota_exhausted
```

Up to 16 unique signed 32-bit codes are accepted outside the reserved JSON-RPC
range (-32768 through -32000; [JSON-RPC error specification](https://www.jsonrpc.org/specification#error_object)). Authentication, internal, invalid-request and other
reserved errors cannot be reclassified. A rate-limit cooldown is an integer from
1 to 86400 seconds; a quota hold has no automatic expiry. These codes are not
standard ACP quota codes and are not HTTP statuses extracted from error text.
[ACP error handling](https://agentclientprotocol.com/protocol/v1/overview#error-handling)
uses the JSON-RPC error envelope; this mapping is explicit adapter configuration.

The reporting run's pinned manifest determines meaning. Holds affect all bindings
with the same `account` label in this project, including subsequent prompts in
open conversations. They survive restart and configuration edits. Other projects
and account labels are independent. Set consistent account labels yourself; the
daemon does not inspect credentials to infer account ownership. Read the
[recovery procedure](../operations/runbook.md#retries-and-account-admission) before
resolving a hold. No built-in harness mapping or provider availability is implied.
