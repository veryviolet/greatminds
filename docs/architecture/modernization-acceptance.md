# Modernization acceptance audit

Audited against [the approved plan](modernization-plan.md), starting at commit
`7df519a`, on 2026-09-07. This is a delivery audit, not a replacement scope.
The owner confirmed no production deployment or installed fleet migration is
required. Project task data and actual crash recovery remain required.

“Confirmed” below means the named implementation and relevant behavior evidence
were inspected. It does not turn fixture coverage into live harness coverage.
“Open” identifies an explicit requirement whose current proof is insufficient.

## Foundation and execution

| Requirement | Inspected implementation/evidence | Result |
| --- | --- | --- |
| F1: isolate service tests, credentials and user configuration | Test isolation fixture; daemon install/environment suites; full offline traversal | Confirmed for the isolated suite; no real service installation in fixtures |
| F2: one effective schema and pinned in-flight contracts | `core/schema.py`, runtime contract snapshots; `test_schema_snapshot.py`, `test_domain_results.py::test_context_uses_pinned_tables_without_mutating_global_schema` | Confirmed: installed/explicit canon is authoritative; mirrors are inspected without silent replacement |
| F3: shared filesystem/FSM operations and task preservation | `domain/results.py`, `domain/maintenance.py`, workspace/stand services; actual handoff and recovery fixtures | Confirmed for managed operations; task identity, block provenance and queue gates persist |
| F4: project/task revision/binding/run/session/result identities | `runtime/store.py`, `runtime/interactions.py`; atomic competing claims, duplicate events/results and stale-revision rejection | Confirmed; identity and idempotency records survive event-tail pruning |
| F5: filesystem store with atomic publication and crash reconciliation | Store transactions, launch gates, result/maintenance/deployment journals; injected crash stages | Confirmed; uncertain external commands are held rather than replayed |
| A1: independent manifests, roles, model/mode, permission, workspace and scheduling | `runtime/config.py`; `project execution`, `daemon doctor`; capability/auth/model confirmation tests | Confirmed configuration separation. Version labels describe declared tested installations; doctor does not claim live compatibility |
| A1: versioned compatibility deliverable | [Compatibility campaign](acp-compatibility.md), adapter package lock and dated JSON evidence | Present; remaining live combinations are explicitly open under A4 |
| A2: common SDK stdio lifecycle and request correlation | `runtime/acp_transport.py`, ACP SDK 0.11.1; real subprocess negotiation, ordered updates, disconnect, cancellation and cleanup tests | Confirmed for implemented protocol surface |
| A2: permission callbacks, path policy and explicit waiting | `runtime/permissions.py`, supervisor; pending/answered/denied/expired/stale/crash fixtures and live callback evidence | Confirmed. Filesystem/terminal callbacks are not advertised; unavailable methods fail explicitly |
| A2: normalized protocol diagnostics with bounded raw evidence | Bounded `run.protocol` negotiation/tool/error/stop facts, private summary export; real supervisor privacy/saturation/crash fixtures and delayed-update RPC-error regression | Confirmed G1: managed-path evidence, with no raw payloads or transcripts |
| A3: two upstream bridges and a native ACP implementation on one client | Codex + Claude bridges and native Grok; [completed mixed pipeline](evidence/acp-mixed-pipeline-completed-2026-09-06.json), lifecycle/permission evidence | Confirmed tested versions and completed local pipeline; no cross-harness history transfer claimed |
| A4: Qwen/Kimi/Grok/Cline/Gemini/OpenHands/Cursor distribution, auth, session, worktree, model and cancellation | Per-stage [campaign evidence](acp-compatibility.md); fresh Cline 3.0.61 prompt failure requires renewed authentication | **Open G6:** only initialized/session-created status for several installations; authentication and complete scenarios remain necessary |
| A5: daemon-owned interactive prompts, streamed output, permission, reconnect and interrupt | Conversation FIFO, task-bound claims, shared broker; `test_acp_conversations.py`, `test_chat_tasks.py`, `test_chat_terminal.py` | Confirmed fixture coverage; live coverage remains explicitly narrower than all roles/harnesses |
| A5: common tmux/VS Code surfaces and removal of native dispatch | `runtime/frontends.py`, thin `cli/coordd.py`, extension CLI terminals; launch tests and [real extension host](evidence/vscode-extension-host-2026-09-07.json) | Confirmed managed execution routes through ACP; host smoke does not exercise Quick Picks or live chat |

## Deterministic orchestration

| Requirement | Inspected implementation/evidence | Result |
| --- | --- | --- |
| B1: claims, supervision, cancellation, startup/periodic reconciliation, process trees | `runtime/daemon.py`, `supervisor.py`, `processes.py`; exclusive-supervisor, launch-gate, surviving-child and stale-identity fixtures | Confirmed on the documented Linux host interface |
| B1: bounded retries and no-progress policy | `retry_policy.py`, durable run state; startup/account/no-progress suites | Confirmed for startup failures and unchanged task turns |
| B1: provider/account limits do not cause independent retry storms | Shared startup backoff exists; post-prompt failures are held | **Open G4:** no explicit shared provider-quota/rate-limit admission control from reliable signals; startup backoff alone is not the whole requirement |
| B2: known maintenance, dependency release, impossible dependencies/cycles | Shared dependency verdicts and journaled SYSTEM maintenance; missing/cyclic/changed/live-role fixtures and installed smoke | Confirmed; ordinary dependency release invokes zero agents |
| B2: dead-holder leases, cleanup, log retention and deduplicated findings | Lease/deployment recovery, `filesystem_health.py`, `event_retention.py`, maintenance finding fingerprints | Confirmed specified local behavior; evidence/task/conversation records are preserved and external systemd logs use host policy |
| B2: maintainer limited to semantic diagnostic work | Shared schema marks maintainer on demand and forbids polling/recovery loops | Confirmed instructions and scheduling contract |
| B3: concrete assignment, role/task/gates/results/artifacts and pinned context | `context.py`, `RunStore.contracts`, CLI contract inspection and scoped commands | Confirmed; roles receive their assignment rather than scanning queues |
| B3: compatible session resume and changed-contract handling | Supervisor session selection and conversation acquire/revision checks; load/mismatch/task-change tests | Confirmed; changed contracts/revisions do not silently consume pending input |
| B3: prompt size, useful-work time and turns against baseline | Input reservations, run stages, queue observation/accepted-transition waits, preparation attempts, receipt resolution and conversation timing | **Open G3:** durable timing boundaries now have recovery/pipeline coverage; productive distributions and equivalent baseline comparison remain unproven |
| B4: typed results, gates, idempotency and SYSTEM provenance | Result envelopes/service, shared CLI validators; crash-point, duplicate, dynamic-gate and invented-authorship tests | Confirmed; prompt completion alone cannot verify a task |
| B4: dependency and human-input decisions | Typed blocked/needs_input records, mechanical dependency release; needs-input leaves task unmoved | Confirmed domain behavior; optional protocol extensions are not inferred from prose |
| B5: worktrees, command execution, evidence, stands and explicit deployment authority | Workspace/command/stand services; real unit commands and merge; profile authorization and evidence freshness tests | Confirmed local fixtures and local live pipeline. This does not claim a live external deployment campaign |
| B5: recovery before retry of uncertain external actions | Durable intents, cleanup proof, explicit resolution, no-replay tests | Confirmed bounded local recovery; exactly-once shell execution is not promised |

## Product requirements and deliverables

| Requirement | Inspected implementation/evidence | Result |
| --- | --- | --- |
| C1: shared operator state, assignment reasons and controls | `observation.py`, `diagnostics.py`, run/chat commands, dashboard/frontend tests | Confirmed state/identity/control surfaces, including G1 managed protocol facts |
| C2: minimal local and explicit fuller/UI/docs/deployed rosters with independent review | `presets.py`, public setup/preset CLI; [one live Codex task](evidence/acp-local-preset-2026-09-07.json) and [installed wheel](evidence/installed-local-preset-2026-09-07.json) | Confirmed representative fresh repository, one harness, three independent role runs, no stand, real checks and merge. The fixture supplies its plan |
| C3: explicit initialization/preflight/services/configuration; preserve customizations and hooks | Bootstrap/service/environment tests; installed idempotent setup and interrupted-operation fixtures | Confirmed current cutover scope; no fleet migration or rollback deliverable required |
| C4: concurrency/time/retry/no-progress/context limits; never switch provider to fit budget | Binding/project/account limits, input reservations and deadline/cancellation tests | Confirmed implemented limits and unknown provider values |
| C4: optional reliable usage budgets, reported/estimated/billed distinction | Bounded SDK-decoded observations, explicit currency/cost limits, durable prompt boundaries and cost continuity; real cancellation and loaded-conversation fixtures | Confirmed G2 for reactive reported session cost. Ambiguous token scope remains unknown and cannot drive an automatic token budget; no billing estimate or hard spending guarantee |
| C4: complete timing and measured performance improvement | [Idle before/after benchmark](evidence/daemon-idle-2026-09-07.json), stage observations, real pipeline durations | Idle read reduction confirmed. Productive timing/comparison remains G3; no role stage was eliminated |
| C5: aggregate stable findings, scoped repair, private bundle and no model turn | Doctor/bundle suites; command resolution from foreign cwd; maintenance/deployment idempotency and injected crashes | Components confirmed. **Open G7:** combine aggregate diagnosis → emitted action → repair → repeat diagnosis on a concrete fault fixture |
| C6: run-bound authority, stale/wrong-role rejection, explicit trust boundary | Token/revision/role/workspace checks; domain/command/permission tests | Confirmed cooperative shared-filesystem boundary; no OS isolation claim |
| C7: maintainable service boundaries, package examples and release evidence | Runtime/domain services; source and wheel scenarios; current changelog updated by this audit | Present and exercised; existing CLI validators remain shared during incremental extraction |
| C7: generated mechanical reference from validated contracts | JSON `project schema`/`project execution` and generated assigned context exist | **Open G5:** no reproducible checked-in mechanical reference generator was found; add and verify the reference artifact |
| C7: real extension-host smoke | [VS Code 1.92.2 evidence](evidence/vscode-extension-host-2026-09-07.json), isolated launcher, process argv/cleanup assertions | Confirmed limited host smoke, distinct from mock UI tests |
| C8: local operation, evidence, recovery and optional integrations; documented host envelope | Filesystem runtime, base-wheel fixture, installed docs/README corrected to Linux `/proc` + pidfd | Confirmed current host/scope statement; native macOS/Windows daemon support is not claimed |
| Next phase: local web workspace | Agreed plan explicitly places it after modernization | Deferred as requested; no database migration, distributed scheduler or automatic model selection introduced |

## Acceptance work items, in execution order

1. **G1 — managed protocol diagnostics: implemented and verified.** The normal
   supervisor records bounded negotiation/tool/error/stop facts, with private
   bundle summaries. Real subprocess fixtures prove secret exclusion, trace
   saturation with terminal error preserved, durable recovery, atomic failure and
   ordered draining of updates before an RPC error.
2. **G2 — reliable usage and budgets: implemented and verified.** Context
   occupancy, unknown-scope token samples and reported cumulative cost remain
   distinct. Optional cost limits require an explicit currency. Real subprocess
   fixtures verify cancellation at the limit, missing/invalid/regressed/currency-
   changing reports and loaded-session continuity. Durable pending markers retain
   crash uncertainty; retry cannot reset cost. The pinned schema contradicts
   itself about token scope, so these samples cannot support a token budget.
   Cost limits are reactive and allow one bootstrap prompt for a new session;
   they do not promise a hard spending cap. Five items below remain open.
3. **G3 — task timing and comparison.** Add the missing observable queue/validation/
   accepted-progress intervals, keeping unknowable times unknown. Compare an
   equivalent workload before/after; existing mixed-model durations are not a
   controlled performance comparison.
   Initial timing instrumentation now distinguishes first-observed queue waits
   from waits after accepted transitions, records preparation attempts and result
   resolution, and timestamps conversational turns. Real three-role pipeline and
   crash/retry fixtures exercise these records. Controlled productive measurements
   and comparison remain open; first model activity is still not useful work.
   A [six-run productive comparison](evidence/productive-pipeline-2026-09-07.json)
   now covers independently installed 3b3db47/4961016 wheels with identical schema,
   dependencies and deterministic task fixture: 1145→355 median daemon state reads,
   17.097→16.567 s observed median wall time. Both are modernization checkpoints;
   the original pre-modernization pipeline baseline remains unproven. These data
   must not be relabeled as an inference or model-quality comparison.
4. **G4 — account admission for explicit quota signals.** Extend shared admission
   only from unambiguous configured/protocol signals, with bounded recovery and
   operator controls. Do not infer credentials, silently switch providers or parse
   vendor prose as authoritative domain state.
5. **G5 — generated contract reference.** Produce mechanical role/transition/runtime
   reference from the validated contract and verify it cannot silently drift.
6. **G7 — combined repair acceptance.** Exercise the exact action emitted by doctor
   against injected durable faults, then verify repeated repair/diagnosis and zero
   model calls. Component-level tests do not substitute for this last connection.
7. **G6 — remaining live coverage.** Revalidate configured authentication before
   classifying an external blocker, then complete session/worktree/model/cancel
   scenarios and a declared role-by-harness evidence table. Current Cline evidence
   specifically identifies a re-authentication requirement; other earlier auth
   failures do not prove credentials are currently absent.

After these items, rerun the complete suite and relevant package/host checks,
inspect every row again, and record the resulting milestone verdicts. Current
milestone verdict: M0/M1/M4/M6 have substantial completed evidence; M2/M3/M5/M7
remain unproven as complete while their listed gaps are open. The overall goal
remains active. No green component test is used to mark the entire plan achieved.
