# Greatminds modernization plan

Date: 2026-09-06. Status: implementation in progress; see Progress below.
Baseline and evidence: [initial audit](rehabilitation-audit-2026-09-06.md).

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

After parity tests and configuration migration, remove the direct Claude
subprocess driver, Greatminds' Codex app-server implementation, generic
headless drivers, and TUI wake/sleep signaling. Remove their obsolete units,
setup artifacts, and documentation through an explicit upgrade migration.

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
Upgrade with dispatch paused/drained, a recoverable state snapshot, validation,
then resume. Define rollback limits when a migration is irreversible. Avoid
floating adapter upgrades under live sessions.

Acceptance: clean install, repeated setup, migration of a customized project,
interrupted upgrade, and supported rollback have reproducible local fixtures.

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
| M6 — complete cutover | Legacy removal, C3 migration, C7 documentation | Clean install and upgrade work entirely over ACP |
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

## Recovery checkpoint

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
