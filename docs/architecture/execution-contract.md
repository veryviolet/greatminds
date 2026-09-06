# Execution contract and durable run store

The modernization runtime separates a role from its ACP executable. Its
versioned configuration lives in `coordination/execution.yaml`. Inspect it
without launching an agent using `greatminds project execution` (or pass an
explicit `--config` path).

```yaml
version: 1
max_running: 4
account_limits:
  local-account: 2
agents:
  example-acp:
    transport: acp
    argv: [example-acp, --stdio]
    adapter_version: "1.0.0"
    harness_version: "1.0.0"
    environment:
      API_KEY: EXAMPLE_API_KEY
    required_env: [EXAMPLE_API_KEY]
bindings:
  implementation:
    role: DEVELOPER
    agent: example-acp
    workspace: .
    scheduling: queue
    permission: ask
    session: resume-if-compatible
    account: local-account
    max_running: 1
    timeout_seconds: 1800
```

This is a configuration example, not an available agent distribution. Actual
adapter versions and compatibility evidence belong in the integration matrix.
The version strings identify the intended installation; parsing configuration
does not verify the executable's version or negotiated protocol capabilities.

`argv` is an array, never a shell expression. Environment entries map child
variable names to parent variable names. Secret values are resolved at launch
time and do not enter the configuration fingerprint or saved contracts.
Bindings choose workspace, scheduling, permission, session, model, mode, and
account independently of the harness. Unknown fields and unsupported values
fail validation. Workspaces are explicit trusted local paths and must exist
before a claim can be created.

## Persistence and ownership

The runtime service stores a versioned snapshot in `.runtime/state.json` beneath
the project's runtime directory. It contains project identity, dispatch pause
state, runs, ordered lifecycle events, and result receipts. Immutable schema and
execution documents live in `.runtime/contracts/`. A run retains the content
hashes of both documents, its role/binding, task revision, workspace, account,
and supervisor identity. Contract documents are synced before the referencing
run is published.

A claim takes the task lock before the store lock, verifies the discovered task
revision, and checks project, binding, and account capacity in one transaction.
One task can have only one active run. Waiting runs still consume capacity.
Task revision includes queue path and file bytes, so a queue move or task edit
invalidates an outstanding decision. Task files remain the domain authority.

Metadata writes use a same-directory temporary file, file fsync, atomic replace,
and directory fsync. An interrupted replacement leaves the snapshot from before
or after the transaction; a malformed existing snapshot fails closed. Filesystem errors after
replace can leave a committed write with an uncertain acknowledgement, so
callers must inspect state before retrying. Run identifiers and receipt identities
make recovery possible without inferring success from the last console output.

The shared task lock keeps a stable inode after release. File existence is not
proof of ownership; the kernel flock is. Removing a lock file can strand existing
waiters on a different inode and break mutual exclusion.

## Lifecycle and result receipts

Run lifecycle records claimed, starting, running, waiting for input/authentication,
cancelling, and terminal completed/failed/cancelled/interrupted states. The
supervisor supplies a unique event ID. Replaying the same event is a no-op;
reusing its ID for different contents is an error. Terminal runs reject new
lifecycle transitions. Pausing dispatch prevents new claims while allowing
active runs to finish.

An agent receives a random per-run credential. The store persists its hash and
omits it from operator snapshots. A result envelope names result, run, task,
task revision, schema hash, decision, and decision payload. Role and provenance
come from the run. A result for another task/revision, an invalid credential,
or an inactive run is rejected. Each run accepts one decision; identical
re-delivery returns the original receipt even after the task moves.

`received` means durably accepted for domain validation. It does not mean
applied, approved, tested, or verified. Similarly, a completed agent turn does
not complete a task. Domain gates and atomic result application are introduced
in the domain-completion milestone.

This is a cooperative shared-filesystem deployment. Run credentials prevent
accidental or invalid submissions through the service, but do not isolate a
process that can read or rewrite the authoritative store. Strong isolation
requires a daemon-owned store and an actual execution boundary.

## Delivery status

The common stdio transport uses the upstream
[Python ACP SDK](https://agentclientprotocol.github.io/python-sdk/quickstart/),
pinned to `agent-client-protocol==0.11.1`. The SDK supplies protocol models,
framing, request correlation, and bidirectional dispatch. Greatminds creates
the child process separately so it can own a process group and enforce bounded
shutdown of descendants as well as the agent itself.

Protocol fixtures cover initialization, session creation, streamed updates,
permission requests during pending prompts, unsupported session loading,
incompatible versions, disconnection, timeouts, and process-group cleanup.
These fixtures use an independent JSON-RPC peer, not the SDK's agent classes.
They establish client behavior, not live harness compatibility.

Filesystem and terminal callbacks are currently unadvertised and explicitly
rejected. Permission requests use a supplied policy/operator handler, with a
deadline and validation against the offered option IDs. Without a handler,
the client cancels the request and reports that input is needed. The transport
does not persist raw updates or stderr; the supervisor must apply diagnostic
redaction and retention when connecting its event sink.

The contracts and store are implemented and tested independently. Existing
launch/daemon paths have not yet been cut over to this store. The common ACP
supervisor will own process lifetime, restart reconciliation, callback policies,
and capabilities; the filesystem store does not guess process death from a
missing heartbeat. Until that integration lands, configuring this file alone
does not change execution behavior.
