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
The harness must preserve this launch’s scoped environment in its tool subprocesses.
A separately running shared harness backend can lose that identity; use an attached
local backend for managed runs. The [Cline compatibility configuration](acp-compatibility.md#cline-after-native-login-2026-09-07)
records one measured instance and its native setting.

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

Bindings choose workspace, scheduling, permission, session, model, mode, reasoning, and
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
session and the strategy is recorded. Model/mode/reasoning settings must be advertised. Reasoning uses the ACP
`thought_level` select category and is applied after model selection, using the
updated options returned by the server.

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

## ACP defaults and cutover

Operator observations use the same ACP run model across `run status`,
`agent status --json` and `dashboard --json`. Configured bindings without runs
are idle; active runs and the latest terminal outcome remain visible, including
runs whose binding was subsequently changed or deleted. Run lifecycle, recorded
process identity/liveness, pinned task revision and configuration drift are
separate fields. A live PID does not turn waiting for authentication or human
input into readiness, and PID reuse does not identify a live owned process.

`agent tools --json` lists configured ACP manifests and pinned version labels.
Its `verification: configured` field does not assert live harness compatibility.
The VS Code agent list displays the same distinction. Dashboard includes task
queues, assignment reasons, stand state and maintenance findings. These read-only
views exclude run credential hashes and do not probe harnesses or inspect terminal
panes. Cross-store observations are best effort; execution admission revalidates
the state before launching or applying a result.

Tasks that explicitly require live roles can resume only when the target context
has a running ACP role with a matching live process identity. Missing roles,
unreadable observations and authentication/input waits hold that explicit gate.
Tasks without a declared live-role requirement do not acquire one implicitly.

`greatminds setup` initializes an empty explicit ACP execution contract and shared
runtime directories. Supplying `--execution-config FILE` initializes from reviewed
manifests and bindings instead. Setup preserves existing contract bytes and task
data, and does not configure harness authentication or install services/plugins.
It refuses to create a second runtime if task data still resides in coordination.

`coordd` always uses ACP. A missing or invalid execution contract is an error.
`launch` exposes daemon/operator frontends. Use `run pause`, `run resume`,
`run cancel`, `run retry` and the `chat` commands to control work and conversations.

ACP run cancellation, recovery, permission checks and task revision validation
remain part of the runtime contract. Follow the modernization plan for the current
implementation status of diagnostics, service configuration and documentation.

### Explicit stand profile selection

When a lease has a worktree, its `coordination/stand-profiles/` profile wins;
otherwise the main project's selected profile is used. The chosen path and
source are recorded with deployment evidence. Files under the package's
`templates/stand-profiles/` are examples, never automatic runtime substitutes.
To evaluate an updated example, explicitly copy or adapt it into the selected
project/worktree profile and review that change. Setup preserves existing
profile bytes; package upgrades do not infer permission to replace a profile
from its content hash. Existing deployment authorization, safety and evidence
freshness checks still apply.

### Bounded startup retries

Queue bindings accept `max_startup_retries` (0–20, default 2),
`retry_initial_seconds` (positive integer, default 5) and `retry_max_seconds`
(positive integer, default 60). The delay doubles after each failed startup and
is capped at the maximum. Zero retries disables automatic repetition.

Only durable `failed` outcomes explicitly recording `prompt_started: false`
and `pre_prompt_activity: false` with a startup transport failure or timeout
qualify. Session updates, permission callbacks or extension notifications before
the prompt prevent automatic replay. Missing executables,
permission errors, authentication/input waits, configuration/protocol failures,
crash-interrupted runs and unknown or post-prompt effects require explicit
operator action. A recorded command or typed result also prevents automatic
startup retry. The same task revision and pinned execution/binding/schema
identities must still match. A contract change does not silently authorize replay.

Retry time and counts are derived from durable run outcomes; restarting the
daemon does not reset them. The scheduler exposes `retry_backoff` and
`startup_retry_limit`, and `run status` includes each assignment's retry time and
budget. Claim admission rechecks the verdict under the store lock and records
`startup_retry_dispatch` when an automatic retry is admitted. Existing capacity,
permission, revision and unresolved-operation gates still apply.

An explicit operator retry permits one further attempt but does not reset the
count for that unchanged revision. This startup policy does not implement
provider-reported rate-limit handling, automatic post-prompt retries, or provider usage/cost budgets; those remain separate modernization requirements.

### Shared account startup backoff

Project-level `account_retry_initial_seconds` (default 5) and
`account_retry_max_seconds` (default 60) are positive integers. Consecutive
recognized startup failures across all bindings, tasks and conversations using
the same `account` produce a shared exponential delay capped at the maximum.
New tasks and explicit operator retries wait too. Other account identities are
independent. Account identity is a configured grouping, not an inferred provider
or credential identity.

The delay is derived from persisted completion times and survives daemon restart.
The first transition to a configured session/running state records immutable
`startup_ready_at`, which resets the connectivity failure streak at that original
time. Permission resumes and crash recovery do not create a newer reset. A run
without this marker supplies no reset evidence. This demonstrates startup
recovery only; it does not establish task progress or passing evidence. Existing
runs are not cancelled, and authentication holds retain their separate policy.
`run status` exposes `accounts` with failure count, next admission time and source
run. Queue scheduling and atomic claims enforce the same delay; blocked chat
admission leaves the user message queued.

This controller handles recognized startup failures. Provider-reported rate
limits, cross-project account coordination and an operator-resettable circuit
breaker are not implemented by this delay. Per-revision automatic retry limits
still bound repeated attempts of one unchanged task.

### Bounded turns without workflow progress

Bindings accept `max_no_progress_turns` (1–20, default 1). Completed background
turns in the same task queue count until an applied `handoff`/`blocked` receipt
or an intervening queue stage establishes workflow progress. Changes only to
task-file bytes, token streams, process liveness and daemon restarts do not reset
the count. Interactive user-paced conversations are not automatic background
continuations and do not consume this counter.

At the default, a completed prompt without a transition holds the task with
`no_progress_limit`. A larger explicitly configured budget allows a subsequent
normal `turn_ended` attempt after `retry_initial_seconds`, provided the pinned
contracts still match and no command/result/uncertainty gate prevents it. Failed
or cancelled post-prompt work is not automatically replayed. Human-input and
no-change results remain explicit holds, even if their application edited task
metadata. All existing capacity, account, permissions and revision gates apply.

`run status` exposes a `progress` verdict and count per assignment. Admission
rechecks the limit atomically and records `no_progress_continuation` for each
continuation of an unchanged revision. Operator retry authorizes one additional
attempt but does not erase the counter. An operator who changes task content
within an exhausted queue stage must explicitly retry it or perform an authorized
workflow transition. This is a workflow-progress budget, not an estimate of code
quality or a detector of meaningful text changes.

## Reported usage observations

Run metadata `usage` retains the latest SDK-decoded `usage_update` context and
cost sample, and the latest prompt response's token sample, each timestamped.
Context `used`/`size` describes occupancy, which may decrease after compaction.
Optional cost describes reported cumulative session cost and its currency; it is
not a billing statement. Missing and invalid reports are explicit statuses, not
zero. Raw metadata is discarded. A timestamped sample alone does not establish
current or cross-run completeness; cost continuity has a separate durable record.

The pinned SDK schema describes prompt token usage both as per-turn and cumulative.
The [upstream ambiguity report](https://github.com/agentclientprotocol/agent-client-protocol/issues/1860)
documents the conflict. Token samples therefore have `scope: unknown`; the daemon
neither sums successive samples nor adds potentially overlapping token categories.
These are SDK-decoded values, not a claim that wire values escaped SDK coercion.

Bindings may configure both `max_reported_session_cost` (a positive finite number)
and `reported_cost_currency` (three uppercase letters, for example `USD`). The
limit applies to reported cumulative cost for the same agent manifest, workspace
and ACP session ID, including loaded sessions. No currency conversion, price
estimate, billing claim or automatic provider/model switch is performed.

This is a reactive limit: cost at or above the threshold cancels an active prompt
through ACP and holds the run with reason `reported_usage_budget`. A new session
may make one bootstrap prompt before a cost report is available; that prompt can
already exceed the limit. A completed prompt must have a fresh cost report for
budgeted work to continue. Without one the run is held as `cost_unavailable`.
This is not a hard spending guarantee or a pre-purchase affordability check.

The cost accounting record marks a prompt pending before ACP sends it. A crash or
uncertain cancellation preserves that uncertainty across session loading. Unknown
prior history, invalid cost, counter regression or currency changes cannot reset
the budget: the affected session remains held. Reported cost and expected currency
must match. An operator may explicitly choose a new session or revise budget
configuration; retry alone does not erase the ledger. Disabled cost budgets do
not introduce an execution gate, but observations and uncertainty are retained.
These controls do not remove previously accepted domain results.

## Bounded client input

Each binding accepts positive integer `max_prompt_bytes` (default 262144) and
`max_session_input_bytes` (default 1048576). The first limits one complete
UTF-8 text prompt, including compiled context and interactive user input. An
oversized first prompt fails before launching the ACP process. Later interactive
messages are checked before sending. Text is never silently truncated.

The session limit bounds cumulative client-submitted UTF-8 text for the same
agent manifest, workspace and ACP session ID in the project store. Each send
reserves its bytes durably before calling ACP. Repeated context counts each time
it is sent. Reservations survive process/daemon interruption and session loading;
an uncertain send retains its debit. Store-level reservation retries with the
same identity are idempotent. Starting a distinct session creates a distinct
input budget; the runtime does not silently discard a conversation to do so.

A failed limit produces `input_budget_exceeded`. Run outcome `input_budget`
identifies prompt/session budget, limit, used and requested bytes. A known older
session without sufficient reservation evidence reports `session_history_unknown`
with `used_bytes: null`; it is not treated as empty. Operator remedies include
reducing the requested input, explicitly configuring a larger budget, or starting
a new conversation with deliberate context. Existing conversation compatibility
rules still apply to configuration changes. Background retry policy does not
automatically retry input-budget failures.

These are client-input limits, not tokenizer counts, model context-window bounds
or billed cost. Agent output, internal tool context and provider-side compaction
are not measured by these byte counters. `context_bytes` describes the compiled
base context; `input_bytes_reserved` and `session_input_bytes_reserved` describe
input reservations. Detailed reservation events contain counts and IDs, not raw
prompt content. Separate provider usage counters remain unknown unless reported.

## Durable run-stage timing

Each run's `timings` records the first observed offset, in seconds from the
start of that supervisor execution. Offsets use a monotonic clock and are written
immediately with `run_stage_observed` events. Recovery preserves observed values;
it does not invent a completion time for a crashed run. Missing stages mean
unobserved, not zero latency. Values are available in the common run snapshot.

| Stage | Observation boundary |
| --- | --- |
| `workspace_ready` | Workspace preparation returned |
| `context_ready` | Base context was built or supplied |
| `process_recorded` | Launch-gate process identity was durably recorded |
| `protocol_ready` | ACP initialize and protocol-version validation returned |
| `session_ready` | Session creation/loading and model/mode configuration succeeded |
| `first_prompt_started` | Input reserved; supervisor begins the first prompt call |
| `first_protocol_activity` | First delivered update, permission or extension event |
| `first_prompt_activity` | First such event after a prompt call began |
| `cleanup_complete` | Transport, permission and command cleanup returned |

Subtract observed boundary offsets to inspect preparation and startup costs.
`outcome.elapsed_seconds` covers the execution including cleanup. These offsets
do not include time in the queue before a claim or measure provider inference
in isolation. The launch gate can precede actual harness exec. Prompt-start time
is a client boundary, not proof the provider received the request.

A loaded session may emit history before a new prompt, so protocol activity and
prompt activity are distinct observations. Neither means useful task progress,
first user-visible text, adequate validation or an approved domain result.
For a multi-message interactive run these are first-occurrence observations,
not a per-message latency distribution. Unknown token/cost data remains outside
this timing contract.

### Queue, validation and accepted transitions

The daemon records the first observation of each currently configured queued task
revision. A claim copies that observation into `queue_observation`, including
`wait_seconds`. Scope `observed_revision_wait` is a lower bound: it cannot recover
time before the daemon saw an externally created or edited task. File modification
time and user-supplied task dates are not substituted for arrival time. Unchanged
observations are idempotent; the active candidate index drops absent revisions,
while run copies survive recovery and event pruning.

An applied domain result also records the destination revision and its acceptance
time atomically. The next claim has scope `accepted_transition_wait`: its queue
interval starts when the preceding transition became accepted by the daemon.
These scopes remain distinguishable in measurements and exports.

Result receipts retain a bounded `validation` record for the latest preparation
attempt: start/completion timestamps, attempt count, validity and monotonic
duration. A crash leaves completion/duration unknown. Retrying preparation starts
a new attempt; recovering an already prepared operation preserves its measurement.
This measures `_prepare` checks, not every later live gate recheck or validation
shell command. Declared commands retain their own execution records.

Receipt `timings` records application start, resolution and elapsed wall time.
Resolution can be applied, rejected or needs-recovery; it is not automatically
successful completion. These intervals can include a recovery delay. Applied
handoff/block transitions additionally give the run a `domain_progress` record
with acceptance time and claim-to-transition duration. Rejected results, no-change
receipts and mere prompt completion do not invent accepted transitions. This is
observable domain progress, not a claim about when the model began useful reasoning.
Duplicate application leaves timestamps unchanged. Missing, non-finite or backward
clock intervals remain null. Numeric summaries are included in private bundles.

Conversation turns record queued/start/resolution timestamps, queue wait and
start-to-resolution duration. Cancelled queued messages have no execution duration.
After a crash, resolution is when interruption was observed, not an inferred time
of process death. Old turns without observations keep unknown intervals. These
fields describe wall-clock boundaries and can overlap other phase measurements;
they must not be blindly summed as independent work durations.

### Measuring productive orchestration

`tools/productive_benchmark.py` runs a deterministic ACP fixture through the real
developer → tester → reviewer pipeline. It asserts three applied results, three
successful daemon commands, six independent test cases, merge of only the intended
file, worktree cleanup and unchanged runs/results/commands after restart. A supplied
plan and fixture agent isolate orchestration from inference. Setup and final
independent checks are outside the timed three-stage pipeline.

Run the same script with independently installed wheel environments, for example:

```sh
/tmp/before-env/bin/python tools/productive_benchmark.py --repeats 3 --output /tmp/before.json
/tmp/after-env/bin/python tools/productive_benchmark.py --repeats 3 --output /tmp/after.json
```

The default load is 50 idle conversations and 100 cancelled run records, preserved
throughout productive execution. Reports include package/schema/dependency identity,
fixture and script hashes, per-role observations and read counts for the daemon
process's RunStore. They exclude subprocess reads and conversation-file reads.
Editable source execution requires `--allow-source` and is marked accordingly;
canon/project/Python import overrides are rejected by the benchmark CLI. An output
path must be new. Temporary projects remain available for inspection.

The [dated six-run comparison](evidence/productive-pipeline-2026-09-07.json) used
separate wheels from 3b3db47 and 4961016, identical dependency/schema/fixture inputs,
and alternating runs. Median daemon runtime reads decreased from 1145 to 355
(69.0%). Observed median pipeline wall time was 17.097 s versus 16.567 s (3.1%
lower). Three samples per version on one host are descriptive, not a general speed
guarantee. Both versions are modernization checkpoints; this does not measure the
original pre-modernization architecture. Other hardening and instrumentation
changes occurred between them, so individual latency effects are not isolated.
Token usage and model quality are not measured. Small context-byte differences
include the different interpreter paths and are not claimed as prompt optimization.

### Measuring idle daemon overhead

The checkout includes `tools/daemon_idle_benchmark.py`. Run it with the project
Python environment to create a temporary fixture, warm up maintenance, and measure
five complete idle daemon passes. Defaults are 50 open idle conversations and 100
cancelled claims. All journal entries are created through store APIs;
the benchmark rejects agent startup and verifies unchanged runtime history.
`--conversations`, `--history`, and `--repeats` select the synthetic load.

The [recorded local comparison](evidence/daemon-idle-2026-09-07.json) measured
67 runtime reads per pass before conversation indexing and 18 after. The median
was 1.105 s before and 1.008 s after on this host. This includes daemon startup
and reconciliation; it excludes fixture construction and warm-up. It is not an
LLM latency or task-completion benchmark. The daemon now indexes live conversation
runs from one observation per pass, while every claim still validates current
capacity and identity under the authoritative lock. No journal data is dropped.

### Runtime event retention

`max_runtime_events` is a project-level integer from 100 to 1,000,000, default
10,000. The daemon installs this policy under its exclusive supervisor lease.
All runtime writers enforce the persisted policy in the same atomic transaction
as their changes; no periodic model turn or extra per-tick scan is needed. Before
an explicit daemon policy is installed, the default limit applies.

On overflow the event tail shrinks to approximately 80% of the limit and records
an `events_pruned` event. This headroom avoids pruning on every subsequent write.
`event_retention` in the runtime snapshot records the configured bound, cumulative
discarded count and last discarded sequence. Run and event sequences remain
monotonic across pruning and restart. A failed atomic write leaves both the
operation and the previous event history unchanged.

`run events` emits a synthetic `events_gap` record before its normal event page
when `--after` predates the retained tail. Its sequence is the last discarded
sequence; `data` includes the requested cursor and first available sequence.
The gap record is additional to `--limit` and is not itself appended to the
journal. Clients should show the missing-history notice, use current snapshots
for authoritative state, and continue from the emitted sequence. Diagnostic
bundles mark events as truncated when history was pruned, even when the complete
remaining tail fits the export limit.

This policy bounds event count, not total project disk usage. Task files, run
identities and idempotency receipts, immutable contracts, command/deployment
receipts and their evidence, and conversation history remain intact. ACP stderr
capture and per-command/per-conversation output already have separate bounds.
The optional systemd service's external journal follows the host's journal policy.

### Managed protocol diagnostics

Each run can retain a `protocol` record independently of the event-log tail:

- `negotiated`: numeric protocol version and an allowlist of capability flags;
- `tool_events`: at most 64 tool call/update observations with timestamps, phase,
  run-scoped hashed tool reference, tool kind/status and content/location counts;
- `tool_events_truncated`: explicit saturation indicator, without pretending that
  missing tool observations did not occur;
- `error`: last numeric JSON-RPC code (when present), bounded exception category and
  the stage that failed;
- `stop`: last normalized stop reason.

Phases distinguish preflight, initialization, authentication, session creation or
loading, configuration and prompting. Repeated identical facts do not append new
records. Once the tool trace fills, the supervisor stops writing further tool
samples; aggregate update counts still include all received updates, and terminal
error/stop facts can still be recorded. These observations do not approve tools
or substitute for typed results and command evidence.

Server `_meta`, tool IDs in clear text, titles, tool arguments/results, locations,
assistant text and raw exception messages/stderr are excluded from this record.
The permission broker retains its separately scoped callback identities needed to
answer approvals. Conversation text remains in the separate conversation journal.
Diagnostic bundles export a revalidated compact protocol summary, not the tool
payloads. Run-level protocol records survive event-tail pruning and crash recovery.

The common client drains preceding session updates before returning either a
successful prompt response or a JSON-RPC error, with a bounded wait. A failed
observation sink cannot silently report successful processing.
