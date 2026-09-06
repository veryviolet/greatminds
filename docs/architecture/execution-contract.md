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

Before external dispatch, the engine persists a deployment attempt in
`.stand/deployments.json`. It records lease identity, owner process identity, and
lifecycle without copying arbitrary lease fields or raw logs. The returned exit
status and log hash are saved before publishing the stand transition. That
transition includes the attempt ID; only its exact match can recover a missing
final receipt after a crash, without executing the command again.

An attempt with an unknown outcome, or a returned result without a confirmed
stand transition, blocks further external deployment across lease changes and
process restarts. `greatminds stand deployment-status` exposes these attempts as
versioned JSON. Executor exceptions retain an unresolved intent; coordinator
retries cannot bypass it. A malformed ledger also blocks execution.

Managed profile execution starts a fresh Linux session through an exec gate.
The child PID, boot ID, start ticks, process group/session, command digest, and
working directory are durably recorded before the gate opens. Failure to record
identity closes the gate without executing the external command. Normal exit,
exceptions, and timeouts use the common bounded process-group cleanup. An omitted
timeout defaults to 1800 seconds for managed deployments. The returned process
status is recorded separately from the domain result. Internal attempt metadata
is excluded from Ansible extra variables.

`stand deployment-recover ATTEMPT_ID` holds the project deployment lock, cleans
the tracked group, and records cleanup without resolving external effects.
`stand deployment-resolve ATTEMPT_ID --reason TEXT` requires confirmed cleanup,
rechecks group absence, and records an operator assessment. It neither marks a
stand ready nor manufactures a successful result; the existing stand state is
preserved. A separate authorized deploy is required to execute again. Assigned
ACP run credentials cannot use these operator decisions.

Attempts predating gated child tracking cannot claim automatic cleanup. Process
group cleanup covers the owned Linux session, not remote changes or processes
that deliberately leave that group. Those external effects require operator
assessment. The ACP daemon sweeps orphan attempts automatically, respecting the
same deployment lock; unchanged cleanup is not repeatedly written. It reconciles
recorded stand transitions before cleaning uncertain attempts and never starts
an agent or replays an external command for this sweep. `run status` includes the
same versioned deployment ledger. Local workflows without a stand ledger create
no stand state. Authorized profile dispatch uses the scheduler described below;
changing a lease alone never resolves an unknown outcome.


## Mechanical stand lease expiry

The ACP daemon checks leases without creating a maintainer turn. A finite,
positive TTL and a valid grant time must prove expiry. Reclamation also requires
no active run for the holder role or task, no remaining tracked process group,
no unresolved task command/result, and no unresolved deployment. Live or unreadable
holder PID registry records keep the lease held. Invalid or uncertain state does not
establish an expired, dead holder.

Reclamation holds the deployment lock, runtime store lock, and stand state lock
in that order. It rechecks preconditions under these locks before publication,
preventing a new claim or deployment from racing the decision. A single atomic
stand update clears the expired lease, records `SYSTEM` history, and promotes
the FIFO head using the existing lease policy. A repeated sweep makes no further
transition. Projects without stand state create none.

`run status` includes `stand_lease` with the current expiry or blocking reason.
Manual `stand reclaim` uses the same TTL and ACP ownership checks and respects
the deployment lock and unresolved deployment ledger. Granting/deploying the
next lease uses the separate authorized profile scheduler below.


## Bounded deployment output

Managed deployment stdout and stderr are drained concurrently with a default
capture limit of 1 MiB per stream. `stand deploy --output-limit BYTES` accepts
1–67108864 bytes per stream. Retained prefixes are written atomically to private
`.stand/deployment-output/ATTEMPT_ID/stdout` and `stderr` files. The ledger records
stream byte counts, full stream hashes, captured byte counts/hashes, file paths,
and truncation flags; raw output does not enter the JSON ledger.

A truncated log cannot pass profile result/no-host checks. The process return
code and bounded artifacts remain available, but the attempt requires operator
assessment and does not mark the stand ready. Output capture limits do not stop
draining pipes or hide an incomplete log behind a successful return code.

The process runner also accepts a cooperative cancellation event for daemon
workers. Cancellation stops the owned process group and preserves external
uncertainty. Once the main process exits, remaining group members are cleaned
without waiting for inherited output pipes to hit the deployment timeout. Output
pipes remaining open beyond bounded cleanup raise a recovery error. Internal
`_deployment_*` metadata is excluded from playbook variables.


## Authorized ACP daemon stand scheduling

Automatic profile deployment is opt-in in `coordination/execution.yaml`:

```yaml
stand:
  authorized: true
  profiles: [smoke-only, full-deploy]
  timeout_seconds: 1800
  max_output_bytes: 1048576
```

Omitting `stand`, or setting `authorized: false`, disables automatic deployment.
The profile list must be explicit and distinct. This permission does not replace
registry role restrictions or per-lease approval for profiles requiring explicit
user approval. Registry/profile-file consistency is checked again at dispatch.

A dedicated worker runs the existing deployment engine while the daemon continues
processing agents, command requests, and operator controls. Dispatch pause stops
new profile selections; existing work may finish. Shutdown signals cooperative
cancellation and drains the worker before releasing the supervisor lease.
`coordd --once` waits for its selected stand deployment as well as agent work.

Before external work, `.stand/schedule.json` records a ticket keyed by the complete
lease snapshot and immutable stand policy. Selection is serialized against pause,
deployment ownership, and run claims. The engine rechecks the captured lease before
using it. A failed or interrupted selection is not automatically replayed for the
same lease/policy, even when no external deployment receipt was written. After
correcting a preflight issue, an operator may invoke `stand deploy` explicitly;
unresolved external effects still require deployment recovery/resolution first.

`run status` exposes `stand_dispatch` and the versioned `stand_schedule`. Failed
tickets retain exception type and known policy error text with environment-value
redaction; arbitrary exception text and process output are not copied there.
Existing bounded capture, exec gating, orphan cleanup, and no-replay rules remain
in effect. Managed profile inputs are checked as described below.


## Managed deployment input identity

Before launching a managed profile, the engine records the workspace source
identity, selected profile path/content hash, executable identity/content hash,
and an HMAC of the process environment plus effective playbook variables. The
shared command-evidence primitive owns the private HMAC key; environment values
are not copied into the deployment ledger. Workspace identity includes uncommitted
source and uses Git ignore rules, or a file walk for non-Git fixtures. Runtime
metadata is excluded so logging and bookkeeping cannot invalidate deployment.

After normal command completion, the engine records another input snapshot.
A mismatch leaves the attempt unresolved and cannot publish `ready`, even when
the process returned zero. The actual process result and bounded output remain
available for assessment. Failure/timeout paths lacking a complete snapshot pair
do not assert stable inputs.

Domain transitions, `gate-check`, and manual `stand ready` revalidate the latest
receipt for the cited lease. The receipt must belong to the task, have an applied
successful outcome and matching input snapshots, and retain complete stdout/stderr
artifacts whose paths, sizes, and hashes still match. An unresolved later attempt
cannot fall back to an earlier successful one. Monotonic ledger sequences order
attempts independently of wall-clock timestamps. Manual ready also requires the
same deployed lease fields and holds the deployment lock while publishing state.

Current source, profile, executable, effective variables, and environment identity
must match the receipt. The ledger stores only the public deployment context needed
to reconstruct variables; PROJECT.env values remain outside it. Managed playbooks
and validators use the same environment normalization: run ID/token, role/project
context, shell working-directory bookkeeping, and terminal presentation variables
are omitted; ANSIBLE_FORCE_COLOR is fixed to zero. Other environment changes can
conservatively invalidate evidence, including differences between caller shells.

`stand.environment_revision` is a nonempty string, defaulting to `"1"`. Change it
when relevant external services or installed infrastructure change. This is a
declared revision, not remote-state discovery. These checks cannot attest remote
machine state or detect transient changes reverted between snapshots. Projects
with neither execution.yaml nor a deployment ledger use marker/evidence checks;
ACP projects require a
managed receipt and never fall back to a marker or prose.

## Daemon-owned interactive conversations

For a new ACP project, `greatminds setup --project-dir PATH --execution-config FILE`
validates and installs the supplied execution contract, creates schema queues,
adds runtime/worktree ignore entries, and writes a missing runtime schema mirror.
The contract is published last. Repeating the command preserves existing files;
an edited schema mirror is reported as drifted and does not override the installed
schema. This path does not launch an agent, install services, change harness trust,
or generate native harness configuration. It rejects flags from the other setup
path that it cannot honor and refuses replacement of an existing execution contract.

An existing fleet without an execution contract requires explicit migration before
this bootstrap can apply. Preparation of that migration and the default setup switch
are in progress; the option above is the available ACP-specific bootstrap entrypoint.

`runtime.interactions.ConversationStore` backs the `chat` CLI and ACP daemon.
Each conversation has a private `.runtime/conversations/<id>/state.json` and a
stable lock. The binding, execution/schema hashes, and workspace are pinned at
creation. Prompt text belongs to this private conversation journal, not the global
run/event snapshot. Consumers must normalize and redact assistant text before
appending it; arbitrary tool payloads are not accepted by the text API.

Caller-supplied request IDs make enqueue retries idempotent. Reusing an ID with a
different prompt fails. A durable sequence establishes FIFO order independently
of JSON key order and clock time; only one turn may be running per conversation.
Cancellation removes a queued turn from dispatch. Cancelling a running turn records
intent, and the daemon must complete ACP cancellation before recording its outcome.

A new supervisor owner interrupts already-started turns without re-enqueueing them.
Queued input and the provider session ID survive. The caller must hold the exclusive
project supervisor lease before acquiring ownership; this store does not itself
assert process liveness or implement session loading. Changed contracts require a
new conversation rather than silently rebinding pending messages. The supervisor
loads the saved provider session when reopening; missing ACP load support fails
the turn with `session_load_unavailable` instead of silently replacing history.
History updates emitted by session loading are not duplicated in the output feed.

Reconnect readers page events by monotonically increasing cursor without changing
execution state. Limits are explicit: 64 KiB per prompt, 64 pending turns, 256 turns
per conversation, and 1 MiB/4096 text events of captured output per turn. Exceeding
output capture emits one truncation event while allowing the daemon to finish the
turn. New prompts exceeding limits are rejected without deleting earlier input.
Conversations use the same Supervisor, gated ACP transport, permissions broker,
configured-command service, run cancellation, and project/binding/account admission
limits as queued work. They reserve a run slot while the session remains connected.
Dispatch pause prevents new conversation runs; an already connected conversation
can continue accepting user messages. Continuous coordd keeps that session open
between messages; `--once` drains its available messages and closes it. Interactive
requests are considered before new background assignments, without preempting an
existing background run. No user prompt is injected into a background task session.

Any configured role binding can start a conversation without creating a workflow
task. Its run uses a namespaced conversation subject identity, a pinned role context
and the binding workspace. It cannot submit a domain result for a nonexistent task.

`chat create BINDING --task EXACT_TASK_ID` instead pins the current file/revision of
one workflow task. The binding role must be permitted to claim its queue. Dispatch
uses the same task lock, worktree policy, compiled context, commands, and domain
result service as background work; the two paths cannot claim that task together.
A changed/moved task holds queued input with a stale-revision explanation. The
revision is checked before each prompt. No message is silently rebound to new task
contents; create another conversation for the current revision when appropriate.

A received task result ends the conversation's current run after the prompt ends,
including in continuous daemon mode, so result application can proceed after
process cleanup. Waiting for a permission with a live process does not qualify as
finished execution. Subsequent queued messages retain their identities and remain
subject to the pinned revision check. Domain transitions and worktree cleanup are
still performed by the shared result service, not by the terminal client.

With ACP coordd running for the project:

```sh
greatminds chat create BINDING
greatminds chat send CONVERSATION --request-id MESSAGE_ID --message 'User request'
greatminds chat attach CONVERSATION --follow
greatminds chat attach CONVERSATION --after LAST_CURSOR --follow
greatminds chat attach CONVERSATION --text --follow
greatminds chat talk CONVERSATION --after LAST_CURSOR
greatminds chat interrupt CONVERSATION MESSAGE_ID
greatminds chat close CONVERSATION
```

Each command accepts `chat --project-dir PATH` before its subcommand. `attach`
streams JSON pages with reconnect cursors; Ctrl-C detaches without cancelling work.
`interrupt` cancels queued input or requests active ACP cancellation, closing that
run's connection. `close` immediately rejects new messages, cancels queued input,
and requests daemon cancellation of the active run. The daemon acknowledges closure
after run cleanup; the journal remains readable and follow readers exit at closure.
Closure works even if the binding/configuration has changed and no longer permits
dispatch. Repeated close requests are idempotent; closed conversations cannot reopen.
Without a running daemon, closure stays requested until the next daemon sweep.
Permission questions remain visible in `run status`; inspect or answer one using
`run permission REQUEST_ID --option OPTION_ID`. Assigned agent credentials cannot
use chat operator controls.
Admission holds appear in the conversation's dispatch events/status; queued input
survives changed configuration or capacity holds. Known environment secrets are
redacted from normalized assistant chunks on a best-effort basis; this is not a
guarantee against secrets split across chunks or unrecognized secret values.

`attach --text` renders messages and outcomes, then prints a reconnect cursor.
`talk` reads the conversation, accepts one message at a time, streams its response,
and presents pending one-time permission options for an explicit numbered choice.
It never chooses a permission by default. `/detach`, EOF, or Ctrl-C leave work
running; `/close` requests the daemon closure described above. A reconnect command
with the last displayed cursor is printed on exit. During a response, another
client can enqueue further messages or use `chat interrupt`; talk waits for queued
work before asking for its next message. Terminal control characters in user and
assistant text are escaped rather than executed, including across stream chunks.

`chat bindings` and `chat list` expose metadata for interface clients without
launching agents or including message contents. The VS Code extension uses these
commands for New ACP Chat, Attach ACP Chat, and Close ACP Chat. Its terminal runs
the same `chat talk` client. The daemon remains the process/session owner when an
editor terminal closes or reconnects.

Richer non-permission input, real extension-host smoke testing, and live interactive
harness validation remain outstanding M5 work.

## ACP launch surfaces

When `coordination/execution.yaml` exists, `greatminds launch --project-dir PATH`
uses the ACP frontend. `--target vscode` or `cursor-ide` writes
`greatminds-acp.code-workspace` with process tasks for coordd, run status, and
conversation metadata. It preserves unrelated workspace tasks/settings and does
not modify the user's `.vscode/tasks.json`. Open the workspace and use the ACP chat
extension commands for role conversations. Workspace generation itself starts no
agent or daemon. The workspace must retain the project as its first folder.

`--target tmux` creates a project-specific session with a coordd window and an
operator shell. It sends no synthetic keystrokes and starts no native harness TUI.
The daemon dispatches according to execution.yaml. Repeated launch reports an
existing tmux session; it does not kill or replace it. `--recreate` is refused for
this path; stop the daemon and close its session explicitly when needed. The
supervisor's exclusive lease remains responsible for preventing duplicate daemon
ownership, including when a service already runs coordd.

The frontend uses the current CLI's Python, or an explicit `--venv` whose ACP
runtime imports are checked before launch. `--config` does not select another
configuration for an ACP project; use its project directory. A project without
an execution contract still uses the other launcher while default migration is
being completed. Generating an ACP workspace does not remove pre-existing fleet
artifacts; that belongs to the explicit project migration.
