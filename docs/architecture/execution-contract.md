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
commands:
  unit-tests:
    argv: [python, -m, pytest, -q]
    roles: [DEVELOPER, TESTER]
    cwd: .
    timeout_seconds: 600
    max_output_bytes: 1048576
    environment_revision: "1"
    purpose: validation
```

This is a configuration example, not an available agent distribution. Actual
adapter versions and compatibility evidence belong in the integration matrix.
The version strings identify the intended installation; parsing configuration
does not verify the executable's version or negotiated protocol capabilities.

Assigned context includes `cli_argv`: the daemon's Python executable followed by
`-I -m greatminds.cli.main`. Agents use that invocation for scoped commands and
result submission. It works without an activated venv or a `greatminds` executable
on PATH and prevents a project-local Python module from shadowing the CLI.

`argv` is an array, never a shell expression. Environment entries map child
variable names to parent variable names. Secret values are resolved at launch
time and do not enter the configuration fingerprint or saved contracts.
An optional agent `auth_method` selects one authentication method advertised by
the ACP server. The supervisor calls it before creating or loading a session;
an unknown method fails explicitly. Omitting it leaves authentication to the
server's existing session behavior. Authentication-required responses retain a
waiting state. Selecting a method can require interactive login according to the
server; the daemon does not silently select another account or method.
For task kinds requiring a worktree in the effective schema, the daemon prepares
the task worktree before starting ACP. A `workspace: .` binding uses that task
worktree; an explicit workspace must be inside it. Existing worktrees are checked
against the expected branch and repository. Other task kinds use the configured
workspace directly. The resolved path and starting Git identity are recorded in
the run, and supplied to the agent's context and permission policy.

Bindings choose workspace, scheduling, permission, session, model, mode, and
account independently of the harness. Unknown fields and unsupported values
fail validation. Workspaces are explicit trusted local paths and must exist
before a claim can be created.

## Operator permission requests

An ACP callback that requires input creates a durable entry in the `permissions`
section of `greatminds run status`. The run enters `waiting_input` while its
process and pending prompt remain alive. Inspect and answer the exact request:

```bash
greatminds run permission REQUEST_ID
greatminds run permission REQUEST_ID --option OPTION_ID
```

Select an advertised `allow_once` or `reject_once` option ID. The service rejects
invented choices and persistent grants. It verifies the supervisor, live process,
session, deadline, and task revision before accepting and consuming the answer.
Repeated identical answers before consumption are idempotent; conflicting or late
answers fail. The agent receives the answer in its existing callback; no new LLM
turn is created. Assigned agents cannot use the operator CLI to approve themselves.

The wait is bounded by the callback's 300-second deadline and the remaining prompt
budget. Timeout leaves an input hold. Cancellation or restart closes unconsumed
requests, including an answered request whose response was not yet delivered.
Recovery preserves the run's input hold and never replays an operator decision.
Inspect the hold and explicitly retry when appropriate; stale request IDs cannot
authorize a new run. Consumed means handed to the callback, not proof of tool success.

Permission records retain the tool description, inputs, locations, and offered
options for review in private runtime state. Known assigned environment values,
the run credential, sensitive field names, and bearer tokens are redacted. This is
best-effort redaction of agent-provided content, not a general secret detector.
Arbitrary vendor metadata is excluded.
Requests exceeding 256 KiB fail explicitly instead of silently truncating details.

The `ask`, `deny`, and `allow-workspace` policies govern ACP permission requests.
They cannot intercept tools that an executable runs without requesting permission.
Harness approval mode, cached configuration, and sandbox restrictions must also be
configured and tested. See the [measured permission behavior](acp-compatibility.md).
The shared-filesystem deployment remains a cooperative trust boundary.

## Configured commands and evidence

An explicit latest plan with `stand_required: false` selects local validation:
the scope-specific stand probes and stand observations are not required. Required
tests blocks, recorded command evidence where configured, task readiness, review,
and source/environment freshness remain enforced. A missing or ambiguous stand
declaration does not waive the existing stand-observation checks.

An assigned agent requests `greatminds run command unit-tests --request-id ID
--wait 60`. The CLI queues a durable request for the daemon; it never executes
the configured command itself. `--wait` only waits for a receipt and does not
cancel on expiry. Inspect it later with `greatminds run command-status ID`.
One unresolved command is allowed per run. Reusing the same request ID for the
same run/command returns the existing receipt; a different request is rejected
until the previous one finishes. Request IDs never cause implicit replay.

Commands are pinned with the run's execution contract. They specify literal
argv, allowed roles, relative cwd, timeout, output limit, environment references,
and an environment revision. There is no interpolation or implicit shell.
Like agent manifests, commands accept `environment` and `required_env` names.
A `purpose: deployment` or `purpose: publication` command additionally requires
`authorized: true` in project configuration; a validation exit code grants no
deployment or publication permission. Configured commands are trusted project
code, so these labels describe project policy rather than sandboxing commands.

Before exec, the daemon records the intent and gated process identity. Each
receipt retains argv/cwd, task/schema/config identities, start/end times, exit
status, Git commit and source hash before/after, an environment HMAC, executable
hash, and bounded stdout/stderr artifact paths/hashes. The HMAC key remains a
private runtime file; environment values do not enter snapshots. Output files
are private local artifacts (mode 0600), not embedded in operator JSON.

Git source identity includes tracked and nonignored files, their contents,
permissions, and internal file symlink targets. Runtime metadata and Git internals
are excluded. Without Git, source files are scanned directly. External symlinks
and directory symlinks are not accepted as reproducible source evidence. Respect
Git ignore rules for generated output; keep result JSON outside the source tree.
An input change during a command makes its successful exit stale. A later source,
environment, executable, or output artifact change also invalidates its evidence.
`environment_revision` must change when relevant external services or installed
dependencies change; local hashes alone cannot identify a remote stand's state.
Stand/lease-bound command scheduling remains a subsequent integration.

Typed results can reference completed request IDs in `payload.command_evidence`.
A tests block may instead specify `command_request_id`; the daemon supplies
`test_command`, `test_result`, and provenance from the receipt. When validation
commands are configured for that role, tests blocks must reference a recorded
command and cannot invent a command/result pair. A recorded nonzero exit supplies
`test_result: fail` for a gated handback; a timeout or uncertain execution cannot
pretend to be a completed test. Test files, adequacy, observations,
readiness, stand gates, and review decisions still pass existing domain validators.
A command's success never moves a task to verified on its own.

Cancellation cleans up the command group before completing the run. Restart
cancels requests that never passed the launch gate. A process recorded without
a final outcome becomes `needs_recovery` after process cleanup and holds task
retry/result application. No command is replayed automatically. After inspecting
its effects, an operator can use `greatminds run command-resolve ID --reason TEXT`
to acknowledge uncertainty. This produces no passing evidence and does not run
anything; authorizing another task run is a separate explicit retry operation.

## Dependency maintenance

For ACP projects, the daemon resumes dependency-ready tasks without an agent
turn. `greatminds wake-check --json` and `greatminds run status` expose the same
versioned findings: ready, waiting/missing dependencies, conflicting terminal
location, malformed/duplicate identity, cycle, withdrawal, required live-role
hold, readiness failure, or an unresolved operation. No repeated heartbeat or
reviewer queue scan is required. Unchanged findings generate no repeated events.

Dependencies must name an existing task in their declared terminal queue. An
active task, an archived task when verified was required, or an ambiguous identity
cannot silently satisfy the condition. Markdown task files require conversion to
typed YAML before automatic resumption. The
graph detects entire cycle components without a recursion-depth limit. A required
live ACP role needs a recorded running process with a matching OS identity;
configuration alone is not proof that a role is usable. An unavailable explicit
target context remains a hold.

The effective schema separately authorizes `system_transitions.resume_dependencies`.
SYSTEM is not an agent role and cannot be selected in a binding. The system
operation uses only the latest blocked block's `resume_to`, requires an active
destination, and preserves schema scope/readiness gates. Withdrawn work still
requires a semantic decision. The original blocking author is retained in the
system journal rather than impersonated as the actor of the resume.

Before moving the task, the daemon locks the task and dependency identities in
stable order, rechecks their revisions, and records an intent with the pinned
schema and dependency evidence. Claims and CLI mutations hold while the operation
is incomplete. Recovery finishes a recorded move/journal once; changed evidence
or conflicting task contents produce `needs_recovery` without overwriting files.
The same task revision and dependency evidence cannot resume repeatedly if the
task reappears unchanged in the blocked queue.

An operator requests reconciliation with `greatminds run repair --operation ID`.
This rechecks the original preconditions; it grants no override. If an intent has
not moved its task, `--abandon --reason TEXT` can cancel that intent while preserving
the source. Changed dependency evidence can then create a fresh intent. An intent
whose task may already have moved must be reconciled, not abandoned. Agent-scoped
credentials cannot request these operator actions.

Projects without an ACP execution contract receive an informational wake report.
Within ACP, both manual domain decisions and automatic maintenance use the shared
terminal-dependency and declared-destination checks.

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

## Scoped context and command output

`greatminds run contract` returns the current assignment with accepted decision
fields and daemon-generated block fields. `--schema` returns the schema pinned to
that run, even if the current canon has since changed. Both require the assigned
run credential.

`run command` and `run command-status` include a bounded `output_preview` for
stdout and stderr (default 8192 bytes per stream; `--output-limit` accepts 0–65536).
The daemon checks the recorded file location, regular-file type, size, and hash
before returning text. Previews are not persisted into receipt metadata and do
not replace source freshness checks. Assigned credentials can inspect only their
own run's command receipts; an operator without assigned credentials can inspect
all receipts.

## Lifecycle and result receipts

`greatminds run submit --json '{"decision":"no_change","payload":{"reason":"..."}}'`
accepts an inline decision (up to 65536 bytes). Pass JSON as one literal shell
argument. `--file /absolute/path/result.json` remains available for larger input;
exactly one input option is required. Both accept a compact
decision: `{"decision": "no_change", "payload": {"reason": "..."}}`. The CLI
fills run/task/schema identity from the current assignment and chooses one stable
result ID per run. Exact repeated submission returns the existing receipt, even
after completion; changing its contents is a conflict. Full envelopes and explicit
result IDs remain supported, but cannot override the assigned run's identity.

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

`received` means durably accepted for domain validation. After the agent stops,
the result service validates all candidate evidence blocks, role permissions,
scope routing, readiness flags, and required gates through the same policy
functions used by the CLI. Validation uses the pinned schema and assigned
workspace in an explicit context; process-wide cwd and role environment are
not changed. `applied` acknowledges the legal domain operation. A completed
agent turn by itself does not complete a task.

An accepted operation records candidate task bytes, source/destination, artifact
hashes, and worktree actions before modifying the task. Recovery checks these
identities, resumes atomic writes/moves, and deduplicates the journal entry.
New blocks retain the decision's role and carry run/result/revision provenance;
the application event names `SYSTEM` as actor and records the deciding role
separately. CLI task mutations and new claims are held while an operation is
incomplete, including when the task is addressed through a short ID.

The four decision payloads are:

- `handoff`: `to_queue`, an array of typed `blocks`, optional `artifacts` and
  `reason`. Block entries contain `kind` and their evidence fields.
- `blocked`: `reason`, `dependencies`, `resume_to`, and optional `artifacts`.
- `needs_input`: `question` and optional `artifacts`; records the question
  without changing queues.
- `no_change`: optional `reason` and `artifacts`; records a terminal decision
  without repeating a model turn for the same revision.

Author/timestamp/provenance fields are assigned by the domain service. Artifact
paths must identify existing files within the assigned workspace. Invalid
decisions become `rejected` without partial task changes. An uncertain merge
or conflicting recovery state becomes `needs_recovery`; it is never treated
as permission to rerun a possibly completed command. Operator repair for these
states and further deterministic command/evidence services remain in progress.

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

`coordd` now selects the common ACP supervisor when the project has
`coordination/execution.yaml`. Projects without that file still use the existing
execution path until the migration milestone. `coordd --once` reconciles and
executes one dispatch batch in an ACP project, then exits. A continuously running
daemon also processes operator cancellation and retry requests.

The supervisor holds one exclusive project lease. Before exec, a child waits
on a pipe until its PID, boot identity, and process start time have been saved.
EOF at this gate means the supervisor died before authorizing launch. Restart
cleanup checks process identities and uses Linux pidfds, through Python or libc,
before recording interruption. Unknown process state is an error, not permission
to dispatch a second agent. Authentication and input holds survive restart.

Dispatch selects concrete tasks from each queue binding's declared role queues.
Idle, paused, capacity-limited, and already-attempted revisions create no agent
turn. Blocked dependency sweeps are reserved for the mechanical controller.
The context compiler provides the assigned task, role, available transitions,
contract identity, and result format. It removes queue-scanning and heartbeat
instructions from the assignment. Compatible sessions may be loaded when the
agent advertises that capability; otherwise context is reconstructed in a new
session and the strategy is recorded. Model/mode settings must be advertised.

Operator commands:

- `greatminds run status` returns runs and assignment reasons as versioned JSON.
- `greatminds run pause` and `resume` control new ACP dispatch.
- `greatminds run cancel RUN_ID` requests bounded cancellation from the daemon.
- `greatminds run retry RUN_ID` authorizes one further attempt after a terminal
  or waiting run. A pending domain result must be resolved before retry.
- `greatminds run submit --file /absolute/path/result.json` receives a decision
  using the run credential supplied by the supervisor. CLI output reports the
  durable receipt status; it does not report a task transition.

The daemon records context bytes, protocol update count, activity time, elapsed
time, session strategy, stop reason, and structured failure codes. It does not
classify failures from keywords in model output. A successful turn with an
unchanged task revision is held until an explicit retry, rather than being
automatically repeated. Typed results now pass through domain application and
its semantic gates. Worktree preparation at dispatch, deterministic command
evidence, repair controls, and live harness compatibility remain in progress.


## Stand state durability and deployment ownership

Stand state uses a stable `.stand/state.lock` and an atomic, fsynced replacement
of `.stand/state.yaml`. Concurrent readers see complete snapshots. An exception
or process death before replacement preserves the preceding lease and history.
All participating writers must use this locking protocol; stop older processes
before a live upgrade. This change does not migrate a running fleet.

Both operator and coordinator deployment calls hold `.stand/deployment.lock`.
A second call fails before external execution and does not consume coordinator
retry attempts. Success, failure, and stale-source rejection can update the
stand only while its preparing state and captured lease still match. A late
result raises a recovery error without overwriting a replacement lease. Retry
exhaustion also checks the captured lease before marking the stand down.

The deployment lock alone does not resolve external effects after process death.
Durable deployment intent, child-process recovery, and ACP daemon stand scheduling
remain required before automatic crash recovery or replay is enabled.
