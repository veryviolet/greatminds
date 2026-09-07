# Greatminds modernization plan

Date: 2026-09-06. Status: implementation in progress; see Progress below.
Baseline and evidence: [initial audit](rehabilitation-audit-2026-09-06.md).
Current requirement-by-requirement status: [acceptance audit](modernization-acceptance.md).

Scope clarification from the owner (2026-09-06): there are no legacy installations
and no production deployment. A live-fleet migration, dual transport compatibility,
and rollback to native drivers are therefore not delivery requirements. Cut over
directly to ACP defaults and remove obsolete execution paths and setup artifacts.
Preserve project task data. Historical migration checkpoints below record work
already done, not a requirement to build more migration infrastructure. Continue
testing crash recovery of actual ACP runs and domain operations; that remains part
of the product's reliability contract.

## Agreed destination

1. All Greatminds-managed agent execution uses ACP, including interactive roles.
2. The daemon and shared domain services perform all mechanically decidable
   orchestration. LLM turns perform work that needs reasoning or judgment.
3. Any supported harness can fill any product role that its negotiated
   capabilities satisfy. Role, harness, model/provider, scheduling policy,
   workspace, and permissions are independent configuration dimensions.

The user also requested a third workstream: recommended product and architecture
improvements grounded in the code/product audit. Track C below records those
recommendations separately from the two agreed execution principles.

The initial audit considered permanent native transport exceptions. This plan
supersedes that suggestion: old drivers exist only during migration. An agent
without a suitable upstream ACP endpoint needs an ACP bridge at the edge, or
remains explicitly unsupported until one exists. The scheduler never acquires
another per-harness protocol branch.

## Shared foundation

- Repair the seven daemon-install test failures caused by writes into the real
  user's systemd configuration. Isolate configuration, captured environment,
  binaries, and service operations without copying real credentials to fixtures.
- Resolve one effective schema snapshot for the daemon, CLI, and generated
  agent context. Pin its version/hash to each run; define update behavior for
  in-flight work and explicit migration for existing project copies.
- Extract filesystem/FSM operations into shared services behind the existing
  CLI. Preserve task IDs, queue semantics, evidence, and operator workflows.
- Define durable identities for project, task revision, role binding, run,
  session, and result. Task identity must survive a harness/session replacement.
- Keep the filesystem store initially. Introduce atomic claims, journaled
  operations, and recovery rules where needed, rather than changing storage
  while replacing execution.

## Track A — ACP execution

### A1. Agent manifests and role bindings

Replace the static tool flags with versioned launch manifests: executable/argv,
adapter version, compatible harness version, environment references, auth
preflight, configuration mapping, and required/optional capabilities. Use argv
arrays rather than shell command interpolation.

Role bindings select an agent manifest, workspace, model/provider options,
permission policy, scheduling policy, concurrency limits, and session policy.
Configuration validation identifies unsupported combinations before dispatch.
No silent fallback to another model, identity, or transport.

Deliverable: effective configuration/doctor output and a compatibility matrix
that distinguishes documented, protocol-tested, and live-tested support.

### A2. One ACP client

Implement a shared stdio client with protocol negotiation, request correlation,
authentication flow, session creation, optional session loading, prompt turns,
streamed updates, cancellation, and shutdown. Select a maintained Python SDK
after checking its coverage against the selected protocol version; do not
reimplement the protocol simply to avoid a dependency.

Implement permission requests and only the filesystem/terminal callbacks the
client advertises. Resolve paths to the run's workspace, enforce configured
policy, preserve tool/permission IDs, and handle requests while a prompt is
pending. An unattended run needing human input becomes an explicit waiting
state; it does not hang indefinitely or auto-approve everything.

Store normalized lifecycle events alongside enough raw protocol evidence for
diagnostics, with sensitive data excluded. Distinguish transport failures,
agent stop reasons, cancellation, waiting for input, and domain completion.
Optional extensions belong behind negotiated capabilities.

### A3. First real integrations

Use the existing upstream bridges for [Codex](https://github.com/agentclientprotocol/codex-acp)
and [Claude](https://github.com/agentclientprotocol/claude-agent-acp), plus one
native ACP implementation such as Qwen Code or Kimi CLI. Pin tested versions.
Run the same tasks through the same client, including permissions, tool events,
restart, session behavior, and cancellation. Do not assume ACP transfers
conversation history between different harnesses; rebuild task context when
changing harnesses.

Deliverable: one mixed-agent pipeline and reproducible compatibility evidence.

### A4. Remaining harnesses

Add Qwen, Kimi, Grok, Cline, Gemini, OpenHands, and Cursor to the compatibility
campaign. For each, verify the actual ACP distribution and version, auth,
session support, worktree behavior, model options, and cancellation behavior.
Reuse upstream bridges where available. If a bridge must be built, keep it
outside the core client/scheduler and run the same ACP conformance scenarios.
The existence of an ACP entrypoint alone does not establish supported status.

### A5. Operator interaction and removal of legacy execution

Provide a Greatminds chat/attach surface backed by daemon-owned ACP sessions.
Interactive planner and live developer roles must support user prompts,
streamed output, permission/input requests, reconnect, and explicit interrupt.
Serialize background work and human prompts per session with a documented
policy; neither can silently overwrite or discard the other.

Tmux and VS Code may display this client, dashboard, and logs. They no longer
host native agent TUIs that the daemon controls through synthetic keystrokes.
Detach/reconnect must not duplicate or silently terminate the running turn.

After parity tests, remove the direct Claude
subprocess driver, Greatminds' Codex app-server implementation, generic
headless drivers, and TUI wake/sleep signaling. Remove their obsolete units,
setup artifacts, and documentation. No installed native fleet needs migration.

Track A is complete when every supported execution path, including interactive
roles, uses the common ACP client and no legacy driver is required.

## Track B — deterministic orchestration

### B1. Durable run supervision

Extract run lifecycle from `cli/coordd.py`. The daemon owns claims, dispatch,
timeouts, cancellation, retries/backoff, concurrency, and process groups.
Persist run state before launching work. Use run identity and task revision to
reject stale completions. Reconcile on startup and periodically; filesystem
events are wake hints, not the only record that work exists.

Separate process liveness, protocol activity, and actual task progress. A live
process or token stream alone does not prove progress. Cancellation escalates
to bounded process-tree termination when ACP cancellation cannot finish.
The OS supervisor restarts the daemon itself.

Retries must distinguish transient failure from missing authentication or
required human action. Repeated successful turns with unchanged task state
must enter a bounded no-progress policy. Account/provider limits must not
cause every role sharing that account to retry independently without bounds.

### B2. Mechanical maintenance and dependency resolution

Move periodic sweeps, dead-process restart, recoverable stale-run cleanup,
expired dead-holder lease handling, log retention, and notification deduplication
into controllers with explicit preconditions and audit events.

Automatically resume blocked tasks when their declared dependencies and gates
are satisfied. Detect missing/impossible dependencies and cycles deterministically;
request a semantic decision only when the dependency declaration needs changing.

Reduce MAINTAINER to diagnosing unknown infrastructure failures and planning
repairs. Routine healthy operation and known recovery paths need no LLM turn.
An operator-triggered maintenance conversation remains available like any role.

### B3. Assigned work and compiled context

The scheduler selects and claims concrete work. A context builder supplies the
role's responsibilities, task, relevant artifacts, permitted domain operations,
applicable gates, expected result, and effective contract version. Expose more
context on demand through the same domain API.

Remove instructions to scan all queues, reread the complete schema every tick,
emit artificial heartbeats, sleep, rearm a loop, and rediscover mechanical
workflow steps. Supply immutable context on session start and explicit updates
when tasks/contracts change. Resume only when the session remains compatible;
otherwise reconstruct from durable task state.

Measure prompt/context size, time to useful work, model turns per transition,
and no-progress turns against the baseline.

### B4. Typed results and deterministic transitions

Define a harness-neutral result envelope, submitted through a shared operation
available to agents via CLI and, where appropriate, MCP. It records run/task
revision, decision kind, artifact references, evidence, and any dependency or
human-input request. Do not parse free-form final prose as authoritative state.

The domain service validates the envelope and applies legal state changes
atomically. Use the same validators from the CLI and daemon. Duplicate delivery
must not append duplicate evidence or repeat a transition. A completed ACP
prompt alone never marks a task verified.

Introduce an explicit system actor for mechanical transitions, retaining the
agent/reviewer decision and its provenance. Preserve semantic approval gates,
ownership, and human approvals already required by project policy.

### B5. Workspaces, command execution, and evidence

Move predictable worktree preparation/cleanup, configured validation commands,
stand lease scheduling, and deployment/profile invocation into services.
LLMs still select or propose changes to plans/tests where reasoning is needed.

Record executed argv, cwd, task revision/commit, timestamps, exit status,
stdout/stderr artifacts, and stand/lease identity automatically. Invalidate
evidence when the evaluated revision or relevant environment changes.
Test adequacy and product correctness still require substantive evaluation.

Distinguish idempotent operations from commands whose completion is uncertain
after a crash. Persist intent and reconcile before retrying non-idempotent work;
do not promise exactly-once shell execution. Keep publication and deployment
authorization explicit in project policy rather than inferring it from rc=0.

Track B is complete when mechanical duties no longer appear as requirements
for LLM turns, and daemon-only tests prove scheduling/recovery/transition logic.

## Track C — recommended product and architecture improvements

Priorities here: P0 is needed for a dependable modernization cutover; P1 should
follow the working core. These are design recommendations, not claims that all
current paths are broken. Implement overlaps with A/B once in shared services.

### C1. Explainable operator state and control — P0

Evidence: `cli/dashboard.py` explicitly infers agent activity from run locks,
heartbeats, and queue ownership. The VS Code extension primarily lists static
tool capabilities and opens terminals (`vscode-extension/extension.js`).

Build an operator view around tasks and runs: assigned work, actual execution
state, last meaningful progress, blocking reason, next scheduled action, retry
count, active revision, and evidence. Distinguish waiting for a dependency,
authentication, human input, capacity, retry time, and a stand lease.

Expose explicit pause/resume dispatch, cancel run, retry, and attach operations.
Specify pause semantics: stop new dispatch, let existing work finish unless
explicitly cancelled. Explain why a task is not running and what would unblock
it. Derive CLI, terminal UI, and VS Code views from the same versioned JSON
snapshot/event interfaces; do not build another state machine in the frontend.

Acceptance: an operator can diagnose representative stalled tasks and act on
them without reading runtime YAML or guessing from a PID.

### C2. Smaller onboarding and selectable pipelines — P1

Evidence: the default roster creates ten agent roles plus observer windows.
The first-project guide combines role setup, marketplace plugins, stands,
Ansible profiles, daemon installation, and tmux before the first task.

Offer a minimal local workflow and explicit presets for fuller review, UI,
documentation, and deployed validation. Keep independent review where the
selected workflow requires it, but do not make every role a mandatory live
participant in every project. Instantiate workers only when useful work exists.
Make stand support and its dependencies optional for workflows that do not
need deployed validation; preserve a clear failure if a required stand is absent.

Acceptance: a fresh local repository completes a representative small task with
one configured harness and no remote stand, while the full preset retains its
review/evidence gates. Measure handoff overhead before eliminating role stages.

### C3. Predictable setup, configuration, and upgrades — P0

Evidence: `cli/setup.py` generates Claude settings, Codex profile sources,
marketplace installs, and git hooks. Existing `coord.yaml` is always skipped;
`--force` does not reconcile it. `cli/update.py` upgrades the package and then
reconciles/restarts using environment-manager-specific paths.

Separate project initialization, dependency/auth preflight, optional agent
extensions, service installation, and versioned migration. Preview effective
changes before application, preserve user customizations, and record ownership
of generated files. Repeated setup must converge without widening permissions
or reinstalling unrelated plugins. Respect existing project hooks.

Keep schema, adapter/harness compatibility, and configuration versions explicit.
Keep configuration changes explicit and reject stale in-flight contracts. Avoid
floating adapter upgrades under live sessions. The initial ACP cutover does not
need a migration or rollback path for native installations.

Acceptance: clean ACP install, repeated setup preserving customizations, and
interrupted run/domain-operation recovery have reproducible local fixtures.

### C4. Resource budgets and useful performance metrics — P1

Evidence: resource wrapping is currently Cursor-specific in `agents/tool_specs.py`;
session reset is based on a Claude turn-count threshold, and headless output is
generally collected after process completion.

Provide project/role/task limits for concurrent runs, wall time, retries,
consecutive no-progress turns, and context growth. Add optional usage budgets
where the harness exposes reliable counters; missing token/cost data must remain
unknown rather than appearing as zero. Distinguish reported usage, estimates,
and actual billed cost. Never silently switch models/providers to meet a budget.

Record queue wait, process startup, protocol handshake, first activity, useful
work, validation, and overall completion time. Compare representative pipelines
before and after modernization; optimize the measured bottlenecks.

Acceptance: a failing provider or unchanged-task loop cannot create unbounded
retries; the operator can see the budget/limit stopping progress.

### C5. Recovery tooling and local diagnostic bundles — P0

Evidence: diagnosis is spread across daemon doctor, watchdog, wake-check, stand
doctor, agent status, task files, and turn logs. Some checks are informational
only. Historical failures repeatedly concern stale locks and stranded work.

Add one aggregate doctor view with structured findings, severity, evidence,
and suggested actions. Reuse existing checks as domain functions. Separate
inspection from bounded deterministic repair; show the repair's preconditions
and record what changed. Support an on-demand local diagnostic bundle with
versions, redacted config, run timelines, and relevant evidence.

Acceptance: a toy project with injected faults yields stable actionable findings;
repair is idempotent and does not consume a model turn. Diagnostic collection
does not require uploading project data or messaging external services.

### C6. Verifiable identity and permission boundaries — P0

Evidence: role identity currently comes from `GREATMINDS_ROLE`, role obligations
also live in prompts, and launchers apply different approval defaults by tool.

Bind domain operations to the assigned run/role/workspace and its policy.
Treat the environment variable as context, not proof of authorization. Ensure
an agent result cannot claim another role's approval or another revision's
evidence. Represent system transitions separately, as required by B4.

Document the trust boundary: a cooperative shared-filesystem deployment does
not isolate agents from raw state mutation. If stronger enforcement is required,
keep authoritative state daemon-owned and expose scoped operations, using an
execution boundary the selected harness can actually enforce. Do not claim
filesystem isolation from a prompt, git hook, or ACP alone.

Acceptance: stale/wrong-run domain submissions and unauthorized role changes
are rejected, while configured legitimate operations work equally across agents.

### C7. Maintainable modules, executable examples, and release evidence — P0

Evidence: `coordd.py`, `task.py`, and `setup.py` are large modules containing
domain logic and CLI plumbing. Plugin documentation describes absent skills.
The packaged feature-plan Markdown template describes an older handoff despite
the CLI enforcing typed YAML task files.

Create clear boundaries for domain/FSM, runtime/supervision, ACP integration,
workspace/stand services, and presentation. Migrate module by module, retaining
compatibility facades only for an explicit transition period. Consolidate schema
loading, version comparison, error types, and duplicated state parsing.

Generate mechanical reference material from validated contracts. Test actual
packaged examples and first-project instructions against a built wheel in an
isolated project. Keep regression tests centered on observable domain/runtime
behavior; add state-machine sequences and crash scenarios rather than preserving
obsolete internal branches just to satisfy tests.

Acceptance: documentation examples execute, shipped artifacts exist, upgrade
fixtures pass, and release notes distinguish offline, protocol, and live-agent
validation. Add a real extension-host smoke test when the VS Code surface changes.

### C8. Bound scope and preserve the product's useful properties — P1

Preserve local operation, inspectable state, explicit evidence, and recovery
without a cloud service. Document the supported host/OS envelope independently
of harness support. Keep deployment and UI integrations behind boundaries so
they do not become prerequisites for the task engine.

Defer a database migration, distributed scheduler, and automatic model selection
until measurements or concrete usage justify them. Use the existing CLI and
VS Code surfaces to prove the workflow first. A local web interface is the
agreed next phase after this modernization plan, as recorded below.

## Next phase — local web workspace

User direction recorded on 2026-09-06: after completing M0–M7, build a convenient
local web interface for managing the product and doing work in it. This is a
follow-on phase; it does not replace or delay the current modernization work.

Initial scope: tasks and queues, agent conversations, approval/input requests,
dispatch controls and cancellation, changes and results, and execution
diagnostics. Refine the interaction design against the completed runtime before
implementation.

The web interface will consume the daemon's versioned operations, snapshots,
and event stream. Task transitions, authorization, scheduling, and session
ownership stay in the shared daemon/domain services. Reconnect must recover
current state and active conversations without duplicating work. Keep the
product local and ensure that running the web interface is optional for CLI
and unattended operation.

## Delivery sequence

| Milestone | Included work | Acceptance evidence |
| --- | --- | --- |
| M0 — trustworthy baseline | Test isolation, effective schema, C7 behavior fixtures | Full offline suite passes; no real user config changes |
| M1 — execution contract | Run/result identities, A1, B1 state model, C6 domain identity | Atomic claims, duplicate events, stale-result rejection, recovery fixtures |
| M2 — common ACP path | A2, A3, B1 execution, C1/C4 event measurements | Same client runs Claude, Codex, and a native ACP agent; cancel/restart work |
| M3 — unattended mechanics | B2, B3, C5 doctor and repair | Idle operation, known recovery, and dependency release invoke no LLM |
| M4 — domain completion | B4, B5, C6 enforcement | Typed handoffs, automatic evidence, preserved review gates, revision checks |
| M5 — role and harness coverage | A4, A5, C1 operator control | Declared role × harness matrix; mixed-fleet and interactive scenarios |
| M6 — complete cutover | Native driver removal, C3 ACP setup, C7 documentation | Every public managed execution path uses ACP; clean install works |
| M7 — product refinement | C2 presets, C4 budgets/optimization, C8 scope review | Simpler first task, measured overhead reduction, documented limits |

A and B share the run contract. They can progress as separate workstreams
after M1, but must not create competing supervisors or task-state authorities.
Track C's P0 requirements are integrated into these milestones; its P1 work
does not delay proof of the core ACP/deterministic execution architecture.
Integrate in small reviewable changes; no whole-project rewrite.

## Verification and success measures

- Test state transitions and scheduling with an injected clock and fake ACP
  server: duplicate/delayed events, disconnects, malformed replies, missing
  capabilities, waiting for permissions, timeout, cancellation, and restarts.
- Test filesystem crash points and subprocess survival with local fixtures.
  Assert durable postconditions, not just constructed argv or method calls.
- Keep live inference tests separate and opt-in. Record pinned harness/adapter
  versions, credentials mode, scenario, artifacts, and observed result.
- Require zero LLM invocations for idle fleets, routine watchdog/restart work,
  bookkeeping, and mechanically satisfied dependency releases.
- Preserve correctness and recovery while reducing context overhead and wasted
  turns. Set numeric latency/token targets after M0 measurements, not by guess.
- Publish upgrade instructions, actual compatibility, failure diagnostics, and
  a reproducible mixed-fleet acceptance scenario before a modernization release.

## Progress

M0 implemented on 2026-09-06:

- Daemon tests isolate user-state paths and strip inherited capture-eligible
  credentials. The seven baseline installation failures are resolved.
- The installed canon is the effective contract. Service selection, task
  validation, daemon role loading, and agent schema output share that source.
- Content-addressed schema snapshots retain their contents across source changes;
  project mirrors are inspected read-only through `project schema --check`.
- Agent bootstrap and operational docs identify the effective contract and
  require process restart after a package/canon update. Per-run schema pinning
  remains part of the upcoming run lifecycle implementation.
- Full regression suite: 1,665 passed, 2 skipped. Wheel and sdist build succeeded.

M1 foundation implemented on 2026-09-06:

- Validated ACP-only manifests and independent role bindings, environment
  references, execution fingerprints, and read-only `project execution` output.
- Durable project/run identities, task revision checks, atomic claims, scoped
  result receipts, event deduplication, pause state, and concurrency limits.
- Schema and execution documents retained by content hash for each run.
- Shared stable-inode task locks close a queued-waiter mutual exclusion race.
- Cross-process claim, stale-result, identity, duplicate event/receipt, capacity,
  failed-write, and contract-reload fixtures exercise the new service.

See [execution contract](execution-contract.md) for semantics and limitations.
M2 transport foundation uses upstream `agent-client-protocol` 0.11.1 for ACP
framing, request correlation, and callback dispatch. Independent wire-peer tests
cover negotiation, streaming, permissions during a prompt, missing session-load
support, incompatible protocol, disconnect, timeout, and process-group cleanup.
The client advertises neither filesystem nor terminal callbacks yet; permission
requests default to cancellation plus an explicit input-needed flag.

M2 supervisor integration:

- `coordd` selects the common supervisor for explicit execution contracts.
- Concrete task dispatch, compiled assignment context, durable launch gate,
  process identity checks, restart cleanup, and session capability checks work
  through the same execution path for all configured roles/manifests.
- Operator JSON state, pause/resume, cancellation, one-attempt retry, and
  credential-bound CLI result submission are available.
- Independent ACP fixtures exercise idle operation, CLI result delivery,
  daemon SIGKILL recovery, cancellation, and suppression of repeated unchanged
  work. No real inference or live fleet upgrade is involved in these tests.
- Verification: full regression run 1,719 passed, 2 skipped; subsequent focused
  fixtures also cover role/queue ownership, compatible session loading, model
  confirmation, and scoped permission decisions. Wheel/sdist build and clean
  wheel dispatch → CLI receipt → restart-hold smoke test passed.

M4 domain-application foundation:

- Typed results are validated and applied by the daemon through shared task
  gates, using scoped pinned schema/workspace context instead of process-wide
  environment changes. Author/provenance fields cannot impersonate other roles.
- Prepared operations retain candidate task bytes and artifact hashes; crash
  recovery resumes file changes and deduplicates evidence/journal entries.
- System application events preserve the originating role/run/result identity.
  Uncertain merge completion and conflicting evidence produce recovery holds.
- The ACP wire fixture now submits a handoff through the CLI and the daemon
  advances it through the domain validators. Fault fixtures cover each durable
  application stage and prove that reprocessing does not duplicate changes.
- Full regression run after domain integration: 1,744 passed, 2 skipped.
  Subsequent targeted checks cover fresh gate validation during recovery and
  background domain processing with explicit worker shutdown. Built-wheel
  smoke verifies ACP submission, gated queue advancement, and restart deduplication.

M4 workspace preparation:

- Before ACP startup the daemon checks the claimed revision, records a workspace
  intent, and prepares the schema-required task worktree. A default `.` binding
  selects that worktree for task kinds requiring isolation; other kinds retain
  their configured workspace.
- Existing worktrees must match the expected branch and Git repository. The run
  retains the resolved workspace and starting Git identity before agent launch.
- Git preparation runs outside the ACP event loop. Cancellation drains it before
  releasing run ownership; Git operations have bounded command deadlines.
- Focused supervisor/store/domain regression: 64 passed. ACP daemon integration:
  23 passed before the additional workspace fixtures. Real harness testing remains
  pending.

M4 configured command service:

- Run-pinned command definitions separate argv, role authorization, workspace,
  environment references, output/time limits, and deployment/publication policy.
- The shared CLI submits requests; the daemon records launch intent/process
  identity and captures bounded output, timing, exit status, source/commit identity,
  executable hash, and environment identity. No interpreter turn runs the mechanics.
- Results and tests blocks can reference the recorded command. Source/environment
  changes invalidate evidence; existing adequacy/readiness/review gates remain.
- Uncertain execution is held after process cleanup and never replayed. Operator
  resolution acknowledges the uncertainty without generating passing evidence.
- Independent ACP fixtures cover request → daemon execution → typed evidence →
  domain application, and SIGKILL during command execution followed by cleanup,
  a durable recovery hold, and no repeated agent/command dispatch.
- Full regression: 1,771 passed, 2 skipped. Subsequent command/domain regression:
  39 passed, including recorded nonzero exits producing gated test handbacks.
  Wheel and sdist build succeeded; installed-wheel ACP command/evidence/restart
  smoke passed. No live model service was used.

M3 context simplification:

- Agents now submit only decision/payload. The shared CLI fills task/run/schema
  identity and a stable per-run result ID. Duplicate delivery is deterministic;
  agents no longer copy hashes or invent delivery identities. Explicit full
  envelopes remain validated for compatibility.
- Store/credential regression: 36 passed after the final identity-error fix;
  the ACP fixtures also submitted compact decisions through the CLI, including
  command evidence and handoff application.

M3 dependency controller:

- ACP domain validation, wake diagnostics, and daemon maintenance share terminal
  dependency semantics and declared resume destinations. The graph reports missing,
  active, conflicting terminal, malformed, duplicate, and cyclic dependencies.
- An explicit schema system operation resumes ready tasks under task/dependency
  locks, retaining pinned schema, dependency revisions, and the blocking author's
  provenance. No reviewer or other LLM turn is created for the mechanical resume.
- Prepared/moved/journaled crash stages recover once. Changed evidence holds;
  bounded operator repair rechecks the original conditions. Abandoning an unmoved
  intent preserves task files and does not turn uncertain work into success.
- Withdrawals, live-role/auth holds, semantic gates, active runs, and incomplete
  operations block automatic movement. Configured roles alone do not prove liveness.
- Shared JSON diagnostics drive `run status` and `wake-check --json`; unchanged
  findings are deduplicated. The reviewer no longer receives routine wake-check
  and mechanical resume duties in the effective schema.
- Full regression: 1,803 passed and 2 skipped, with one documentation-wording
  check subsequently corrected. The final documentation/controller/daemon subset
  passed all 31 tests. Built-wheel smoke covers system resume → ACP dispatch →
  shared operator status with no reviewer turn. No live inference was used.

Remaining maintenance controllers, stand/lease integration, live harness
compatibility, interactive attach, and complete migration/removal of the
existing launch paths remain pending. M2 is not complete until the real
integration campaign and remaining client policies have passed.

M2 real executable campaign started:

- All nine installed/test-isolated harness endpoints (Codex, Claude, Qwen, Kimi,
  Grok, Cline, Gemini, OpenHands, Cursor) accepted ACP protocol 1 initialization
  through the common client. Normal startup access resolved filesystem/OAuth
  restrictions in the initial probes.
- The [compatibility matrix](acp-compatibility.md) records exact installations,
  tested argv, advertised capabilities, explicit Codex binary selection, and
  the remaining session/tool/inference/recovery stages. Test adapters are pinned
  by package definition and lockfile; a reproducible repository probe is included.
- Session creation passed for Codex, Claude, Grok, and Cline. The remaining five
  returned authentication-required before explicit authentication; this does not
  establish that cached credentials are absent. The manifest now supports an
  explicitly selected advertised authentication method before session creation/load.
- Codex 0.153.4 with adapter 1.10.0, Claude SDK 0.3.257 with adapter 0.75.1,
  and Grok 1.0.13 passed the same exact synthetic text response through the shared
  transport. Codex 0.149.1 returned a model-version error as assistant text despite
  `end_turn`; the response assertion correctly failed it. No global CLI was updated.
- Supervisor, runtime contract, probe, and public-document regression: 60 passed.
  The matrix retains separate session/inference evidence. Tools, permissions,
  session recovery, cancellation during inference, and mixed-agent tasks are pending.

M2 session lifecycle campaign:

- Codex 0.153.4, Claude SDK 0.3.257, and Grok 1.0.13 loaded sessions in new
  processes and recalled a synthetic value from the previous turn without that
  value appearing in the second prompt. No cross-harness history transfer is claimed.
- All three acknowledged cancellation with `cancelled` after streaming had begun,
  then exited within transport shutdown. Tool/permission-pending cancellation
  remains a separate required scenario.
- Model and mode selection moved into the common transport. Codex and Claude
  confirmed explicit advertised choices; Grok did not advertise model config
  options in these sessions, so model selection remains untested there.
- A regression fixture exposed asynchronous SDK notification dispatch crossing
  RPC boundaries. The transport now waits for received session updates to finish
  processing before returning session creation/load or prompt completion. This
  prevents historical messages from leaking into the next response and prevents
  response completion from overtaking the last output chunk. Handler failure is
  explicit. Live history/cancellation tests passed again after this fix.
- Focused transport/supervisor/daemon/probe regression: 54 passed. Documentation
  checks: 8 passed. Full regression: 1,819 passed, 2 skipped. Wheel and sdist
  built offline; installed-wheel stream-boundary/model-selection/cancellation
  smoke passed in an isolated environment.

M2 operator permission broker:

- Pending ACP permissions now have durable request identities, exact offered
  choices, expiry, and run/process/session/revision bindings. Operator inspection
  and one-time answers use `run permission`; the existing callback continues
  without another agent turn. Pending waits are visible in run status.
- Duplicate identical pending answers are idempotent; conflicting, stale, expired,
  and persistent grants are rejected. Consumption is recorded before delivery;
  cancellation/restart never replays an answer. Recovery preserves an input hold.
- Review details exclude known credentials, sensitive fields, bearer tokens,
  and arbitrary vendor metadata. Redaction is best effort. Cooperative local
  state access and each harness's own sandbox remain explicit trust boundaries.
- Real Claude, explicitly configured Grok, and Codex's explicitly escalated
  synthetic command passed operator CLI → same callback → expected file checks.
  Initial Grok/Codex runs wrote without requesting approval; their mode-specific
  behavior is retained in the matrix. Client policy cannot intercept unrequested
  tool execution. No global harness configuration was changed.
- Fixtures cover approval/rejection, cancellation while pending, stale revisions,
  expiry, redaction, and SIGKILL before delivery of both pending and answered
  requests. Full regression: 1,826 passed, 2 skipped; the subsequently added
  reproducible-probe test and metadata-redaction check each passed. Documentation:
  8 passed. Wheel/sdist built; installed-wheel operator-CLI/callback/file smoke passed.

M2 mixed pipeline / M7 local validation preparation:

- The real Codex developer and Claude tester completed a local feature task's
  first two typed handoffs using separate daemon-run unit tests. Artifacts and
  command receipts passed domain validation and reached `feature_review`.
- Grok review hit the 240-second prompt budget while waiting for an operator
  permission. No real review/merge/verified outcome is claimed. The retained
  private continuation locator is `/tmp/greatminds-mixed-pipeline-report.jsonl`;
  its first record identifies the existing project. Continue that task after
  reducing service-command overhead, without repeating accepted earlier stages.
- The scenario now has a repository probe accepting three user-supplied ACP
  bindings. Its fixture exercises all three transitions, source-bound command
  evidence, daemon merge, worktree cleanup, main-branch tests, and restart without
  replay. Focused contract/domain/scope/pipeline regression: 68 passed.
- Context uses the daemon interpreter's isolated CLI invocation, removing PATH
  and workspace-module lookup dependence. A test with empty PATH and a shadow
  module proves it resolves the intended CLI.
- The effective schema permits explicit local plans to omit stand-only probes;
  unspecified/stand-required plans preserve those checks. Local validation and
  review still use the existing domain gates and configured command evidence.
- The real pipeline exposed avoidable LLM work: external result JSON creation,
  reading daemon-owned command logs, discovering result fields, and repeated
  approvals just to invoke scoped CLI operations. These are the next deterministic
  interface improvements, not reasons to bypass executor permissions globally.
- Full regression: 1,829 passed, 2 skipped, with one fault-injection test stopping
  the daemon while it held the store lock. The test now synchronizes SIGSTOP
  outside that transaction; the final daemon/pipeline/scope subset passed all
  30 tests. Documentation checks passed all 8. Installed-wheel three-role pipeline,
  merge, cleanup, and restart smoke passed; this is fixture evidence, not a real
  harness review result.

M2 completed mixed pipeline and scoped CLI checkpoint:

- The retained Codex → Claude → Grok task now reached `verified`: all three
  receipts applied, daemon merge and worktree cleanup completed, six tests pass
  in the main checkout, and idle restart preserves runs/results/commands.
- Grok required an explicit retry with a 600-second budget and one-time operator
  permissions. The earlier partial evidence remains; completed evidence is in
  `evidence/acp-mixed-pipeline-completed-2026-09-06.json`.
- Inline JSON submission removes result-file creation. Bounded, integrity-checked
  command previews remove separate log reads. Credential-scoped `run contract`
  exposes pinned schema and generated fields without rediscovering current canon.
- Installed-wheel three-role fixture pipeline, merge, cleanup, and restart passed.
- Full regression: 1834 passed, 2 skipped. The whole modernization plan remains
  active; one completed local task does not establish every harness/role policy.

M3/M4 stand durability preparation:

- Stand state now uses the shared atomic publication primitive and a stable lock
  inode. Fault tests cover failed serialization/replacement and process death;
  concurrent writers and unlocked readers retain complete state and updates.
- A project deployment lock serializes operator/coordinator execution. Late
  success, failure, and stale-source results cannot overwrite another lease.
  Coordinator retry exhaustion rechecks ownership, and lock contention does not
  consume retries or force an active deployment down.
- Full regression: 1845 passed, 2 skipped. Additional coordinator retry guards
  passed the 12-test deployment/routing regression; fault and deployment tests
  passed all 23 cases, documentation all 8. Installed-wheel state/exclusion smoke
  passed. No external deployment or live state migration was performed.
- Next stand work: durable deployment intent and child identity/recovery, followed
  by ACP daemon scheduling and expired dead-holder lease reclamation. Do not infer
  exactly-once external execution from the deployment lock.

M4 durable deployment intent checkpoint:

- External dispatch now persists an attempt before execution, then the result
  before the stand transition. A final receipt is recoverable from the exact
  deployment ID in atomic stand history without re-running the command.
- Unfinished attempts block replay across process restarts and lease changes.
  Exceptions preserve uncertainty; corrupt ledgers fail closed. Operator JSON
  `stand deployment-status` exposes the attempts without manual YAML inspection.
- Fault tests cover process death after an external effect, failed stand write
  after a successful command, missing final receipt, and same-lease history that
  does not prove the particular deployment. Raw logs and arbitrary lease secret
  fields are excluded from the ledger.
- Stand/ledger/coordinator regression: 253 passed, 1 skipped; documentation: 8
  passed. Installed-wheel intent/receipt recovery smoke passed. Final ledger
  validation and engine regression passed all 27 tests. No live deployment was used.
- Remaining: child process identity/gated launch, bounded cleanup, explicit
  resolution of uncertain external outcomes, ACP daemon deployment scheduling,
  and dead-holder lease reclamation. Owner identity alone cannot prove child exit.

M4 gated deployment process and operator recovery checkpoint:

- Managed profile execution now uses a fresh Linux session and an exec gate.
  Child identity is saved before authorization; a failed write prevents execution.
  Timeout/default 1800 seconds and exit paths clean the owned process group.
- `stand deployment-recover` cleans recorded orphan groups under the deployment
  lock; `deployment-resolve --reason` requires cleanup and operator assessment.
  Resolution does not change stand state or create successful evidence. Assigned
  ACP credentials cannot approve recovery. Older untracked attempts fail closed.
- Real local subprocess fixtures prove identity-before-exec, refusal after failed
  persistence, timeout cleanup of descendants, recovery after killing the owner,
  and profile-engine integration. These fixtures invoke no remote deployment.
- ACP daemon sweeps now reconcile completed transitions and clean orphan process
  groups without an agent turn or external replay. Active deployment locks are
  respected; unchanged cleanup is deduplicated. `run status` includes the ledger.
- Full regression: 1867 passed, 2 skipped. Subsequent daemon integration passed
  all 53 targeted tests; final crash-window/document checks passed all 16.
  Installed-wheel gated command, daemon cleanup, and explicit resolution passed.
- Next: integrate stand scheduling into the ACP daemon, then expire dead-holder
  leases with explicit liveness evidence. Extend command output bounds and
  source/environment evidence to stand receipts.

M3/M4 deterministic stand lease expiry checkpoint:

- ACP daemon expiry requires a valid elapsed TTL and absence of active holder/task
  runs, residual groups, unresolved commands/results, and deployment uncertainty.
  Unreadable or live legacy holder records block reclamation.
- Reclamation rechecks under deployment → runtime store → stand locks, records
  SYSTEM history, and promotes FIFO in one atomic state replacement. Repeated
  sweeps do not repeat transitions; local workflows create no stand state.
- `run status` explains stand lease holds. Manual reclaim shares TTL and ACP
  ownership checks and cannot overlap a deployment or bypass unresolved effects.
- Daemon/lease/reclaim integration: 42 passed. Final lease/manual/documentation
  regression: 57 passed. The controller suite including a claim arriving between
  inspection and publication passed all 18 tests. Installed-wheel expiry, SYSTEM
  history, and idempotent sweep passed. No live stand was modified.
- Next: ACP daemon profile scheduling with explicit authorization, output limits,
  and source/environment evidence; remaining M3 maintenance and M5–M7 work stays
  open. Lease reclamation alone does not complete stand integration.

M4 bounded deployment capture and cancellation preparation:

- Replaced unbounded process output buffering with concurrent bounded stream
  draining, full-stream hashes/counts, and private atomic prefix artifacts.
  Manual deploy accepts an explicit capture limit; truncated output cannot be
  accepted as a successful stand verdict from an incomplete log.
- Main-process exit cleans remaining group members promptly; inherited pipes
  cannot keep a finished command waiting for the full deployment timeout.
  Cooperative cancellation is available to the upcoming daemon scheduler.
- Tests exercise multi-megabyte stdout/stderr, complete hashes, private artifact
  permissions, binary output, cancellation, background pipe holders, and refusal
  to mark a stand ready after capture truncation.
- Stand/ACP daemon regression: 266 passed, 1 skipped. Focused executor/output
  regression: 60 passed; documentation: 8 passed. Installed-wheel capture bound,
  complete stream hash, and private artifact smoke passed. No remote deploy ran.
- Next: wire authorized profile dispatch and cancellation into the ACP daemon
  scheduler; extend source/environment evidence. Full M3/M5–M7 scope remains open.

M4 authorized ACP stand scheduler checkpoint:

- Optional execution `stand` policy declares explicit authorized profiles, timeout,
  and output limit. Registry role restrictions, per-lease user approval, and file
  consistency remain enforced by the shared deployment engine.
- Dedicated worker runs profiles without blocking ACP processing. Pause prevents
  new selection; shutdown cancels and drains the worker. `--once` waits for it.
- Durable lease/policy tickets prevent identical automatic replay after failure or
  interrupted selection. Exact lease revalidation closes the selection/start race.
  Operator status includes dispatch reasons, tickets, and redacted policy errors.
- Final scheduler tests: 18 passed; ACP/config integration: 68 passed. Synthetic local
  executables cover successful deployment, no-agent operation, restart, shutdown,
  policy/approval refusal, lease mutation, and lost selection acknowledgement.
- Full regression: 1909 passed, 2 skipped; documentation: 8 passed. Installed-wheel
  authorized local profile and no-replay restart smoke passed. Final scheduler
  checks additionally cover role/file approval and nonzero-result classification.
- Next: stand source/environment evidence and remaining deterministic maintenance,
  then interactive ACP roles, default migration, and product simplification (M5–M7).

M4 managed stand input identity checkpoint:

- Managed profile execution records source/profile/executable identities and an
  HMAC of effective process environment plus playbook variables before execution.
  It reuses the configured-command evidence primitive and private key.
- A post-command mismatch prevents ready publication, preserving the real process
  result and unresolved outcome. Runtime metadata is excluded from source checks;
  environment values are absent from the ledger.
- Focused command/stand/evidence regression: 69 passed; expanded stand/ACP/command
  and documentation regression: 307 passed, 1 skipped. Fixtures return zero while
  changing source, profile, or PROJECT.env and still cannot mark the stand ready.
  Final shared-context regression passed 45 tests. Installed-wheel stable-input
  success and changed-source refusal passed on local synthetic profiles.
- Next: revalidate stand evidence at domain consumption, declare external environment
  revisions, and complete remaining maintenance / interactive ACP / migration work.

M4 stand evidence consumption checkpoint:

- Domain transitions, CLI gate checks, and manual ready now require current
  managed deployment evidence in ACP projects. They compare local inputs again
  and verify complete captured output by path, size, and hash. Later failed or
  unresolved same-lease attempts cannot reuse an earlier successful receipt.
- Manual ready serializes with deployment and checks the deployed lease snapshot.
  Operational run credentials and terminal variables are excluded from both the
  playbook environment and its identity. Public context is reconstructable;
  environment values remain private. Declared `stand.environment_revision` covers
  operator-known external changes; it is not remote-state discovery.
- Expanded stand/config/ACP/gate regression: 340 passed, 1 skipped. Final focused
  regression: 49 passed, including same-ID lease mutation, output symlinks,
  malformed environment revisions, and source changes between deploy and gates.
  Installed-wheel local deployment accepted fresh evidence and rejected a source
  change through both the CLI gate and task transition evaluator. No remote
  deployment was performed.
  Legacy marker fallback exists only without both ACP configuration and ledger,
  pending the explicit M6 migration.
- Next: remaining deterministic maintenance and interactive daemon-owned ACP
  sessions, then default configuration migration and product simplification.
  M5–M7 and the complete harness compatibility campaign remain open.

M5 interactive journal foundation:

- Added a private conversation journal with pinned binding/config/schema/workspace,
  idempotent prompt IDs, durable FIFO ordering, one running turn, cancellation
  intent, and cursor-based output reading. Supervisor ownership changes interrupt
  started turns without automatic replay and preserve queued input.
- Nine focused tests passed: concurrent claims, lost enqueue acknowledgement,
  reconnect, recovery/old-owner rejection, cancellation, UTF-8 output bounds,
  contract changes, and queue limits. This is an internal persistence foundation;
  it does not yet launch an interactive session or expose chat commands.
- Immediate next step: integrate this journal with daemon-owned ACP sessions,
  shared capacity and permissions, then implement chat/attach and restart tests.
  Keep the complete migration and compatibility scope open.

M5 daemon-owned conversation integration checkpoint:

- Added `chat create/send/attach/interrupt`. All execution goes through the same
  ACP Supervisor and gated transport. Dialogues can use any configured role binding
  without a synthetic workflow file; conversation runs cannot submit task results.
- Shared admission covers project, binding, and account capacity plus authentication
  holds. Continuous coordd keeps a conversation session connected between messages;
  once mode drains queued input. Reopening requires saved-session load support,
  with an explicit failure when unavailable. Loaded history is not streamed twice.
- User input is serialized per conversation and never injected into background
  task turns. Shared permission callbacks can be answered by the operator. Attach
  reads cursor-based JSON events; detach leaves the run alive. Interrupt requests
  transport cancellation. Configuration/capacity holds retain queued messages and
  publish a dispatch explanation.
- Initial daemon/supervisor regression: 57 passed. Final conversation/journal/
  contract regression: 58 passed. Installed-wheel create/send/attach, two turns in
  one session, cursor reconnect, and saved-session continuation passed with the
  local ACP fixture. No new live harness compatibility claim is made.
- Full regression: 1954 passed, 2 skipped, with one documentation wording failure.
  The wording was corrected and the documentation checks passed on rerun. An
  additional focused test proves shared background capacity and rejection of a
  conversation's attempted task result. No implementation failure occurred in
  the full run; its collected suite predates that additional admission test.
- Next: terminal conversation presentation, explicit session close, task attachment,
  richer input events, real interactive harness checks, and VS Code integration.
  Default migration/removal of legacy execution and remaining M3/M7 remain open.

M5 explicit conversation closure checkpoint:

- `chat close` stops admission, cancels queued messages and requests cancellation
  of the connected run. Only the daemon acknowledges closure after cleanup;
  the history remains readable. Follow readers stop after the closed event.
- Closure is independent of current binding/configuration compatibility and does
  not start an agent just to close a never-dispatched dialogue. The once-mode loop
  drains pending closure controls before returning.
- Focused closure/journal tests: 25 passed. Expanded conversation, daemon,
  contract, and documentation regression: 80 passed. Checks include empty process
  groups before closure acknowledgement and unchanged journals after restart.
  Installed-wheel connected-session close, retained history, and follow-reader
  exit passed using the local ACP fixture and the project's resolved runtime path.
- Next: terminal presentation and task attachment, followed by the outstanding
  real interactive harness/input/VS Code scenarios and M3/M6/M7 scope.

M5 terminal conversation client checkpoint:

- `chat attach --text` renders messages and outcomes with a reconnect cursor.
  `chat talk` accepts messages, streams responses, and presents explicit numbered
  one-time permission choices. It does not launch its own agent or auto-approve.
- Detach/EOF/Ctrl-C retain work; `/close` requests daemon cleanup. The client prints
  a reconnect command on exit. Escaping terminal controls prevents streamed text,
  including split escape sequences, from issuing terminal commands.
- Initial terminal tests: 4 passed, including separate CLI subprocesses against
  the daemon/ACP fixture, explicit permission rejection, read-only replay, and
  detach during work. Existing conversation tests: 16 passed.
  Expanded terminal/conversation/documentation regression: 28 passed.
  Final terminal checks after permission-detail rendering: 4 passed. Installed-wheel
  terminal input, streamed reply, and detach/reconnect cursor smoke passed with
  the local ACP fixture; live-provider interactive validation remains outstanding.
- Next: task attachment, richer input and interactive live-harness coverage, then
  VS Code/default migration and the remaining deterministic/product work.

M5 task-bound conversation checkpoint:

- `chat create --task EXACT_TASK_ID` pins a real task revision and enforces the
  role's queue claim policy. Shared admission prevents concurrent background and
  interactive claims. Required worktrees, task context, command evidence, and
  typed results use the existing services.
- Recheck the revision before every prompt; changed tasks retain queued input
  behind an explicit hold. A task result releases the interactive run after the
  prompt so the domain service can apply it. Live permission waits cannot permit
  task mutation or worktree cleanup while the agent is still executing.
- Expanded conversation/domain/contract/supervisor regression: 100 passed.
  Focused task tests: 5 passed, including actual task worktree preparation,
  shared task admission, stale revision, and result application gating.
  Terminal/task/documentation checkpoint: 17 passed. Final task suite: 6 passed,
  including revision changes between messages in a connected session and result
  application in continuous daemon mode. Installed-wheel task attachment, context,
  and domain application passed using the local ACP fixture.
- Next: richer input events and real interactive harness coverage, then VS Code
  integration, default migration/removal of legacy paths, and remaining M3/M7.

M5 VS Code ACP conversation integration checkpoint:

- Added read-only `chat bindings/list` metadata for interface clients; message
  contents stay in the private conversation journal. The extension selects a role
  binding and optional exact task ID, creates or attaches an existing conversation,
  and opens the shared terminal client using an executable/argument array.
- Close ACP Chat uses the daemon close command. Terminal closure only detaches;
  no native harness TUI or additional transport is introduced. Command errors are
  shown in the extension output. Existing cockpit commands remain until M6.
- Node syntax and mocked extension tests passed, covering task/role selection,
  paths containing spaces, existing-session attachment, close, and cancellation.
  Actual graphical extension-host acceptance remains open.
  CLI metadata/terminal/task regression: 11 passed; documentation: 8 passed.
  Installed-wheel binding/create/list metadata smoke passed without creating any
  agent run; private message contents are absent from the interface listings.
- Next: remaining interactive harness/input checks and M6 default migration,
  alongside the outstanding deterministic maintenance and product improvements.

M6 shared ACP bootstrap preparation:

- Extracted deterministic bootstrap behind `setup --execution-config FILE`:
  validate the supplied manifest, create shared schema queues and runtime mirror,
  preserve user files, and publish the execution contract last. It performs no
  harness launch, service installation, trust mutation, or native-tool setup.
- Repeated setup is idempotent. Different existing contracts and fleets requiring
  migration are refused; this option is not a hidden live-fleet conversion.
- This is migration groundwork. Default setup/launch still require conversion,
  and native driver removal remains part of M6 rather than being declared done.
- Bootstrap/setup/documentation regression: 56 passed. Installed-wheel bootstrap,
  conversation creation, and ACP daemon execution passed in a fresh local fixture.
  No existing live project, service, or harness configuration was migrated.
- Next: explicit old-configuration migration and ACP launch surfaces, then default
  switch/removal with updated acceptance tests. M3/M5/M7 remaining scope persists.

M6 ACP launch surface checkpoint:

- `launch` selects ACP frontends when execution.yaml exists, including protection
  against entering the other launcher through an explicit coord.yaml path.
  VS Code/Cursor workspace tasks run the daemon and operator queries as process
  argv, preserve unrelated workspace content, and leave folder tasks untouched.
- Tmux creates only a coordd window and an operator shell. Existing project-specific
  sessions are reported without recreation; no native harness commands or synthetic
  keystrokes are emitted. Destructive recreation requires explicit operator cleanup.
- Launch/documentation regression: 42 passed. Final frontend tests: 5 passed,
  including configuration-path bypass prevention. Tmux command behavior is tested
  with a process mock; graphical editor and live tmux acceptance remain open.
  Installed-wheel workspace generation and repeat generation passed; executing
  the generated daemon argv with --once completed on an empty local ACP project.
- Next: explicit fleet configuration migration and default setup/driver removal.
  This does not complete M6 or the remaining M3/M5/M7 acceptance scope.

M6 explicit execution migration review checkpoint:

- Added read-only migration review for an explicitly supplied ACP contract. It
  exposes source/target role policies and missing/added/repeated roles, supports
  explicit retirement declarations, and pins reviewed configuration/schema bytes.
  It does not infer adapters, model identities, permissions, or credentials.
- Existing ACP projects cannot re-enter native setup through plain setup or
  migrate/update refresh. Combined configuration/runtime layouts are held for
  explicit migration instead of silently creating an empty second runtime.
- The migration review does not apply changes. Next: quiescence proof, backups,
  durable contract publication and generated-artifact retirement, then the default
  switch and actual driver removal. Role coverage alone is not migration readiness.
- Migration/bootstrap/setup regression: 37 passed; documentation: 8 passed.
  Installed-wheel review detected missing roles, accepted explicit retirement,
  and left project files unchanged. No live fleet migration was performed.

M6 execution observation and ACP launch barrier checkpoint:

- Migration reviews now expose current-user/project process holds, live registry
  PIDs, active runs, unresolved domain/command/deployment work, and unreadable
  state. Command-line contents are absent from the report. Dynamic observations
  are separate from the static configuration review hash.
- ACP supervisors hold a shared project/user execution barrier through cleanup;
  its exclusive side prevents new ACP startup during migration. The stable Linux
  lock sits outside project source/runtime layouts. Existing supervisor locking
  continues to enforce one daemon, and failed startup releases the new barrier.
- This is not complete quiescence proof for prior-version native launchers.
  Review output explicitly leaves launch exclusion unverified. Next: retire/block
  those entrypoints, verify stopped services, and implement durable migration apply.
- Migration/supervisor/daemon regression: 54 passed; documentation: 8 passed.
  Final process/barrier suite: 8 passed, including cleanup after failed secondary
  supervisor admission. Installed-wheel exclusive barrier/startup refusal, release,
  and quiet-fixture observation passed without launching an agent.

M6 native launcher routing checkpoint:

- `start-agent` (including dry-run), the Click `pty-launch` command, and its
  direct Python entrypoint refuse ACP projects before environment loading,
  registry writes, or process creation. Canonical project discovery covers
  nested worktrees and explicit project overrides. Invalid contracts, directory
  placeholders and broken symlinks do not silently select native execution.
- Native-launch refusal and existing native dry-run/driver regression: 27 passed.
  This is a routing guard, not lifetime launch exclusion for prior-version
  processes. Migration apply, service quiescence and native driver retirement
  remain outstanding; launch exclusion remains unverified in migration reviews.

M6 native parent execution exclusion checkpoint:

- Current-version native `coordd`, `pty-launch`, `start-agent` preparation and
  frontend launch hold the shared project execution barrier. Native routing is
  rechecked after admission. PTY entrypoints share one implementation; the
  parent retains the barrier through child wait and registry cleanup.
- `coordd` and frontend launch resolve the canonical project before choosing
  ACP, including invocation from nested worktrees. Directory placeholders and
  broken execution-contract symlinks fail validation instead of selecting the
  native daemon. Explicit project selection keeps the legacy daemon's child
  environment and barrier identity aligned.
- The descriptor does not survive direct `exec`; the standard PTY wrapper
  reacquires before forking. No-PTY direct launches, detached children, and
  prior-version services still require independent quiescence verification.
  Migration review therefore still does not claim complete launch exclusion.
- Final native/ACP routing, barrier and frontend regression: 61 passed, including
  a real synthetic child inside a PTY and observed release after cleanup.
  Worktree/restart path and documentation regression: 12 passed. Installed-wheel
  checks rejected all four native CLI entrypoints under exclusive exclusion and
  ran an empty ACP daemon batch from a nested directory. No live harness or
  existing fleet was launched or migrated.

## Recovery checkpoint

ACP observation and internal driver removal checkpoint:

- Removed the remaining native Claude subprocess, Codex app-server JSON-RPC,
  headless driver registry and TTY wake implementation from cli/coordd.py. The
  module now only resolves the project and invokes the common ACP daemon. The
  native restart implementation and static native tool registry are deleted.
- Agent status/tools, dashboard and run status use shared ACP observations.
  Waiting states, task/configuration revisions, process identity and lifecycle
  remain distinct. Changed/deleted bindings do not hide existing runs. The
  dashboard includes task queues and assignment/stand/maintenance observations.
  VS Code's agent tree now displays configured ACP manifests, not native modes.
- Explicit required-live-role gates now consume ACP run state, including target
  contexts, and hold missing/unreadable/unusable required roles. No terminal pane
  parsing participates in this gate. Native driver/pane-only tests were removed;
  domain field and stale-deployment tests are retained for continued porting.
- Validation: ACP conversation/task/operator regression 32 passed; observation,
  project selection and stale-deployment suite 18 passed; daemon/maintenance/
  observation/documentation suite 56 passed. VS Code mocked API suite passed.
  Installed-wheel status surfaces agree without changing project files; native
  driven modules and dispatch functions are absent. No live provider was invoked.
  Full test collection succeeds; full offline execution still needs porting of
  remaining tests that assume native setup or diagnostic APIs.
- Next: simplify daemon service installation/doctor (including app-server units),
  remove unused setup/auth/profile code and migration scaffolding, finish
  watchdog/event-log/extension/documentation cleanup, and complete the remaining
  deterministic maintenance and compatibility items. No existing installation
  migration or rollback work is required.

Current owner clarification and direct ACP cutover checkpoint:

- The owner confirmed there are no legacy installations or production deployment.
  Uncommitted work on live-fleet migration apply/rollback was discarded. No more
  native migration compatibility is required; preserve project task data while
  removing the native implementation directly.
- Plain setup now initializes an explicit empty ACP contract and shared queues;
  supplying a contract remains supported. Repeated setup preserves user config.
  Setup no longer dispatches native harness/plugin/auth/service configuration.
- Public coordd always selects ACP, including from nested directories. Launch
  only uses ACP frontends. Direct start-agent/PTY modules and their driver registry,
  native frontend generation, the native daemon loop, and native migration
  implementation have been deleted. Native fleet restart/migrate are no longer
  public commands; package update no longer restarts native agent panes.
- Remaining cutover work: remove internal driven-driver/TTY helpers after replacing
  agent/dashboard diagnostics; remove unused setup/service/profile integration
  code and migration scaffolding; update service installation and extension paths;
  port remaining tests and documentation to ACP defaults. Full offline suite
  execution is still required after that cleanup. Collection currently succeeds.
- ACP defaults/bootstrap/frontend/public-documentation regression: 28 passed.
  Synthetic conversation and task-bound ACP regression: 22 passed. Installed-wheel
  default setup, nested daemon, synthetic ACP conversation, frontend generation
  and absence of direct native launch modules passed. No new live harness
  compatibility or production migration is claimed by these checks.

The authoritative continuation point is this committed plan and the compatibility
matrix, not the lifetime of a desktop conversation. Current focus: finish M2 real
integration and operator policies, then the outstanding M3–M7 items above. The full
plan is still active. A successful synthetic response is not completion of M2.

Continue with remaining interactive input handling and the outstanding M3–M7
work. Scoped submission, command output, and the retained real pipeline now have
completion evidence. Durable
one-time permission callbacks and real command approval now
have evidence; complete harness permission-policy parity is still required. Session
load, history continuity, and cancellation after streaming now have real evidence;
model selection is verified for Codex/Claude only. Keep raw provider logs and
authentication material out of the repository. Preserve `.codex-solo-handoff/`
as an existing user artifact.

The convenient local web interface is the next product phase after this plan;
its implementation has not started.

This document authorizes no release or migration of existing live fleets by
itself; those operations are separate from developing and testing the changes.

### Recovery checkpoint: static ACP doctor (2026-09-07)

Replaced `daemon doctor`'s direct Claude prompt with shared ACP configuration
validation, executable discovery and required-environment checks. The command
never starts an agent, reports only environment names, offers JSON, and works
with an explicit ACP project root without `coord.yaml`. It labels evidence as
static and names the environment sources; it does not claim service-manager
environment parity, authentication or negotiated protocol support. Relative
executable/PATH entries remain unresolved by this static check. Removed the
three native prompt-probe tests and replaced them with a no-subprocess,
read-only, secret-redaction and missing-prerequisite regression.

Validation: ACP doctor and existing project environment drop-in tests: 9 passed.
Service app-server removal, manifest-based environment capture and service name
resolution remain the next cleanup steps. The full modernization plan remains
in progress.

### Service transport cleanup (2026-09-07)

Removed the native Codex app-server template, its executable/socket resolution,
installation and enable branches, and restart-time refresh. Service management
now installs only the common coordd template regardless of old vendor window
configuration. Removed singleton-service detection, the `daemon migrate` command
and the unused update migration helper; no installed legacy fleet exists.
Retired tests for those deleted implementations and kept common daemon service
and environment tests. Added an explicit install regression asserting the exact
systemctl calls, single installed template, ACP coordd entrypoint and absence of
the migration command.

Validation: 31 service/doctor/environment tests passed; full suite collection:
1626 tests without collection errors. This is not a full-suite execution claim.
Project naming and environment capture still need conversion from native setup
assumptions, and update service opt-in behavior remains to be reviewed.

### ACP service identity (2026-09-07)

Service naming no longer reads `coord.yaml`: resolve an explicit name, an
existing unique registry entry for the project, or the project directory name.
Implicit selection from a nested directory finds the enclosing project. Validate
service identifiers before install mutations, reject conflicting directory/name
pairs and duplicate-basename redirection, and require explicit selection for
multiple registered aliases. Doctor uses the same resolver. Documented optional
service installation in the quickstart.

Validation: 44 service/doctor/coordd tests passed, including fresh ACP setup,
service registration and nested-directory start without coord.yaml, invalid
names, collisions and ambiguous aliases. systemctl was substituted in tests;
no live user services were modified. Environment capture, registry concurrency
and service/update failure handling remain to be completed.

### Service failure handling (2026-09-07)

Systemctl calls now have a 30-second bound and explicit CLI diagnostics for a
missing executable or timeout. A timeout reports uncertain service state and
asks the operator to inspect it; it does not claim the manager job was cancelled.
Installation fails on enable/reload errors. Restart cannot proceed after a
failed reload. Both paths reload the manager even when the unit files are
unchanged, allowing a retry after an earlier failed manager reload. Removed the
unsupported promise that enabling a user unit alone guarantees logout survival.

Validation: 49 service/doctor/environment tests passed, including repeated
failed reload, successful retry, blocked restart, missing executable and timeout.
Updated the old no-reload-on-identical-files assertion to the recovery contract.
All systemctl interactions in these tests were substituted.

### Manifest-driven environment selection (2026-09-07)

Removed native Claude credential-file diagnostics, driven-role detection and
service warnings, plus their unused vendor executable helper. Captured service
environment names now come from agent/command `required_env` and `environment`
source references. Still-declared captured values survive a maintenance shell
that lacks them; deleted references are pruned. Writes use the shared durable
atomic replacement helper with private temporary-file permissions. The schema
consistency test now compares the shared ACP configuration snapshot instead of
calling removed native daemon helpers.

Validation: 51 service/environment/schema tests passed. Tests use synthetic
references and secrets; no provider was launched. Follow-up remains necessary
for systemd EnvironmentFile quoting/parsing, per-project PATH, registry locking,
and complete service configuration preflight. This checkpoint establishes
manifest selection, not full environment serialization parity.

### Systemd environment serialization (2026-09-07)

Replaced shlex-based EnvironmentFile serialization/inspection with a dedicated
codec. It preserves literal whitespace, multiline values, quotes, backslashes
and shell-looking text without expansion; malformed quotes/escapes and NUL fail
with diagnostics that omit values. Missing optional files remain allowed, while
other read errors are no longer silently treated as empty configuration.
Captured file comparisons preserve carriage returns, and unchanged captured
files have their private permissions restored.

Rules were checked against the upstream [systemd environment parser](https://github.com/systemd/systemd/blob/main/src/basic/env-file.c)
and the versioned [255 parser interface](https://github.com/systemd/systemd/blob/v255/src/basic/env-file.h).
An optional regression calls the installed systemd 255 shared-library parser
on 200 seeded synthetic values and compares its output byte-for-byte with the
inputs and the Python decoder. It passed on this host without launching a
service. Broader service/schema suite: 68 passed; final codec/environment/doctor
subset after private-permission and error-redaction adjustments: 24 passed.
Per-project PATH, unit directive escaping and registry locking remain pending.

### Literal service paths and generic PATH (2026-09-07)

Removed vendor-specific PATH discovery and login-shell execution from service
rendering. Launcher paths remain argv elements rather than being split on
whitespace; interpreter symlinks remain intact. The generic baseline can be
replaced by project EnvironmentFiles or by declared agent/command PATH mapping.
Added unit-word escaping for whitespace, controls, quotes and percent specifiers;
launcher arguments containing dollars use systemd's no-expansion execution
prefix. HOME, PATH and EnvironmentFile directives use this literal encoding.

Validation: 75 service/environment/schema tests passed. Native
`systemd-analyze verify --man=no` accepted a generated unit whose executable path
contains spaces, percent specifier text and a dollar sign; no service was started.
Drop-in tests verify percent escaping and quoted paths. Remaining service work
includes registry concurrency, strict configuration preflight and update's
optional service lifecycle behavior.

### Durable project registry (2026-09-07)

Project registration now holds the shared storage primitive's bounded stable
file lock across read/check/write and publishes with atomic fsynced replacement.
Concurrent writers cannot lose unrelated registrations or redirect a name after
another writer claims it. Unchanged registrations do not rewrite the document.
Readers reject malformed JSON, duplicate names, invalid identifiers and relative
or non-string project paths; read failures no longer turn into an empty registry.

Validation: parallel-process tests start eight registrations together, both for
unique names (all retained) and a shared name (one winner). Injected publication
failure preserves previous bytes and removes the temporary file. Corrupt input
remains unchanged after attempted registration. Combined registry/service/schema
suite: 86 passed. Tests operate only on temporary registry files and substituted
service-manager calls. Strict service config preflight and update lifecycle
remain the next service tasks.

### Service activation preflight (2026-09-07)

Install validates the shared ACP contract and environment-file syntax before
writing service files or registry entries. Start, restart and repair require a
registered project and validate that registered root before activation. Restart
performs this check before refreshing units. Stop and status remain available
without requiring a valid execution contract so a broken project can be stopped.
Service tests now create actual minimal ACP contracts instead of implicitly
accepting native-only or unregistered project fixtures.

Validation: 106 registry/service/environment/schema tests passed. New cases cover
missing and malformed contracts, forbidden native transport, malformed project
environment, unregistered activation and stopping a broken project. Failure
cases compare all temporary file bytes before/after and assert zero systemctl
calls. No live user services were changed. This preflight verifies the static
contract; authentication and negotiated capabilities remain runtime checks.
Next: update must respect optional service installation and remove remaining
native restart assumptions.

### Optional service lifecycle during update (2026-09-07)

Update no longer installs missing systemd templates or contains native tmux
session/agent restart helpers. Project refresh resolves the current ACP root or
an explicit registered project. Only services with an existing template and
project drop-in are refreshed; systemctl try-restart preserves inactive services.
Unregistered foreground workflows receive a manual daemon restart hint. Package
self-replacement now uses an argv list with the current interpreter (preserving
spaces and venv symlinks) and retains explicit --project selection.

Retired auto-service-install and native tmux lifecycle tests, preserving package
upgrade/environment-manager checks and adding ACP lifecycle cases. Validation:
134 update/registry/service/environment/schema tests passed. Tests prove no
service files or subprocess service actions for uninstalled services, exact
reload/try-restart calls for installed services, failure propagation, explicit
root selection, and interpreter/project preservation. No live service was
restarted and no package upgrade was performed. Full modernization remains open;
remaining native setup/helper/CLI cleanup and full-suite execution are next.

### Remove dead vendor setup implementations (2026-09-07)

Deleted unused Claude settings/auto-mode generation, plugin installation,
credential pretrust, per-role Codex homes/skills setup and native git-hook
installation helpers. Public setup remains the shared ACP bootstrap. Removed the
suite-wide plugin-install suppression environment flag; the ACP setup regression
now explicitly runs without that flag, rejects any external subprocess and
asserts vendor installers are absent. Retired tests specific to removed vendor
setup implementations, retaining unrelated task, hook and stand behavior checks.

Validation: 54 ACP bootstrap/project-environment/stand-profile tests passed;
9 retained hook/configuration tests passed; full collection: 1578 tests without
collection errors. Pure deterministic template/profile helpers remain pending
module cleanup, including review of obsolete profile migration hashes. Native
CLI/hooks and full-suite execution remain open; this checkpoint is not completion
of the modernization plan.

### ACP event observer and removal of native stop hooks (2026-09-07)

Removed the host-specific stop-decide CLI/module and its native hook tests.
Removed driven-log's unused native driver event writer/observer and replaced the
operator surface with `run events`: JSON Lines from the durable RunStore, an
exclusive sequence cursor, bounded batches and optional follow. It does not
create a separate observer log or launch agents. VS Code's command/palette now
opens ACP run events. Wake-check remains because its ACP branch already calls
the shared dependency maintenance service; duplicate non-ACP logic still needs
cleanup with preservation of dependency diagnostics.

Validation: 40 run-event/default-ACP/maintenance/extension scaffold tests passed;
Node extension harness passed. Event tests cover read-only pagination and
follow without replay. Final suite collection: 1553 tests without errors.
No live provider or extension host session was exercised in this checkpoint.
Remaining native assets, watchdog/wake fallback code, setup template helpers,
and full-suite execution remain open.

### First broad post-cutover test audit (2026-09-07)

Ran the whole suite with a 20-failure diagnostic limit rather than relying only
on targeted checks. The first batches exposed tests calling deleted Codex
app-server, native Claude turn/reset/retry, SIGINT/tmux wake and native migration
helpers. Retired those implementation-specific files; this does not establish
ACP retry/backoff parity, which remains explicitly required by B1/M3. Preserved
Ansible package checks and git permission checks. Replaced automatic git-hook
installation assertions with proof that ACP setup preserves a user's hook and
does not install one. Ported inbox/planning setup fixtures by removing the old
--lang flag while retaining their domain assertions.

Evidence: ACP supervisor (including real synthetic process cleanup and restart
claim recovery), git permission/setup and package checks: 31 passed. Inbox and
planning domain tests: 5 passed. Latest broad attempt stopped at 20 failures,
319 passed (16.73 seconds); this is NOT a green full-suite claim. Next failures
are recorded in /tmp/greatminds-modernization-suite-4.txt: obsolete live-developer
launch, native backlog APIs and pane-based live-role tests. Port live-role
invariants to durable ACP runs; do not discard the required-live-role gate.
Other broad logs: /tmp/greatminds-modernization-suite{,-2,-3}.txt.

### ACP required-live-role gate parity (2026-09-07)

Ported local/remote live-role tests from pane text and native registry files to
RunStore claims, transitions and process identities. Found and fixed an actual
resolver discrepancy: agent diagnostics and dependency enforcement disagreed
for explicit coordination-directory contexts and relative context paths. Both
now use the shared live_roles_runtime resolver; relative paths are anchored to
the owning project, and project/runtime/config paths select the same ACP store.

Validation: 78 remote/local role, maintenance, observation and stale-deployment
gate tests passed. The matrix covers running, waiting_auth, waiting_input,
failed and idle roles, reused PID identity, missing contexts, unconfigured roles,
header/block overrides and local/remote separation. Maintenance inspection,
wake-check JSON and the domain resume validator agree on readiness in eight
local/remote state scenarios. Claims/states are synthetic and use the test
process identity; no live provider was invoked. Required-live-role enforcement
is retained. Other broad-suite failures and native fallback cleanup remain open.

### Complete broad-suite traversal and fixture porting (2026-09-07)

The broad suite now reached its end: 1359 passed, 9 failed, 1 skipped, 5 errors
in 272.48 seconds (/tmp/greatminds-modernization-suite-6.txt). Remaining failures
were references to deleted native inotify/deploy globals and obsolete --lang
setup fixtures. Retired native backlog, coord.yaml generation, staged keystroke
launch and inotify/deploy-driver tests. Kept domain live-developer/stand rules;
ported the actual requires_live_roles append-block test to a waiting_auth ACP
run, and user-feedback/body-file tests to current setup without weakening their
assertions. Native force-down/retry tests are not evidence for the new deployment
ledger's explicit recovery policy; its own tests remain required.

Validation after porting: 49 ACP-daemon/live-role/default setup checks passed;
45 user-feedback/body-file/stand/stale-lease checks passed. The full suite has not
yet been rerun after these final fixture updates, so a green full-suite result
remains unproven. Next: rerun the complete suite, investigate any real failures,
then continue removing obsolete runtime fallback paths and shipped native assets.

### Recovered full-suite result and ACP watchdog cutover (2026-09-07)

Recovered from another interrupted desktop session at dcee1a7. The prior suite-7
log ended without a summary, so it is not a successful validation record. A new
complete baseline run finished: **1366 passed, 1 skipped in 278.89 seconds**
(`/tmp/greatminds-modernization-suite-8.txt`). It collected the baseline before
this watchdog edit; the changed watchdog has separate targeted evidence below.

Removed watchdog's native PID registry, driven-lock and mirrored retry-file
inspection. It now observes ACP binding/run state and process identity through
the common observation service. Removed the prompt obligation to run watchdog
at every reviewer tick. Inspection uses the shared schema snapshot, respects an
explicit project despite an environment override, and detects stale YAML tasks
as well as Markdown data. Worktree inspection uses the selected schema/project
policy, reports the actual base path, and does not report an unavailable check
as healthy. Invalid schemas fail visibly instead of silently becoming empty.

Validation: **31 watchdog/worktree tests passed**. ACP fixtures cover idle,
running, authentication/input waits, failed/interrupted runs, and reused PID
identity; inspection leaves runtime file contents unchanged. Tests also cover
YAML/Markdown staleness, template exclusion, explicit project selection,
corrupt configuration/schema and unavailable worktree inspection. Native retry
file tests were replaced with ACP observations, not claimed as retry-policy
parity. No live agents or services were launched.

Next: unify wake-check with MaintenanceService and port its dead-dependency
regressions; continue native asset/setup cleanup, then finish the outstanding
B1/M3 retry/backoff/no-progress/account/retention policies and the remaining
milestone acceptance requirements. The full modernization goal is still open.

### One dependency verdict for daemon and CLI (2026-09-07)

Removed wake-check's parallel legacy parser, graph scan, readiness algorithm and
configuration-based branch. Text and JSON now present MaintenanceService
findings, using the same selected schema and explicit project resolution.
Removed the matching native fallback from the task transition validator;
terminal dependency identity, live-role holds and declared resume_to apply
regardless of execution.yaml presence. This also removes fail-open handling of
live-role inspection errors from that fallback. Markdown data is preserved and
reported as requiring conversion by the shared service, never silently resumed.

Ported dead-dependency regressions to valid persisted task fixtures and shared
structured findings: wrong terminal vs missing vs active vs satisfied, cascading
root cause, cycles, readiness failure without execution.yaml, read-only operation
and explicit project selection. Existing maintenance crash recovery and live-role
checks remain. Targeted validation: **88 tests passed**.

Built the wheel offline and installed it with dependencies into a separate venv.
The new `tests/smoke/installed_dependency_resume.py`, executed by that installed
Python, proves repeated setup preserves configuration, CLI reports readiness,
coordd --once applies exactly one SYSTEM resume, and a second daemon start does
not duplicate its journal record. The fixture has **zero agent runs**. Module
origin was verified under the installed venv, not the editable source tree.
Example project: /tmp/greatminds-dependency-wheel-project-8ri8h885. Run the script
with any isolated wheel-installed Python; it creates its own temporary fixture.
Updated the operations runbook to current ACP launch, auth diagnostics and
maintenance commands, removing the deleted restart --bootstrap instruction.

Full-suite rerun is recorded below when complete. Remaining work still includes
native assets/setup helpers, deterministic supervision policies, optional stand
dependencies, performance/budget measurements and the full milestone acceptance
audit. This installed-wheel fixture is not live-harness compatibility evidence.

Full validation after the dependency cutover: **1379 passed, 1 skipped in
282.59 seconds** (`/tmp/greatminds-dependency-full-suite.txt`). No collection
errors or failing tests. The standalone installed-wheel smoke is additional
validation, outside pytest collection.

### Minimal installation and optional Ansible profiles (2026-09-07)

Moved the existing validated ansible-core range to the `stands` extra and removed
inotify-simple, which has no remaining runtime imports after the ACP cutover.
Regenerated uv.lock offline without changing unrelated package versions. YAML
profile execution retains explicit missing-executable failure and now points to
`greatminds[stands]` in the running environment. This changes installation cost,
not deployment authorization or required stand/evidence gates.

Replaced the obsolete first-project guide (native setup --session, vendor plugin
installation and native configuration instructions) with the implemented ACP
setup, explicit manifest/auth, daemon/chat, observations and optional service and
stand workflow. README documents the optional extra. The selectable pipeline
presets and measured one-harness complete task campaign required by C2 remain
open; this checkpoint does not claim them complete.

Validation: base wheel built and installed offline into a fresh venv: **10
packages**, with no ansible or inotify module. The reproducible installed smoke
script's new `--without-stands` mode verifies absence, the actionable missing
Ansible error, repeated setup, exactly one SYSTEM dependency resume across two
daemon starts and zero agent runs. Fixture:
/tmp/greatminds-dependency-wheel-project-g71n0b3f. A separate fresh `[stands]`
installation installed **17 packages**, including ansible-core 2.17.14. All
**18 package/profile tests passed**, including real ansible-playbook syntax check
with synthetic local inventory and Ansible temporary state under /tmp. No remote
connection or deployment was performed. uv lock --check --offline passed.

### Shared literal environment inputs for stand execution (2026-09-07)

Found a remaining deterministic inconsistency: stand_executor parsed PROJECT.env
line by line, stripped quote characters and silently returned empty configuration
on read errors, whereas daemon preflight used EnvironmentFile semantics. Extracted
read_environment into core.service_environment and routed both readers through
it. Stand extra-vars and subsequent deployment evidence now see the same literal
multiline, quote, backslash and metacharacter values. Missing files remain
optional; malformed syntax, invalid UTF-8, unreadable files and broken symlinks
fail with sanitized errors before command execution. No shell evaluation occurs.

Validation: **54 environment/stand evidence/ledger/executor tests passed**;
**139 daemon/deployment/prerequisite tests passed** in the broader related run.
New end-to-end argv fixture inspects the temporary extra-vars JSON before its
cleanup and verifies exact synthetic values. Invalid-input cases forbid any
subprocess launch. No real deployment or provider call was performed.

This aligns file parsing, not every environment source: foreground coordd still
inherits its process environment while the installed service obtains PROJECT.env
through systemd. Explicit parity of foreground/service environment layering
remains to be completed. B1/M3 supervision policies and other outstanding
milestone requirements also remain open.

### Foreground/service configured environment parity (2026-09-07)

Closed the environment layering gap noted in the preceding checkpoint. coordd
now resolves current-process, PROJECT.env and selected-registration captured
values before entering serve. It establishes that environment before creating
threads and passes the same mapping to the ACP supervisor/command services;
stand/workspace subprocesses inherit the configured process values too. The
entrypoint restores the caller's environment on return or error. No registration
or credential capture is performed by foreground startup.

A unique registration for the exact project root is selected automatically;
multiple aliases require --project. Explicit names must exist and match the
resolved project directory even when both CLI options are provided. Unregistered
projects (including directories with spaces) use no captured file. Invalid files
or ambiguous/mismatched identity fail before serve. The inherited shell and
systemd base environments can still differ; the guarantee is the same explicit
configuration layering, not identical unrelated host variables.

Validation: **101 startup/default-ACP/service tests passed**. An additional
**26 startup/ACP-daemon tests passed**, including actual subprocess inheritance
of multiline values in unregistered, automatically registered and explicitly
registered cases. Failure restoration and no-launch invalid input checks passed.
The operations runbook documents precedence and restart semantics. No live
systemd service or provider was started. Remaining B1/M3 supervision policy,
legacy asset cleanup and full milestone acceptance work stays open.

### Explicit stand profiles without historical template substitution (2026-09-07)

Removed the hash-based runtime substitution that could select a packaged
worktree template instead of the named project profile. The loader now selects
the lease worktree's explicit coordination profile, then the main project
profile. Source/path evidence retains that choice. Removed the unused stale-hash
migration table and reseed implementation from setup; no installed legacy fleet
requires this mechanism. Profile updates are explicit project/worktree edits.

Retired tests solely for historical hash registration/reseed behavior. Retained
current template invariants for PATH, loopback transport, wheel command behavior
and .git exclusion/cleanup, plus the worktree precedence and deployment gates.
A new regression uses an actual historical full-deploy fixture, places a
competing packaged example in the worktree, repeats setup and proves the explicit
profile is selected and preserved byte-for-byte in both main/worktree cases.

Validation: **71 profile/template/evidence checks passed, 1 optional Ansible
syntax check skipped**; **65 deployment/ledger/default-ACP checks passed**.
No deployment, service or live provider was invoked. Execution contract documents
explicit profile selection. Other unused setup helpers/assets and the outstanding
B1/M3 policies and milestone acceptance audit remain open.

### Thin ACP setup and current operator documentation (2026-09-07)

Removed all remaining unused setup helpers: native session/hook command
formatting, env/context force-overwrite generators, native bootstrap copying,
root-copy migration, implicit stand seeding and duplicated queue/role constants.
Public setup is now a thin call to the common ACP bootstrap. No task data or
project files are removed by this code deletion.

Tests now exercise the actual setup command's created directories against the
effective schema and preservation of user PROJECT.env/PROJECT.md across repeated
runs. Kept lock/intent creation regression checks. Stand loader/registry/executor
tests explicitly copy chosen packaged examples as fixtures, instead of relying
on a dead setup API; packaged profile and metadata invariants remain tested.
Retired only assertions of obsolete generator/force-overwrite behavior.

Updated docs home, daemon architecture and CLI table from native windows/drivers,
synthetic wakes and deleted driven-log/restart commands to ACP operation. The
architecture page distinguishes current explicit retry behavior from unfinished
backoff policies. Fixed documentation build warnings for source-checkout probe
paths without implying those scripts are published documentation assets.

Targeted validation: **79 passed, 1 optional Ansible syntax check skipped**.
`mkdocs build --strict` succeeded without warnings, writing to
/tmp/greatminds-setup-docs. Full-suite result follows below. Native packaged
prompts/assets and remaining concept/installation pages still require their own
cutover audit; docs build success alone does not establish their semantic parity.
B1/M3 supervision policies and the other full-plan acceptance items remain open.

Full traversal: **1360 passed, 1 skipped, 2 failed in 282.09 seconds**
(/tmp/greatminds-post-setup-full-suite.txt). Failures were a registry fixture
mocking lookup without registering the project and a documentation assertion
requiring the removed native "Default tool" table. Replaced the former with
real registration in the suite-isolated registry and the latter with explicit
ACP execution/permission/chat requirements, retaining stand-policy assertions.
The **15 related startup/documentation checks then passed**. No runtime code was
changed after the broad traversal; a new all-green full-suite run is not claimed.

### Bounded durable ACP startup retries (2026-09-07)

Added per-binding max_startup_retries (0–20, default 2), retry_initial_seconds
(default 5) and retry_max_seconds (default 60). Only durable failed startup
transport/timeout outcomes explicitly recording prompt_started=false and
pre_prompt_activity=false qualify. Missing executables, permissions, protocol or
configuration errors, auth/input holds, interrupted/unknown outcomes, changed
contracts and post-prompt failures remain held. Any recorded command or result
also prevents automatic startup replay. Early session/permission/extension
activity is treated as insufficient evidence for safe retry.

The shared retry_policy computes exponential delay, next_at and attempt budget
from persisted runs. Restart does not reset the count/time. Queue assignment
uses it, automatic claims recheck it atomically alongside existing admission
gates, and accepted repeats record startup_retry_dispatch. Run status includes
retry timing/counts. Explicit operator retry permits one attempt without resetting
the unchanged revision's automatic failure budget. No model/provider fallback or
message-based error classification was added.

Validation: 83 configuration/supervisor/observation/policy checks passed;
37 policy/ACP-daemon checks passed; after adding the early-activity exclusion,
20 final policy checks passed. Controlled-clock scenarios prove 5s/10s delays,
cap enforcement, zero retry, budget exhaustion and restart persistence. The
supervisor/daemon integration injects startup TimeoutError and verifies exactly
three admissions at 1000/1005/1015, then no more. Actual ACP fixture scenarios
remain covered by the daemon tests. No live provider was called.

This advances B1/M3 but does not complete them: shared account/provider cooldown,
configurable no-progress policy, broader health/retention and resource metrics
remain required. Post-prompt uncertain work is not automatically replayed.

### Account-wide startup backoff (2026-09-07)

Added project account_retry_initial_seconds (default 5) and
account_retry_max_seconds (default 60). Recognized pre-prompt startup failures
form a shared streak across tasks, role bindings and conversations with the same
configured account. A capped exponential delay gates new tasks and operator
retries too; other account groups remain independent. Existing active work is
not cancelled. Durable prompt-start evidence or an active configured session
resets this connectivity streak, without implying domain progress or approval.

The account verdict is computed once per account in each assignment snapshot,
exposed in run status, and rechecked under the claim transaction for both queued
and interactive admission. A delayed conversation retains its queued message.
No provider-prose parsing, model switching or credential-identity inference is
introduced. Failure timestamps/counters remain derived from durable outcomes,
so reopening the store/daemon does not reset backoff.

Validation: 96 policy/configuration/supervisor/observation checks passed;
44 account/ACP-daemon/conversation integration checks passed in 77.03s;
30 final policy checks passed after caching per-account observations. Scenarios
cover multiple bindings/tasks, independent accounts, capped delay, restart,
explicit retry, interactive message preservation and invalid budgets. ACP
integration uses local synthetic processes; no live provider was invoked.

Remaining B1/M3 work includes configurable no-progress behavior, provider-reported
rate-limit handling, operator-resettable account circuit breaking, health/log
retention and broader budgets/metrics. Account grouping here is project-local;
it does not claim cross-project credential coordination. Other full-plan
acceptance requirements remain open.

### Durable workflow no-progress budget (2026-09-07)

Added max_no_progress_turns per binding (1–20, default 1). Completed background
turns count within the task's queue stage; token updates, process liveness,
metadata-only revision changes and daemon restart do not reset the count.
Applied handoff/blocked receipts or an intervening queue stage reset it. User-paced
conversations are excluded from automatic background turn counting.

With an explicitly larger budget, normal completed turns can continue after
retry_initial_seconds when contracts still match and no command/result or other
admission gate holds the task. Unknown post-prompt failures, including those that
changed task bytes, do not acquire retry authority from that change. Human-input,
no-change and rejected result holds survive task metadata updates. Explicit
operator retry permits one additional run, never erases the prior count.

Renamed the shared admission function to retry_admission to reflect startup and
no-progress decisions. The scheduler and atomic claim use it; admitted
continuations emit no_progress_continuation. Run status exposes per-assignment
progress counters/limits. The default observed reason is now no_progress_limit
for a completed unadvanced task, rather than the ambiguous revision_already_attempted.

Validation: 78 initial policy/config checks passed; 85 ACP-daemon/conversation/
observation/policy integration checks passed in 97.93s; after tightening changed
revision handling, 47 final policy checks passed. Tests cover default/expanded
budgets, delay, store reopen, explicit retry, metadata churn, command/semantic
holds, invalid limits and actual local ACP echo processes stopping after exactly
two configured turns. No live provider was invoked.

Remaining supervision/product work includes provider rate-limit handling,
operator-resettable account circuit breaking, health/retention, richer budgets
and performance metrics; native assets, remaining docs and the full milestone
acceptance audit are also still open.

### Stable account recovery evidence (2026-09-07)

Replaced mutable running-state/prompt-outcome inference with startup_ready_at,
recorded once when a configured ACP session first becomes running. Account
backoff orders that durable observation alongside startup failures. Permission
resumes and crash recovery cannot erase a newer failure or resurrect an older
failure streak by moving the apparent time of a successful connection.

Validation: 87 account/startup/no-progress/runtime contract checks passed in
21.14s, including store reopen after crash recovery and permission resume after
a newer account failure. No live provider was invoked. This checkpoint does not
close the remaining modernization milestones listed above.

### ACP installation and update guidance (2026-09-07)

Rewrote installation, upgrading and the former native Codex profile guide around
ACP manifests/bindings, separate harness authentication, optional stands and
explicit service installation. Documented installed-service try-restart and
manual foreground restart; removed the unsupported daemon release-notification
claim. Corrected project schema's drift hint: setup preserves existing mirrors.

Validation: strict MkDocs build passed in 1.54s. Behavior descriptions were
checked against bootstrap, update, configuration and project-schema code.
Remaining concept/architecture pages and packaged native assets still need the
cutover audit; this checkpoint does not claim that cleanup is complete.

### ACP architecture and role concepts (2026-09-07)

Replaced native lifecycle/PTY wake descriptions with queue and on-demand ACP
admission, durable conversations, configured session policy and deterministic
supervision. Architecture now describes the daemon's domain responsibilities,
pinned contracts and uncertain side-effect recovery. Updated role descriptions
and filesystem layout to expose execution.yaml and private runtime evidence.

Validation: strict MkDocs build passed in 1.48s. The preceding installation/CLI
checkpoint also passed 8 project-schema/public-help checks in 0.76s. Packaged
bootstrap, native profile/template files and tests that assert their old behavior
remain under audit; none are represented here as current setup output.

### Full regression checkpoint before native asset removal (2026-09-07)

The full suite after durable account recovery completed with 1411 passed,
1 skipped in 297.41s. Subsequent changes through the architecture-doc checkpoint
are documentation and a schema-drift hint; the hint's CLI checks passed separately.
This is a regression checkpoint, not completion of the modernization plan.

### Remove unused native package assets (2026-09-07)

Removed static native bootstrap/coord.yaml templates, Codex per-role profiles,
Claude plugin manifests and default native MCP configuration from package data.
Removed unused project-bootstrap/doc path helpers. Setup already uses only ACP;
these files had no execution consumer and contradicted generated run context.
Retired assertions about native panes/profiles; retained role/FSM checks and
added actual ACP-context coverage for CLI mutations and daemon-owned scheduling.

Preserved Explorer's destructive-target boundary in the shared role schema:
local orchestration or unresolved targets are forbidden; destructive scenarios
require an explicitly authorized disposable stand and verified target identity,
otherwise a blocker. A real claimed-run context test verifies delivery across
harnesses. This is a contract boundary, not a claim of OS shell isolation.

Validation: 83 affected context/setup/role/data checks passed in 6.41s. Built and
installed a fresh wheel into /tmp/greatminds-acp-clean-installed offline (10 base
packages). Inspected wheel members to confirm native assets are absent. Installed
smoke passed: idempotent setup, one deterministic dependency resume, zero agent
runs and no duplicate replay; base installation has no Ansible dependency.
Remaining schema prose/event-trigger cleanup, supervision improvements and full
milestone acceptance remain open.

### Remove native wake machinery and agent polling policy (2026-09-07)

Removed the unused _send_enter module and its native-TUI tests. No production
module consumed it. Removed unused schema event_wake, heartbeat hang thresholds,
Claude permission defaults and wake-mechanism glossary. MAINTAINER is now an
operator-paced diagnostic role for unknown failures and bounded repair proposals;
it has no self-loop timer, routine restart or lease-sweep duties. Schema glossary
and inbox guidance no longer require polling at the start of every turn.

Role queue ownership and transition/evidence gates remain in force. This change
also corrects the schema's obsolete claim that setup installs a git permission
hook. Direct harness shell access still requires the documented cooperative
boundary; deleting native defaults does not create OS enforcement.

Strict MkDocs build passed in 1.44s. Affected lifecycle/default configuration,
compiled context, schema validation and local ACP daemon checks passed; exact
count recorded below. The packaged COORDINATE reference still contains native
lifecycle prose and is the next cleanup target. Remaining full-plan acceptance
requirements are unchanged.

Validation result: 128 passed in 56.72s. Tests used synthetic local ACP agents;
no real provider or service was invoked.

### ACP coordination reference and full cutover regression (2026-09-07)

Reworked packaged COORDINATE.md around daemon-owned claims, typed results,
deterministic dependency resumption and durable operation recovery. Removed
static bootstrap, native driver/plugin loading, mandatory queue/inbox polling,
agent-authored journals/intents and final-line marker requirements. Corrected
claims that no central daemon exists or journals can be reconstructed from the
current queue snapshot. Display templates remain optional presentation.

Preserved stand verification rules, profile registry conventions, independent
review and the narrow section 9.1 self-blocker carve-out. Documented deployment
freshness and explicit runtime authorization. Removed a prose-only MAINTAINER
commit exception that contradicted schema git_permissions. No task data or
workflow transition table changed in this checkpoint.

Validation: the complete suite after native asset/wake removal and reference
cleanup passed with 1375 passed, 1 skipped in 299.35s. Strict documentation build
passed in 1.42s. Retired two tests requiring agents to manufacture final-line
markers; schema display-template checks and substantive domain gates remain.

Next concrete C4 gap confirmed in code: supervisor records context_bytes but
has no configured maximum input/context size. Implement explicit bounded
admission for oversized context without silent truncation or model switching;
include interactive input and persisted-session limits in the design. Remaining
compatibility, diagnostics, retention and full milestone acceptance stay open.

### Durable client-input budgets (2026-09-07)

Added binding max_prompt_bytes (262144 default) and max_session_input_bytes
(1048576 default), validated as positive integers and included in contract
fingerprints. The supervisor checks complete UTF-8 prompt text, including user
messages, before sending; an oversized initial prompt never launches ACP.
Session input is reserved under the store transaction before each ACP send and
accumulates across loaded runs. Uncertain sends retain their debit. Reservation
identities are idempotent; unknown prior session input fails explicitly instead
of appearing as zero. No truncation or implicit session/model replacement.

Run outcomes expose input_budget_exceeded with limit/used/requested bytes;
reservation events and metrics expose counts without prompt text. These limits
bound client input, not provider tokens, internal tool context, output, actual
context-window occupancy or cost. Those distinctions are documented explicitly.

Validation: initial supervisor/runtime regression 63 passed in 22.64s; initial
input/conversation checks 29 passed in 28.98s; expanded input/supervisor checks
40 passed in 28.33s; final boundary tests 17 passed in 9.52s. Scenarios include
UTF-8 boundaries, no process on oversize, actual ACP success at the exact limit,
multiple interactive messages, session reload, recovery/reopen, duplicate
reservations, unknown prior usage and distinct-session isolation. Strict docs
build passed in 1.47s. All ACP calls used local synthetic agents.

C4 still requires broader timing/usage observations and representative baseline
comparison. C5 aggregate diagnosis/local bundles, retention and the remaining
real compatibility/acceptance campaign are not closed by this budget feature.

### Durable execution-stage observations (2026-09-07)

Added first-occurrence monotonic offsets for workspace/context readiness,
launch-gate process recording, protocol initialization, configured session,
first prompt start, first protocol activity, first prompt activity and completed
cleanup. Each observation is persisted immediately in run.timings and a runtime
event. Repeated observations preserve the original value; interrupted recovery
preserves observed stages without inventing later stages or zero durations.

The common run snapshot exposes these fields for all operator frontends.
Documented exact boundaries: launch gate is not actual harness exec, first
activity can be loaded history, prompt start is a client boundary, and none of
these measurements demonstrates useful work, validation or domain approval.
Missing measurements remain unknown. Queue wait and per-message distributions
are not claimed by these execution-relative first-occurrence offsets.

Validation: 41 supervisor/input-budget regression checks passed in 26.63s;
19 timing/ACP-conversation checks passed in 33.60s. Tests include actual local
ACP stage ordering, prelaunch input rejection, restart preservation, immutable
first observations and rejected invalid/foreign observations. Strict docs build
passed in 1.48s. No live provider or external service was invoked.

Remaining C4 work includes representative baseline/pipeline measurements,
queue/validation timing and reliable provider usage reporting where available.
Other plan milestones remain open; instrumentation alone is not performance
acceptance or a claim of end-to-end speedup.

### Aggregate local diagnostic findings (2026-09-07)

Added greatminds run doctor (text/JSON) with versioned component checks,
findings, severity, selected evidence, suggested actions and summary counts.
Reuses effective configuration, shared static agent prerequisites, ACP operator
rows, assignment admission, dependency maintenance and deployment ledger reads.
Extracted agent_prerequisites so daemon doctor uses the same check implementation.

Independent components continue after inspection failures. Errors expose class
names instead of raw exception messages; reports exclude prompt bodies, command
argv/output and raw result envelopes. Authentication, dependency, backoff and
uncertain-operation holds remain distinct. Diagnosis does not write runtime
state, launch agents or execute repairs. Documented exit/status semantics and
scope: no-findings is not a live-provider or atomic daemon-health verdict.

Validation: 6 prerequisite/operator regression checks passed in 1.22s;
9 initial aggregate/public-help checks passed in 1.56s; final 12 aggregate,
prerequisite and operator checks passed in 1.55s. Cases cover read-only repeatable
inspection, secret exclusion, malformed operation collections, broken config
without hiding deployment faults, and distinct auth/dependency holds. Strict
MkDocs build passed in 1.48s.

C5 remains open: add the local diagnostic bundle, integrate watchdog-specific
stale intent/task/worktree findings, and consolidate bounded recovery actions.
The aggregate is implemented as a domain report usable by the future web UI;
no additional frontend state machine or live-provider probe was added.

### Private local diagnostic bundle (2026-09-07)

Added run doctor --bundle PATH. The versioned JSON export contains installed
library versions, configuration fingerprints/binding limits, selected findings,
recent run timing/metric/state records and event headers. Identifiers become
salted references consistent within an export; salt/mapping are not exported.
Raw configuration/env values, argv, task/prompt bodies, output, result payloads
and event bodies are excluded. Metadata is still diagnostic information, not a
guarantee of complete anonymity.

Defaults bound export to 100 runs, 200 events and 200 findings (errors first),
with truncation flags and full diagnostic summary counts. Unknown fields stay
null/unavailable. Serialization has a 5 MiB ceiling. Publication uses a private
0600 temporary file and atomic no-replace link; existing files/symlinks are never
overwritten. No upload, external message, provider probe or repair is performed.

Validation: initial 14 export/diagnostic/public-help checks passed in 1.63s;
final 15 passed in 1.84s. Cases cover privacy, reference linkage, bounded windows,
unknown metrics, malformed runtime, atomic/private no-overwrite publication,
CLI error reporting and total size limits. Strict MkDocs build passed in 1.49s.
Built a fresh wheel, installed offline into /tmp/greatminds-diagnostics-installed
(10 base packages, no Ansible), and passed installed dependency-resume smoke:
one system resume, zero agent runs and idempotent setup. The installed CLI also
exported a readable private bundle successfully for that toy project.

Full regression after input budgets, timing and diagnostics passed: 1407 passed,
1 skipped in 307.96s. C5 still needs shared watchdog-specific findings and
recovery-action consolidation. Other full-plan milestones remain open.

### Shared filesystem health checks (2026-09-07)

Extracted orphan-intent, stale-task and orphan-worktree inspection into shared
domain functions used by watchdog and run doctor. Bundles retain these finding
codes and pseudonymized artifact references. Thresholds and queue kinds come
from the effective schema. Parking queues protect associated worktrees; custom
terminal queues no longer hide orphans. Explicit verified/archive retention
policy prevents false orphan reports. Numeric sequence aliases remain supported
without treating arbitrary four-character slug prefixes as task identities.

Directory enumeration now propagates access failures rather than allowing glob
to silently report an empty healthy directory. A concurrently disappeared file
is skipped; a failed component remains visible in doctor and prevents watchdog
from printing All clear. Inspection remains read-only and never authorizes
pruning, removes an intent, or mutates workflow state.

Validation: initial 24 existing watchdog/doctor/bundle checks passed in 2.81s;
32 expanded shared-health/public-help checks passed in 3.82s; final 31 health,
watchdog, diagnostic and bundle checks passed in 2.95s after retention-policy
coverage. Cases cover YAML/Markdown thresholds, template/non-file exclusion,
custom terminal/parking queues, numeric aliases, configured retention, directory
access failure, cross-surface findings and private artifact names in bundles.
Strict docs build passed in 1.45s before the final retention clarification.

C5 still requires consolidation of bounded repair actions with their preconditions
and evidence. The previous full-suite checkpoint (1407 passed, 1 skipped) predates
this extraction; current validation is the affected suites above. Full-plan
compatibility, pipeline presets, performance acceptance and retention remain open.

### Scoped bounded recovery descriptions and idempotent controls (2026-09-07)

Diagnostic findings now expose supported recovery_actions with argv, explicit
project environment/cwd, effect, required inputs and preconditions. Text doctor
prints scoped suggestions. Actions are descriptions only; existing services
still enforce operation state, revision/dependency gates, source/destination
conditions and deployment cleanup/locking. No generic forced-repair or external
command replay was introduced. Bundles omit executable descriptors containing
raw project paths and operation identifiers.

Command/deployment resolution and maintenance abandonment return the original
receipt when repeated with the same explanation; different explanations are
rejected. Repeated previously requested prepared/applied maintenance repair
returns existing state without another event. Failed reconciliation can still
be explicitly requested again after correcting its cause. Partial agent context
(RUN_TOKEN alone as well as RUN_ID) is rejected by all mutating recovery controls.
Resolution never manufactures passing evidence or deployment readiness.

Validation: 62 initial command/maintenance/deployment/diagnostic checks passed in
14.69s; 87 expanded recovery, process, diagnosis and bundle checks passed in
17.15s; final 23 recovery-surface/diagnostic/public-help checks passed in 2.00s.
Tests include immutable repeated receipts, conflicting explanations, unchanged
state/events on duplicate requests, deployment cleanup prerequisites, token-only
operator denial and a suggested command executed from another cwd against the
correct project. Strict docs build passed in 1.50s. No live provider was invoked.

C5 now has aggregate local findings, shared filesystem scans, private bounded
exports and descriptions backed by existing deterministic recovery services.
Full milestone acceptance still requires auditing their combined coverage;
remaining C2 presets, C4 comparative performance/resource work and real harness
compatibility requirements are not satisfied by these diagnostic controls.

### Explicit role presets and local TESTER contract (2026-09-07)

Added project presets and preview/apply commands for local, UI, documentation,
deployed and full role rosters over an existing named ACP manifest. Applying a
preset fills empty bindings atomically under the setup lock; identical repetition
preserves bytes, and conflicting existing bindings are rejected. Agent manifests,
configured commands and schema transition/evidence gates remain unchanged. All
preset permissions are ask; interactive roles remain on demand. The first-project
guide now starts with a manifest and a selected roster. Agent context includes the
configured roles from the run's frozen execution contract.

TESTER instructions now distinguish local tasks requesting declared daemon
commands from stand-required tasks with lease, readiness and actual SSH evidence.
No validation gate was relaxed. A single synthetic ACP executable completed the
local preset's developer/tester/reviewer pipeline with three real command receipts,
typed results, daemon merge and restart without replay; its on-demand planner did
not consume a background run. This starts from a prepared task and does not claim
fresh-install live-provider onboarding acceptance.

Validation: 86 preset, runtime-contract, role-contract and public-documentation
checks passed in 5.74s; both original and local-preset integration pipelines passed
in 25.76s. Strict documentation build passed in 1.47s; diff whitespace check passed.
C2 live-provider first-task acceptance and comparative handoff overhead remain open.

Full regression checkpoint after shared filesystem health, scoped recovery and
execution presets: 1429 passed, 1 skipped in 328.45s. No live provider was invoked.
Implementation checkpoint: 309a60e. Continue with the outstanding acceptance
items above; this checkpoint does not mark the entire modernization complete.

### Measured idle conversation scheduling overhead (2026-09-07)

The daemon previously reread and scanned all runs for each open conversation.
It now builds one live-conversation index per pass, skipping that read when no
conversation journals exist. This is an observation only: each new claim still
rechecks current capacity and identity under the store lock. Closing active
conversations continues to request cancellation before acknowledging closure.

Added tools/daemon_idle_benchmark.py: real store APIs create 50 idle conversations
and 100 cancelled historical claims; warm-up is excluded, five complete daemon
passes are measured, agent launch is forbidden, and unchanged runtime history is
asserted. On this host the baseline at 3b3db47 used 67 runtime reads per pass and
1.105 s median; the updated loop used 18 reads and 1.008 s median (about 9% lower).
Recorded evidence is evidence/daemon-idle-2026-09-07.json. These are synthetic idle
passes, not productive task latency or provider performance. No state was pruned.

Validation: 61 conversation/runtime/recovery checks passed in 32.53s. The expanded
idle/conversation/daemon/account campaign passed 47 checks; its new capacity test
initially asserted the fixture's project default incorrectly (4 rather than its
binding limit of 1). After correcting that assertion, the test passed in 5.64s:
three ready conversations completed with at most one claimed nonterminal run.
The idle regression checks constant runtime-read growth and unchanged history.
Strict docs build passed in 1.52s. The earlier full-suite checkpoint remains
1429 passed, 1 skipped; no new full-suite claim is made for this change.

C4 now has a reproducible measured reduction for one concrete daemon bottleneck.
Comparative productive-pipeline overhead and the remaining plan acceptance items
are still open. The previous goal turn was progress: committed presets and a full
regression checkpoint. This turn changes runtime behavior with measured evidence.

### Live one-harness preset and fresh wheel acceptance (2026-09-07)

Extended the reproducible pipeline probe with --local-agent: initialize a fresh
Git project through the public setup CLI, apply the public local preset, and run
its explicit synthetic no-stand clamp task. Both the original mixed configuration
and new setup/preset fixture passed (2 tests in 27.52s).

Real Codex 0.153.4 / codex-acp 1.10.0 completed developer, tester and reviewer with
one manifest, permission ask and no permission callbacks. Three typed results
were applied with SYSTEM provenance; three daemon unit-test commands succeeded.
Only clamp.py changed in the merge, the task worktree was removed, all six tests
passed independently on the merged code, and restart preserved runs, results and
commands. Planner remained on demand. Context sizes were 7,887 / 9,347 / 10,419
bytes; run durations were 45.051 / 46.685 / 32.709 seconds. Detailed allowlisted
observations are in evidence/acp-local-preset-2026-09-07.json. This task's plan was
provided as fixture input; interactive planning was not part of the scenario.

Built the current base wheel offline and installed it in a fresh isolated venv
without the stands extra. Verified module resolution from site-packages. The same
public setup/preset pipeline passed with the synthetic ACP server, including merge,
evidence, worktree cleanup and restart; a separate installed smoke confirmed
idempotent setup, one dependency SYSTEM resume and zero agent runs. The wheel hash
and assertions are in evidence/installed-local-preset-2026-09-07.json.

C2 now has the requested fresh local repository, one real harness, separate review
roles and no remote stand, together with independent wheel packaging evidence.
These scenarios do not prove all roles on all harnesses. Strict documentation
build passed in 1.50s before the final installed-evidence paragraph.

Also revalidated the currently installed Cline, which now reports 3.0.61 (older
session evidence was 3.0.50). Session creation succeeded, but the first synthetic
prompt failed with JSON-RPC -32603; private stderr identified re-authentication
required. Cleanup completed and restart/load was skipped after initial failure.
No account/model selection was changed. Sanitized evidence and the compatibility
matrix record this limitation; Cline live completion requires restored login.

Next confirmed acceptance gap: the VS Code extension has only mock-API tests;
code and xvfb-run are available for a real extension-host smoke. B2 retention,
remaining protocol/harness coverage and the full requirement audit remain open.

### Real VS Code extension host and literal terminal argv (2026-09-07)

Added a real Linux extension-host smoke and isolated launcher, following VS Code's
extensionDevelopmentPath/extensionTestsPath runner contract. The system editor was
1.84.0, below the extension's declared ^1.92.0 engine, so an official standalone
1.92.2 distribution was downloaded to /tmp; no installed editor/profile changed.
The smoke activated the development extension, exercised real CLI metadata calls,
observed actual terminal processes/argv for events, coordd and dashboard, and
confirmed process termination on disposal with zero configured agent runs.

This exposed an existing inconsistency: cockpit commands used shell-interpolated
CLI strings while chat already used explicit terminal argv. All cockpit commands
now use shellPath/shellArgs too. A CLI symlink containing spaces and $(literal)
worked literally in the real host. Existing chat selection and domain ownership
remain unchanged. Quick-pick interactions and live chat were not exercised in this
host smoke; their existing separate coverage is not relabeled as host coverage.

Evidence: evidence/vscode-extension-host-2026-09-07.json. Six direct Node unit
scenarios passed, npm test passed, and six Python scaffold/public-doc checks passed
in 0.68s. Host activation, CLI backend and three process/cleanup checks passed on
VS Code 1.92.2. The reusable launcher isolates user data/extensions, disables update
and telemetry settings, preserves local logs, and bounds/cleans its process group.

C7's real extension-host smoke now exists and passes. Remaining B2 retention,
protocol/harness coverage and final requirement-by-requirement acceptance are
still open; this does not mark the full modernization complete.

### Atomic bounded runtime event retention (2026-09-07)

Added project max_runtime_events (100..1,000,000; default 10,000). The exclusive
supervisor installs the policy; runtime transactions enforce it atomically with
their own writes. Overflow retains roughly 80% of the configured tail and appends
one events_pruned marker, with cumulative discard metadata in the snapshot. This
avoids a new scan/controller on every daemon tick and avoids a marker per write.
Run and event sequences now derive from the last durable sequence, not list length.

Run identities, event/result idempotency receipts, contracts, task files, command
and deployment evidence and conversation history are untouched. Pruning bounds
the runtime event count, not total project storage. The optional systemd journal
continues to use host policy; ACP stderr and command/chat outputs retain their
existing per-output bounds. No raw native executor logs remain in the managed path.

run events emits an explicit synthetic events_gap before the retained page when a
cursor is too old; follow advances once past that gap. Diagnostic bundles preserve
numeric retention metadata and report truncated history even when all retained
events fit the export. Failed atomic publication leaves the original operation
and log together intact. Repeating the same policy is byte-preserving.

Validation so far: 66 existing runtime/bundle/event/daemon checks passed in 48.79s;
38 budget/account/idle and new boundary checks passed in 13.24s (two new tests first
needed their existing fixture imported, then passed); all 11 retention cases passed
in 2.81s; final 15 retention/cursor cases passed in 2.91s. Cases exercise durable
deduplication after old events disappear, monotonic later claims, actual task bytes,
atomic-write failure, invalid limits, expired cursors between follow polls, bundle
truncation, and daemon restart with zero harness launches. Strict docs passed in
1.56s. Full-suite validation is running and will be recorded separately.

The 50-conversation idle benchmark still performs a constant 19 runtime reads per
complete daemon invocation (18 before the one startup policy transaction), rather
than one additional read per conversation. The timing sample overlapped the full
suite and is not used as a new latency comparison. The prior completed goal turn
was progress: real extension-host acceptance and a committed CLI launch fix.

Full traversal after event retention: 1441 passed, 1 skipped, 1 failure in 363.21s.
The sole failure was the public-doc wording guard matching the word historical
in the idle benchmark's fixture description. Rephrased it as cancelled claims;
all eight affected documentation/reference checks then passed in 0.63s, and strict
docs passed in 1.51s. The late-added follow-gap test was not part of that already
collected full run; it passed in the separate 15-case cursor/retention campaign.
No runtime failure was reported by the full traversal. This records the actual
verification scopes rather than claiming a second complete full-suite run.

### Requirement-by-requirement acceptance audit (2026-09-07)

Created modernization-acceptance.md against every foundation/track requirement,
named milestone and acceptance deliverable. Inspected current dispatch, transport,
context, observation, recovery and process code; relevant concrete fixture cases;
installed/live/host JSON evidence; and public setup/release documentation. The
matrix distinguishes implemented behavior from remaining proof, and does not turn
initialization-only harness records into support claims.

Seven explicit gaps remain: bounded managed protocol diagnostics (G1), reliable
usage/cost observations and supported budgets (G2), complete useful task timing and
productive comparison (G3), shared admission from explicit provider quota signals
(G4), generated mechanical reference (G5), remaining live harness coverage (G6),
and a combined aggregate-doctor-to-repair acceptance scenario (G7). The audit gives
each its evidence, scope and next verification. Whole-plan completion remains
unproven; no new database, distributed scheduler or web scope was introduced.

Corrected two confirmed publication gaps: installation/README now specify the
actual Linux /proc/pidfd host envelope rather than generic POSIX, and Unreleased
notes describe the current ACP/deterministic implementation with separate offline,
installed-wheel, live-provider and extension-host verification limits. Package
version and released history were not changed; nothing was published.

Validation: all eight documentation/reference checks passed in 1.44s and strict
docs in 1.50s; diff whitespace check passed. The previous goal turn was progress:
atomic event retention and its regression evidence were committed. Next work is
G1 in the normal supervisor path, followed by the finite open list in the audit.
