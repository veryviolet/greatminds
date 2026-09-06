# Greatminds modernization plan

Date: 2026-09-06. Status: implementation plan; runtime changes not yet made.
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

The next implementation step is the shared run contract in M1. ACP execution
and automatic workflow controllers are still pending, not implemented by M0.
This document authorizes no release or migration of existing live fleets by
itself; those operations are separate from developing and testing the changes.
