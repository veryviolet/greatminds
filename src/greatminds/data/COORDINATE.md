# Multi-agent coordination protocol

Greatminds coordinates task workflows through a durable daemon and one ACP
client. This reference explains ownership and evidence rules. The effective
schema defines mechanical transitions; `coordination/execution.yaml` defines
agent manifests, role bindings, commands and execution policy.

`greatminds project schema` prints the effective installed schema.
`.greatminds/schema.yaml` is a diagnostic mirror; setup preserves an existing
mirror. An explicit `GREATMINDS_CANON_DIR` selects an alternate canon.

The daemon compiles assigned context from the task and pinned contracts.
Agents do not reread the whole schema or scan all queues before every turn.
Use `greatminds run contract --schema` for additional run-pinned rules. Project
background can live in operator-maintained `coordination/PROJECT.md`.

When prose and schema disagree, resolve the conflict against actual validators
and the effective contract; do not infer permission to bypass a gate.

## 1. Philosophy

- **Queue location describes workflow state.** A validated transition transfers
  ownership; inline flags and final agent prose do not.
- **Evidence is append-only.** Iterations add new blocks rather than rewriting
  earlier decisions. Runtime services generate author and timestamp fields.
- **The daemon performs mechanical work.** It claims revisions, manages ACP
  sessions, evaluates admission, runs configured commands and reconciles results.
  Agents provide reasoning, implementation and substantive review.
- **Typed results drive transitions.** An agent submits a decision through
  `greatminds run submit`. The domain service validates identity, revision,
  allowed transition and evidence before changing queues.
- **Observability uses durable state.** `run status`, `run events`, `wake-check`
  and `watchdog` explain execution and domain state without relying on a TUI.

## 2. Roles

The effective schema defines responsibilities, forbidden actions and queue
ownership. Sharing a harness across roles does not combine their authority.

### 2.1 ACP lifecycle

Every managed role uses the same ACP session lifecycle. Binding scheduling is
`queue` for eligible background tasks or `on-demand` for operator input.
Harness, role, model, workspace and permission policy are separate choices.
Tmux and IDE frontends display daemon-owned conversations and observations.

The daemon handles startup backoff, configured no-progress budgets, concurrency,
permission waits, cancellation and restart reconciliation. It does not ask an
agent to poll, emit artificial heartbeats or schedule another loop. The OS
supervisor can restart the daemon itself. MAINTAINER remains available for
operator-requested diagnosis of unknown failures and repair planning.

An ACP turn ending is not task verification. Session reuse requires compatible
contracts and agent support; switching harnesses does not transfer their private
conversation history. Durable tasks provide portable work context.

## 3. Scenarios

See `.greatminds/schema.yaml` `scenarios:` for A/B/C definitions.

---

## 4. Shared state

Editable project configuration lives under `coordination/` in the project
root. Runtime/system state lives under `.greatminds/`: queues, inboxes, locks,
events, pinned run contracts, journals and stand state. Product source and normal
project files stay in the root.

### Queues

The full list of queues, their owners, and allowed transitions is in
`.greatminds/schema.yaml`. Read it once when you onboard a role; do not
re-derive from prose.

Categories:
- **Active queues** (`feature_inbox`, `feature_plan`, `feature_dev`, etc.):
  work is in flight; the owning role processes them.
- **Parking** (`feature_blocked`): waiting on explicit dependencies; not
  active work; the daemon evaluates mechanical resumption gates.
- **Terminal** (`verified`, `archive`, `*_verified`, `*_archive`): end
  states for normal flow. Product `verified` can still be rolled back by
  `ARCHITECT-REVIEWER` with an explicit `rollback` block when post-verify
  discovery shows the work is wrong, invalid, or already reverted.

Stand resource:

```
.greatminds/.stand/state.yaml
greatminds stand lease -> coordd auto-deploys (ready/down) -> holder release
```

Stand access is lease-backed and mediated by the `greatminds stand` CLI.

Review sessions (scenario B):

```
review_sessions/<id>.md  (lives until the session concludes, then archive/)
```

### Other runtime artifacts

- `.greatminds/journal.ndjson` — append-only journal of transitions. One
  NDJSON records provide transition and recovery evidence. Preserve this
  journal: queue snapshots cannot reconstruct its past decisions.
- `.greatminds/intent/<task>-<role>-<uuid>.json` — created BEFORE every
  `mv`, cleared AFTER successful `mv`. Detects crashed transitions. See
  §6.
- `.greatminds/inbox/{role}/` — mailbox for cross-role messages without
  moving tasks. Each message is a small markdown file with front-matter
  `to_role, from_role, task_ref, question, answered_at`. The recipient
  role inspects messages relevant to assigned work or an operator request.
- `.greatminds/.runtime/` — durable run, result, command and control records,
  frozen contracts and bounded command output. These are recovery evidence.

---

## 5. Hard ownership invariant

A task file in another role's active directory is **read-only**. Do not
append blocks to it, do not edit it, and do not move it.

- Directory location is the source of ownership.
- Inline flags (`ready_for_review: true`, `ready_for_architect: true`,
  `result: pass`) are evidence; they do not transfer ownership.
- Failed reviews and handbacks are allowed **only** from directories
  owned by the reviewing role. `ARCHITECT-REVIEWER` may hand back only from
  `feature_review/`. TESTER only from `feature_test/`. READER only from
  `feature_docs_review/`.
- If a task is blocked only by explicit named dependencies, the current
  owner must not leave it in an active queue indefinitely. Park it in
  `feature_blocked/` with a `blocked` block (see §7). Wake-up is
  performed by the daemon when dependency and role gates allow it.

---

## 6. Journal and intent

Shared domain services write intents, apply validated transitions and append
journal records. Agents never hand-author intent, journal or runtime files.
Runtime result and maintenance records link work to durable identities so
reconciliation can recognize a committed transition after interruption.

`.greatminds/journal.ndjson` and `.greatminds/intent/` preserve workflow evidence.
`.greatminds/.runtime/` holds run and operation state. Existing queues alone do
not reveal every unfinished operation. Do not delete these files to clear a
hold; inspect the operation and use its supported recovery action.

A command may have external effects even if its process is gone. Uncertain
command/deployment outcomes require explicit resolution before another attempt.
The runtime does not promise exactly-once shell execution.

## 7. Dependency blocking and resumption

If assigned work needs another artifact, submit a typed `blocked` decision with
explicit dependencies and `resume_to`. The service validates and records the
blocked state; the agent does not move the file itself.

The shared dependency service checks syntax, missing targets, terminal-state
requirements, impossible dependencies and cycles. `greatminds wake-check`
reports readiness and holds. The daemon rechecks task and live-role gates before
performing a journaled system resume. Mere file existence is insufficient.

Malformed or semantic blockers need a role decision; deterministic readiness
does not invent a replacement dependency or approve a task. The source blocked
record and system action retain provenance.

User-requested cancellation is a separate path: `ARCHITECT-PLANNER` can use
`greatminds task withdraw <task-id> --reason "<why>"`. The withdrawn task is
parked in `feature_blocked/`; archival still requires the schema's reviewer path.

## 8. Stand gate (`greatminds gate-check`)

For any task with `plan.stand_required: true`, TESTER must run
`greatminds gate-check <task-id>` before moving the task to `feature_review/`.

The script:
- finds the task,
- reads `plan.stand_required`,
- reads the latest `tests.stand_evidence` block,
- requires lease evidence with `lease_id`, `result`, and the tested commit,
- verifies the tested commit matches the task's implementation `base_commit`
  (prefix match either direction),
- verifies `worktree_fingerprint` when both the task and evidence carry one,
- requires fresh deployment evidence for the referenced lease and task,
- prints `pass | fail | missing | n/a` (one word).

TESTER records the result in the `tests` block as:

```yaml
gate_check_result: pass | fail | missing | n/a
gate_check_at: <ISO>
gate_check_commit: <sha>
```

`ARCHITECT-REVIEWER` refuses to approve a stand-required task without
`gate_check_result: pass`. The gate is not a courtesy. Missing lease evidence,
wrong-commit evidence, mismatched worktree fingerprints, or
`result != pass` fails the gate.

### 8.1 Stand profiles (`coordination/stand-profiles/`)

When a lease enters preparing, coordd reads `lease.profile`, resolves that
profile through `coordination/stand-profiles.yaml`, loads the referenced
Ansible YAML file from `coordination/stand-profiles/`, and executes it through
the configured authorized deployment policy. The deployment ledger records
intent and outcome; changed inputs invalidate readiness evidence. An uncertain
outcome needs explicit recovery before replay.

Ownership and usage:

- The project profile registry lives at `coordination/stand-profiles.yaml`.
- Stand playbooks live at `coordination/stand-profiles/<file>.yaml`.
- USER and DEVELOPER write or update profiles when adding new deploy
  scenarios.
- coordd uses a profile only when the active lease carries
  `profile=<name>`, loading the registry entry for `<name>` and then the
  referenced YAML playbook.
- Only YAML/ansible profiles are executable.

Convention:

- Registry key: profile name used by `greatminds stand lease --profile <name>`.
- Registry `file`: YAML file under `coordination/stand-profiles/`.
- Registry `used_for`: machine-readable capability tokens from
  `schema.stand_profile_registry.used_for_values`.
- Registry `default_for`: role-intent tokens from
  `schema.stand_profile_registry.default_for_values`; each token belongs to at
  most one profile.
- Production registry entries set `environment: production`,
  `requires_explicit_user_approval: true`, and an `allowed_roles` list. Lease
  commands for those profiles include `--profile-approval USER_APPROVED` after
  USER approval.
- YAML files use a subset of ansible-playbook syntax (machine-runnable
  by coordd's deploy path) — required fields `name`, `hosts`, `tasks`; optional `vars`,
  `handlers`, `gather_facts`.
- The lease's `deploy_prerequisites_only` metadata flag tells the deploy
  path to execute only tasks tagged `prerequisite`. This is used when TESTER
  must verify the deployment pipeline itself after only the host prerequisites
  are prepared.

Schema source-of-truth for profile conventions: `schema.stand_profile`.
Project files provide the selected playbook; `coordination/execution.yaml`
authorizes runtime deployment policy. Setup does not silently select a packaged
playbook. YAML execution requires the optional `greatminds[stands]` dependency.

### 8.2 Stand-only verification tasks (`plan.verify_only`)

A task whose whole point is to exercise the stand — "deploy the full
stand by profile X", "bring the stand up and confirm it works", "verify
this playbook" — produces NO product code, so it does NOT flow through
an implementer queue. There is deliberately NO task-less stand deploy:
every lease serves an auditable task (`stand lease --task` is required),
which is what anchors the stand's audit trail and the tested/verified
gate. So "just deploy the stand" and "test the stand" are the SAME FSM
path — a `verify_only` task — differing only in the verification DEPTH:

- **deploy-only** ("deploy the stand by profile X"): the bar is readiness —
  coordd deploys the profile and TESTER confirms the stand came up healthy
  (ssh / docker / health GET). No functional probes required.
- **behavioural test** ("check that X works"): the bar adds
  `functional_probes` + `tester_observations` — TESTER exercises the
  behaviour on the deployed stand.

PLANNER maps a bare "deploy the stand by profile X" to a `verify_only`
task with that profile and a readiness-only bar — it does NOT invent a
separate mechanism and does NOT hand-run a manual runbook. The flow:

1. **PLANNER** plans it with `stand_required: true`, a concrete
   `stand_reason`, the `profile` to exercise (e.g. `full-deploy` for
   scenarios A/B, `vite-dev` for live UI, `smoke-only` for warmup), and
   `verify_only: true`. PLANNER routes it `feature_plan → feature_test`
   directly (the `plan.verify_only` transition — no implementer step).
   PLANNER never runs ansible or checks the product itself.
2. **coordd** executes the profile on the lease (deterministic ansible) and does readiness
   ONLY — ssh-reachable, `docker`/health GET, gpu, endpoint presence.
   It does not do acceptance.
3. **TESTER** holds the `feature_test` lease, waits for `ready`, exercises
   the behaviour on the deployed stand, and records a `tests` block with
   real stand evidence (`reproduction_steps`, `observed_*`, `lease_id`,
   `result`, `commit`, plus `functional_probes` + `tester_observations`
   for backend/ui), then runs `gate-check`. It advances via the normal
   `feature_test → feature_review → verified` path.
4. Behavioural "use it like a user" verification can instead be an
   **EXPLORER** `review_sessions` lease; bugs found go to `feature_inbox`.

So: deploy/readiness = coordd, behavioural verification = TESTER
(or EXPLORER), bug intake = `feature_inbox`, planning/routing = PLANNER.

---

## 9. Tested / verified — definition

For ANY task with `plan.stand_required: true`, "tested" / "verified"
means a **reproducible behavioral verification on the live stand**,
NOT just pytest green. Pytest with mocks is necessary but NOT
sufficient: mocks encode the author's mental model of how live ACP /
process / systemd / agent tools behave, and cannot detect failures the
author did not anticipate.

TESTER's `tests` block on a `stand_required: true` task MUST include
`stand_evidence` with three fields:

1. **Reproduction steps** — exact commands to trigger the original
   failure mode on the live stand, including target identity, ACP input,
   expected runtime state and the observation command.
2. **Observed-without-fix** — output of step 1 BEFORE the fix
   (recorded from a separate lease probe, from the bug report, or from a
   pre-fix wheel build).
3. **Observed-with-fix** — output of step 1 AFTER the fix is
   deployed on the stand. Must visibly differ from
   observed-without-fix in a way that resolves the bug.

Without all three present, TESTER's `test_result` is NOT `pass`. It
is `partial` (or `fail`) with the missing evidence named, and the
task bounces back to DEVELOPER / PLANNER. The §8 `gate_check_pass`
rule still applies; this strict definition is **on top of**
`gate_check_pass`, not in place of it.

**TESTER vs coordd-deploy boundary.** coordd's deploy/readiness
records prove infrastructure only (container UP, version, `/health` 200,
schema sane). They are NOT test results.
TESTER's `tests` block on a `scope: backend|ui` task MUST also
record:
- `tests.functional_probes` — list of commands TESTER ran AGAINST
  the prepared stand (curl, psql, UI clicks per scope).
- `tests.stand_evidence.tester_observations` — TESTER's verbatim
  probe output, DISTINCT from deploy/readiness `observed_with_fix`. Verbatim
  copies of readiness text are rejected at the CLI level (rubber-stamp
  guard).
Scopes `docs` and `research` are exempt: READER review and audit
findings cover those.

`ARCHITECT-REVIEWER` refuses to approve a `stand_required: true` task
whose tests block lacks these three fields. Reviewer cites this
COORDINATE.md section in the handback.

`ARCHITECT-PLANNER` writes plans that specify the EXACT stand
reproduction command in `stand_reason` — not "stand evidence proves
it works", but the actual command and the expected before/after
output. If PLANNER cannot specify the reproduction, the task is
mis-scoped and PLANNER must rescope or split before routing.

In status reports to USER, the words "fixed", "tested", "verified",
"done" require this evidence. Otherwise the correct phrasing is
"pytest green, awaiting stand verification" or "implementation
complete, no behavioral verification yet".

### §9.1 Fix-for-self-blocker carve-out

If a task's `plan.stand_required` is true AND its TESTER tests block
contains all three `stand_evidence` fields (reproduction-steps,
observed-without-fix, observed-with-fix), but its associated
lease evidence carries `result=partial` or `result=fail` ONLY because of
a verification-infrastructure limitation that THIS task's fix
demonstrably removes — then `ARCHITECT-REVIEWER` may approve without
`gate_check_result=pass`. REVIEWER MUST cite this carve-out and the
tests block's chicken-and-egg explanation in the review block.

This carve-out is strictly limited: if the verification limitation
existed for reasons UNRELATED to the fix, the standard §9 evidence
requirement applies and the task bounces back.

### §9.2 Mid-task acceptance changes — broadcast to all roles

When `ARCHITECT-PLANNER` changes a task's acceptance criteria
mid-flight (after the `plan` block has been written and the task has
moved past `feature_plan/`), PLANNER MUST send the change as a
`kind: info` inbox message to **every role the task will touch in
subsequent ticks**: DEVELOPER (or the current queue owner),
TESTER and ARCHITECT-REVIEWER if `stand_required`.

The message MUST explicitly name what changed and what each recipient
must do differently — e.g. "TESTER: cite lease <NEW> not
<OLD>", "REVIEWER: gate-check now applies against <NEW> evidence",
"DEVELOPER: refile impl with new base_commit / acceptance text".

Single-role notification is a **protocol violation**: it leaves
TESTER/REVIEWER citing stale acceptance, causing review-block
bounces that re-emerge as new DEV asks and burn hours of stuck
pipeline. The broadcast is mandatory regardless of which role
prompted the change (asked, escalated, or PLANNER acted proactively).

---

## 10. Inbox mailbox

`.greatminds/inbox/{role}/` stores cross-role messages. Use `greatminds inbox`
to list, inspect, send and acknowledge messages. Do not edit or delete mailbox
files directly. Messages can clarify assigned work, but do not substitute for
validated task results or grant a role additional authority.

A mailbox is distinct from a daemon-owned conversation: `greatminds chat`
provides ordered operator messages, streamed ACP responses, permission choices,
reconnect and cancellation. Detaching a frontend does not replay its last prompt.

## 11. Watchdog (`greatminds watchdog`)

The watchdog reports orphan intents, stale tasks, worktree findings and ACP
execution observations. It does not restart native agents or infer success from
PID registry entries. Use `greatminds run status` for admission holds, pending
operations and account retry state; use `greatminds run events` for run events.

Watchdog inspection does not move tasks. Routine maintenance and recovery are
daemon controllers with explicit preconditions, not mandatory reviewer turns.

## 12. Progress and supervision

Process liveness, protocol activity and workflow progress are separate signals.
A token stream or heartbeat file does not advance a task. The daemon tracks
completed turns without progress and enforces `max_no_progress_turns` (default
one). Task metadata changes do not reset that budget.

Recognized pre-prompt startup failures can retry with bounded backoff. Shared
account delay covers bindings and conversations using that configured account.
Configured-session readiness is timestamped once; permission resume and crash
recovery do not create a new connectivity success. Unknown post-prompt failures
require explicit retry authority, subject to other unresolved-operation gates.

Use `run pause` to stop new dispatch, `run resume` to restore it, and
`run cancel RUN_ID` to request bounded cancellation. Pause lets active work
finish. Permission and authentication waits remain visible operator states.

## 12.5 Per-task worktrees

Each task gets its own working tree under
`<project_dir>/.worktrees/<task-id>/` on branch `task/<task-id>`.
Implementers (DEVELOPER / UI-DEVELOPER / TECHNICAL-WRITER)
`cd "$(greatminds worktree path <task-id>)"` before **editing** — the
CLI resolves the path from schema policy.

TESTER does **not** edit or execute in the worktree. For a local task with
`plan.stand_required: false`, TESTER requests configured validation commands
through the daemon and independently assesses their receipts and adequacy.
When the plan requires a stand, TESTER acquires its lease, waits for readiness,
and records actual SSH probes against the **deployed stand**. Local command
receipts cannot replace required stand evidence.

`uv run` / `uv run --active` is **forbidden for every role anywhere in
the repo**: `--active` syncs the cwd project into the *active* venv —
inside a `.worktrees/<id>/` that hijacks the stable fleet environment,
writing an editable `.pth → .worktrees/<id>/src`; when the worktree is
later pruned on merge the `.pth` dangles and every fleet agent dies at
import (`ModuleNotFoundError: greatminds`). If an implementer
sanity-runs tests locally, use ONLY an isolated `.venv`
(`unset VIRTUAL_ENV && uv venv && uv pip install --python .venv/bin/python -e .`)
— never `uv run`, never `--active`, never the fleet venv.

Lifecycle:

- `greatminds task mv ... feature_dev|feature_ui_dev|feature_docs`
  creates the per-task worktree.
- The implementer works from `.worktrees/<task-id>/`, so unrelated
  tasks cannot contaminate the main checkout or each other's working
  trees.
- `greatminds task mv ... verified` by REVIEWER merges the task branch
  back with `--no-ff`, preserving an explicit task boundary in git
  history.
- A verified product task is normally done, but REVIEWER can append a
  `rollback` block with a non-empty `reason` and move it from `verified`
  to `archive` after a code-level revert, or back to `feature_review` when
  the task needs another amendment/review cycle.
- `greatminds task mv ... archive` removes the task worktree.

Worktrees separate task code, while runtime transactions use locks and durable
claims. A worktree is not an OS sandbox or evidence of exclusive host access. The deploy playbook rsyncs the worktree
(not the main project tree) when the active stand lease names the task and
worktree. Policy lives in `.greatminds/schema.yaml > worktrees:`.

## 13. Git rules

Default:

- `ARCHITECT-REVIEWER` is the only product-work committer.
- MAINTAINER does not receive additional commit authority from its diagnostic
  role. Apply the effective `git_permissions` and explicit project policy.
- Implementers, TESTER, READER, USER, and EXPLORER do not
  commit.

Allowed to everyone for inspection: `git status`, `git diff`, `git show`,
`git log`.

Forbidden unless a role and project policy explicitly allow it:
`git add`, `git commit`, `git push`, `git stash`, `git reset`,
`git restore`, `git checkout` against tracked content, `git rebase`,
`git revert`, force-push, branch/tag deletion.

No `git add .`; the committer stages exact paths only.

---

## 14. Boundaries

- Agent execution has one ACP transport; unsupported harness combinations fail
  explicitly rather than selecting a native driver.
- Filesystem state is retained; no database migration is required for this model.
- Conflicts and uncertain side effects are not automatically declared resolved.
- Workspace and role contracts are cooperative boundaries; they do not replace
  OS isolation or provider-side permissions.

## 15. Setup and assigned context

`greatminds setup` creates runtime directories and an empty execution manifest,
preserving existing project data. Install and authenticate the chosen ACP
harness separately, configure bindings and run `greatminds daemon doctor`.
Start a foreground daemon with `greatminds coordd`, or explicitly install an
optional systemd user service. Setup does not install native profiles or plugins.

The context builder supplies the assigned revision, role obligations, applicable
transitions, command definitions and result format. Use the CLI argv provided in
that context; `run contract --schema` reads the run-pinned schema. Do not obtain
a different role or bypass revision checks by editing environment variables.

Operator inspection commands include:

```bash
greatminds run status
greatminds run events
greatminds watchdog
greatminds wake-check
greatminds project schema --check
```

## 16. Visual event markers

`schema.visual_events` retains display templates for operator summaries.
Structured runtime and domain events are authoritative; an agent's final-line
marker is optional presentation and cannot prove that a transition committed.
Frontends should render recorded actions instead of requiring an LLM to repeat
mechanical state as a specially formatted message.
