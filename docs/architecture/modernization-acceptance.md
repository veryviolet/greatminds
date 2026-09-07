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
| A4: Qwen/Kimi/Grok/Cline/Gemini/OpenHands/Cursor distribution, auth, session, worktree, model and cancellation | Per-stage [campaign evidence and role matrix](acp-compatibility.md); fresh explicit auth attempts and pending-permission cancellation on three harnesses | **Open G6:** only initialized/session-created status for several installations; authentication and complete scenarios remain necessary |
| A5: daemon-owned interactive prompts, streamed output, permission, reconnect and interrupt | Conversation FIFO, task-bound claims, shared broker; `test_acp_conversations.py`, `test_chat_tasks.py`, `test_chat_terminal.py` | Confirmed fixture coverage plus public CLI restart/recall/cursor scenarios for planner and live developer on Codex/Claude/Grok; live coverage remains narrower than all roles/harnesses |
| A5: common tmux/VS Code surfaces and removal of native dispatch | `runtime/frontends.py`, thin `cli/coordd.py`, extension CLI terminals; launch tests and [real extension host](evidence/vscode-extension-host-2026-09-07.json) | Confirmed managed execution routes through ACP; host smoke does not exercise Quick Picks or live chat |

## Deterministic orchestration

| Requirement | Inspected implementation/evidence | Result |
| --- | --- | --- |
| B1: claims, supervision, cancellation, startup/periodic reconciliation, process trees | `runtime/daemon.py`, `supervisor.py`, `processes.py`; exclusive-supervisor, launch-gate, surviving-child and stale-identity fixtures | Confirmed on the documented Linux host interface |
| B1: bounded retries and no-progress policy | `retry_policy.py`, durable run state; startup/account/no-progress suites | Confirmed for startup failures and unchanged task turns |
| B1: provider/account limits do not cause independent retry storms | Pinned manifest error-code rules, durable project/account holds, atomic claim admission and subsequent conversation prompt checks | Confirmed G4 for explicitly configured ACP application codes: bounded cooldown or operator-resolved quota hold. Generic error prose cannot trigger quota state; no built-in live harness code mapping is claimed |
| B2: known maintenance, dependency release, impossible dependencies/cycles | Shared dependency verdicts and journaled SYSTEM maintenance; missing/cyclic/changed/live-role fixtures and installed smoke | Confirmed; ordinary dependency release invokes zero agents |
| B2: dead-holder leases, cleanup, log retention and deduplicated findings | Lease/deployment recovery, `filesystem_health.py`, `event_retention.py`, maintenance finding fingerprints | Confirmed specified local behavior; evidence/task/conversation records are preserved and external systemd logs use host policy |
| B2: maintainer limited to semantic diagnostic work | Shared schema marks maintainer on demand and forbids polling/recovery loops; public CLI has no journal-to-agent wake dispatcher | Confirmed instructions and scheduling contract; journal records are preserved while SYSTEM releases dependencies without reviewer/inbox wake effects |
| B3: concrete assignment, role/task/gates/results/artifacts and pinned context | `context.py`, `RunStore.contracts`, CLI contract inspection and scoped commands | Confirmed; roles receive their assignment rather than scanning queues |
| B3: compatible session resume and changed-contract handling | Supervisor session selection and conversation acquire/revision checks; load/mismatch/task-change tests | Confirmed; changed contracts/revisions do not silently consume pending input |
| B3: prompt size, useful-work time and turns against baseline | Input reservations, run stages, queue/accepted-transition waits, preparation/receipt timing; original and installed ACP productive comparisons | Confirmed G3 measurement scope: bytes are not billed tokens; original native median 4.685 s/5 calls, optimized ACP 8.354 s/3 calls. Native latency regression remains a documented limitation |
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
| C4: complete timing and measured performance improvement | [Idle benchmark](evidence/daemon-idle-2026-09-07.json), original baseline, [continuous ACP optimization](evidence/continuous-acp-optimization-2026-09-07.json) | Confirmed bounded measurements: ACP median 10.950→8.354 s after import/parse optimization; original native remains faster. No universal speed guarantee; all three useful role stages remain |
| C5: aggregate stable findings, scoped repair, private bundle and no model turn | Doctor/bundle suites; public doctor → exact emitted command resolution → repeat diagnosis; account hold repair cycle; maintenance/deployment recovery fixtures | Confirmed G7 concrete fault cycles with unchanged task data, idempotent resolution, no command/agent execution and no implicit retry |
| C6: run-bound authority, stale/wrong-role rejection, explicit trust boundary | Token/revision/role/workspace checks; domain/command/permission tests | Confirmed cooperative shared-filesystem boundary; no OS isolation claim |
| C7: maintainable service boundaries, package examples and release evidence | Runtime/domain services; source and wheel scenarios; current changelog updated by this audit | Present and exercised; existing CLI validators remain shared during incremental extraction |
| C7: generated mechanical reference from validated contracts | [Generated reference](contract-reference.md), exact-text `--check`, role/queue/validator checks, all presets through execution validation | Confirmed G5: deterministic source fingerprints and drift tests; installed-wheel generation matches on Python 3.11/3.12/3.13; CI/docs/release workflows enforce the check |
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
   they do not promise a hard spending cap. G6 below remains open.
3. **G3 — task timing and comparison: implemented and measured.** Queue wait,
   preparation, accepted progress and result resolution are recorded separately
   from first model activity. Real pipeline and crash/retry fixtures exercise them.
   A [productive comparison](evidence/productive-pipeline-2026-09-07.json) on
   independently installed modernization checkpoints measured 1145→355 median
   runtime reads and 17.097→16.567 s wall time with identical task fixtures.
   The [original continuous-daemon comparison](evidence/original-pipeline-2026-09-07.json)
   measured native median 4.685 s versus ACP 10.597 s, with five versus three
   agent calls. A subsequent [six-run optimization campaign](evidence/continuous-acp-optimization-2026-09-07.json)
   measured ACP median 10.950→8.354 s (23.7% lower) after deferring command-client
   SDK imports and caching verified schema parsing. Gates and recovery assertions
   remain intact. This closes the finite measurement/optimization item; native
   latency remains lower. These fixture timings do not measure inference, billing
   or model quality, and profiling samples are excluded from comparisons.
4. **G4 — account admission for explicit quota signals: implemented and verified.**
   `account_limit_errors` maps at most 16 nonreserved ACP application codes using
   the reporting run's pinned manifest. Rate-limit cooldowns are 1..86400 seconds;
   quota exhaustion requires explicit operator resolution. Shared holds block
   all new claims and later conversation prompts for the same project/account.
   Retry, configuration edits and restart do not erase them. Concurrent signals
   cannot shorten holds; new hold identities invalidate stale resume actions.
   Real ACP fixtures cover initialization/prompt signals, generic-error rejection,
   secret exclusion and restart without another launch. No provider prose parsing,
   credential inference, model switching or implicit live harness mapping exists.
   An [independently installed base-wheel fixture](evidence/account-admission-2026-09-07.json)
   confirms restart admission, explicit CLI resume and stale-action rejection.
5. **G5 — generated contract reference: implemented and verified.**
   `tools/generate_contract_reference.py` emits complete role declarations, queue
   and transition tables, declared SYSTEM operations, run states, normalized record
   fields/defaults and validated examples of all presets. Unknown references or
   unregistered requirement validators fail generation. `--check` detects missing
   or stale output without rewriting it; source fingerprints include the validators
   and controllers behind declarations. The reference deliberately distinguishes
   record fields from the full input-validation grammar and dynamic domain gates.
   [Installed-wheel evidence](evidence/contract-reference-2026-09-07.json) confirms
   reproducibility on Python 3.11/3.12/3.13 and real offline pipelines on 3.11/3.12.
   CI, documentation and release workflows check drift. Two actual CI smoke steps
   validate the current packaged resources and fresh project setup contract.
6. **G7 — combined repair acceptance: verified.**
   `test_aggregate_repair_cycle.py` invokes public `run doctor --json`, executes
   the exact emitted command-resolution argv/environment/cwd, repeats resolution
   and diagnosis, and checks task bytes, runtime idempotence, absent executor/command
   effects and absence of implicit retry. The injected missing-completion fault
   is acknowledged, not converted to passing evidence. The account-limit suite
   additionally exercises the aggregate doctor → scoped resume → repeat diagnosis
   cycle and rejection of stale identities and agent credentials.
7. **G6 — remaining live coverage.** Revalidate configured authentication before
   classifying an external blocker, then complete session/worktree/model/cancel
   scenarios. The declared role-by-harness evidence table is now present. Fresh
   authentication checks distinguish missing Qwen configuration, Kimi/Cline login,
   OpenHands agent configuration, discontinued Gemini individual access and a
   Cursor ACP browser-login wait despite cached native status (confirmed in the
   exact probe process log; the reason cache validation failed remains unknown).
   Cancellation while
   permission is pending passed on Claude/Codex/Grok. Public CLI planner/live-developer
   conversations also passed session recall after daemon restart, duplicate delivery
   and cursor reconnect on all three. These checks do not establish the remaining
   domain-role or harness scenarios.

After these items, rerun the complete suite and relevant package/host checks,
inspect every row again, and record the resulting milestone verdicts. Current
milestone verdict: M0/M1/M4/M6 have substantial completed evidence; M2/M3/M5/M7
remain unproven as complete while their listed gaps are open. The overall goal
remains active. No green component test is used to mark the entire plan achieved.
