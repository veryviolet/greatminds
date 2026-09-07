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
