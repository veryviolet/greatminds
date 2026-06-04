# Multi-agent coordination protocol

This document is the contract for a file-based finite state machine that
coordinates Claude agents on a product. Mechanics (queue names, transitions,
required front-matter fields, watchdog thresholds) are defined in the
machine-readable `schema.yaml`. This document is the prose version: it
explains the philosophy and the invariants that hold the system together.

When `schema.yaml` and this prose disagree, `schema.yaml` is authoritative
for mechanics, and the prose is authoritative for the spirit of the
invariants. Fix whichever is wrong, do not paper over the conflict.

Project-specific values live in the installed `coordination/PROJECT.md`
(canon refers to them as `${...}` variables). Each agent's system prompt is
the single static `coordination/bootstrap.md`; it reads this canon
(`schema.yaml` + `COORDINATE.md`) plus `PROJECT.md` at the start of every
tick.

Every installed agent reads `COORDINATE.md`, `schema.yaml`, its own role
`.md`, and `coordination/PROJECT.md` before acting.

---

## 1. Philosophy

- **State lives in the filesystem.** A task's directory is its state. A
  handoff is `mv <queue>/X <next-queue>/X`. There is no daemon, no database,
  no broker.
- **Append-only inside files.** Every block (plan, implementation, tests,
  review, blocked, etc.) is appended. Earlier blocks are never edited.
  Iterations create new blocks with the same name.
- **Ownership is location.** Inline flags such as `ready_for_review: true`
  are evidence, not handoff. Only the physical move transfers ownership.
- **Coordd drives turns, not decisions.** `coordd` observes filesystem
  events and starts the next role turn when work lands. It does not own
  task state, choose transitions, or override role ownership.
- **Read-only observability.** Tools such as `greatminds watchdog`,
  `greatminds wake-check`, `greatminds gate-check`, `greatminds agent status`,
  and `greatminds journal` only read; they never move files. They produce
  reports for the appropriate role to act on.

---

## 2. Roles

See `schema.yaml` `roles:` for the full roster and per-role description.

### 2.1 Reactive fleet lifecycle

Every role declares `schema.yaml > roles.<ROLE>.lifecycle`. That field
describes how the role receives work; it is orthogonal to task scenario
mode (`A`, `B`, `C`) and to the product queue it owns.

Lifecycle values:

- `interactive` — human-paced chat. The role is user-facing and acts
  when the operator speaks to it. `ARCHITECT-PLANNER` is the normal
  interactive product role: it discusses scope with USER, then files or
  plans work after explicit approval.
- `self-loop` — autonomous watchdog loop. The role wakes itself on a
  timer and may also be woken early by coordd. `MAINTAINER` uses this
  model so fleet recovery does not depend on a user being present.
- `driven` — no persistent agent loop. The tmux pane is idle between
  turns. `coordd` observes an inbox, queue, or stand-state event and
  runs one role turn, then the role exits. Driven roles do one tick per
  invocation; they do not self-pace with `/loop`, long sleeps, or
  `ScheduleWakeup`.

The launch path is selected by lifecycle plus tool:

| Lifecycle | Tool | Turn mechanism | Between turns |
|---|---|---|---|
| `interactive` | `claude` | operator chat / USER prompt | live chat session |
| `self-loop` | `claude` | `/loop` plus `ScheduleWakeup` timer, with coordd early wake | loop waits for next tick |
| `self-loop` | `codex` or `cursor` | explicit loop plus Bash sleep fallback, with coordd interrupt | loop waits for next tick |
| `driven` | `claude` | coordd spawns one `claude -p` / resume turn with the rendered role bootstrap | idle bash pane |
| `driven` | `codex` | coordd drives one fresh `codex app-server` stdio turn and persists the app-server thread id | idle bash pane |
| `driven` | `bash` | direct command run by the owning automation | process exits |

Driven dispatch is intentionally gated by both `schema.yaml` lifecycle
and the installed `coord.yaml` window mode. This lets an installed fleet
migrate one role at a time; when both say `driven`, coordd uses the
driven turn path. Otherwise the role keeps its configured legacy launch
behavior until the operator updates the fleet config.

`MAINTAINER` is non-user-facing in this model. USER asks about fleet
health, restarts, schema changes, or upgrades go to `ARCHITECT-PLANNER`
first; PLANNER forwards an inbox ask to MAINTAINER when infrastructure
action is needed. The recovery chain is:

```text
systemd user unit -> coordd -> MAINTAINER self-loop -> worker restart / coordd restart / PLANNER escalation
```

`coordd` and systemd keep the observation and process layers alive;
MAINTAINER decides only safe fleet-recovery actions and escalates
product/FSM decisions back to PLANNER.

---

## 3. Scenarios

See `schema.yaml` `scenarios:` for A/B/C definitions.

---

## 4. Shared state

All runtime files live under `coordination/` in the project root. This
directory is normally gitignored.

### Queues

The full list of queues, their owners, and allowed transitions is in
`schema.yaml`. Read it once when you onboard a role; do not re-derive from
prose.

Categories:
- **Active queues** (`feature_inbox`, `feature_plan`, `feature_dev`, etc.):
  work is in flight; the owning role processes them.
- **Parking** (`feature_blocked`): waiting on explicit dependencies; not
  active work; wake-up by `ARCHITECT-REVIEWER`.
- **Terminal** (`verified`, `archive`, `*_verified`, `*_archive`): end
  states for normal flow. Product `verified` can still be rolled back by
  `ARCHITECT-REVIEWER` with an explicit `rollback` block when post-verify
  discovery shows the work is wrong, obsolete, or already reverted.

Stand resource:

```
coordination/.stand/state.yaml
greatminds stand lease -> STAND-KEEPER ready/down/up -> holder release
```

The legacy `stand_requests/ -> stand_wip/ -> stand_done/` queue path is not
the active workflow. Current stand access is lease-backed and mediated by the
`greatminds stand` CLI.

Review sessions (scenario B):

```
review_sessions/<id>.md  (lives until the session concludes, then archive/)
```

### Other runtime artifacts (new)

- `coordination/journal.ndjson` — append-only journal of transitions. One
  NDJSON line per move with fields `t, actor, task, from, to, reason,
  intent_id`. Gitignored. Optional for observability; not load-bearing.
- `coordination/intent/<task>-<role>-<uuid>.json` — created BEFORE every
  `mv`, removed AFTER successful `mv`. Detects crashed transitions. See
  §6.
- `coordination/inbox/<role>/` — mailbox for cross-role messages without
  moving tasks. Each message is a small markdown file with front-matter
  `to_role, from_role, task_ref, question, answered_at`. The recipient
  role reads its inbox at the start of every tick.

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
  `ARCHITECT-REVIEWER`'s job.

---

## 6. Journal and intent (new)

These artifacts make crashed transitions visible and provide a global
order of events. Both are write-only by the role doing the move; readers
(humans, watchdog) only inspect.

### Intent files

Before any `mv`, the moving role creates an intent file:

```text
coordination/intent/<task>-<role>-<uuid>.json
```

with contents:

```json
{
  "actor": "<role>",
  "task": "<task-id>",
  "from": "<queue>",
  "to":   "<queue>",
  "started_at": "<ISO>",
  "expected_finish_within_seconds": 30
}
```

After the `mv` succeeds, the role removes its own intent. If the role
crashes mid-move, the intent file is left behind. `greatminds watchdog` reports
intent files older than 5 minutes as orphaned, and the appropriate role
investigates.

### Journal (`journal.ndjson`)

After every successful `mv`, the role appends one NDJSON line to
`coordination/journal.ndjson`:

```json
{"t":"<ISO>","actor":"<role>","task":"<id>","from":"<q1>","to":"<q2>","reason":"<short>","intent_id":"<uuid>"}
```

The journal is derived state: you can reconstruct it by replaying the
filesystem if it is lost. Useful for `greatminds watchdog`, post-mortems, and a
global linear order of transitions.

---

## 7. Dependency blocking and wake-up

If a task cannot make progress because it explicitly depends on another
named artifact (a verified task, a tests-block stand evidence record), the current
owner must:

1. Append a `blocked` block (see
   `templates/coordination/feature_blocked/_TEMPLATE.md`):
   - `dependencies: ["<queue>/<task-id>.md", ...]` — strict format,
   - `resume_to: <queue>` — where the task should go when unblocked.
2. `mv <current-queue>/X feature_blocked/X`.

`ARCHITECT-REVIEWER` runs `greatminds wake-check` at the start of every tick. The
script:
- validates dependency syntax,
- checks if each dependency file exists,
- reports tasks where ALL dependencies exist as "ready to wake",
- flags malformed dependencies and orphan blocked-tasks.

For each ready-to-wake task, `ARCHITECT-REVIEWER` appends a wake-up note and
moves the task to `resume_to`.

---

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

When STAND-KEEPER grants a lease, it loads a profile file matching
`lease.profile` (the enum declared in `schema.stand.resource.profiles_allowed`)
from the canonical directory `coordination/stand-profiles/` and executes
it as part of the deploy.

Ownership and usage:

- Stand profiles live at `coordination/stand-profiles/<name>.{yaml,md}`.
- USER and DEVELOPER write or update profiles when adding new deploy
  scenarios.
- STAND-KEEPER uses a profile only when the active lease carries
  `profile=<name>`, loading it with `load_profile(coord, lease.profile)`.
- YAML and MD profile files are both supported; the author chooses the
  format that best fits the scenario.

Convention:

- File name: `<profile-name>.yaml` or `<profile-name>.md`,
  where `<profile-name>` is the `lease.profile` enum value
  (e.g. `full-deploy.yaml`, `vite-dev.md`, `smoke-only.yaml`).
- YAML files use a subset of ansible-playbook syntax (machine-runnable
  by SK) — required fields `name`, `hosts`, `tasks`; optional `vars`,
  `handlers`, `gather_facts`.
- MD files are free-form prose; SK injects the file contents into its
  next-tick prompt and writes the Bash needed to work the recipe.
- If both formats exist for the same profile name, YAML wins.
- The lease's `deploy_prerequisites_only` metadata flag tells SK to
  execute only tasks tagged `prerequisite` for YAML, or only the
  prerequisite section for MD. This is used when TESTER must verify the
  deployment pipeline itself after SK prepares only the host prerequisites.

Schema source-of-truth: `schema.stand_profile`. The runtime loader and
validator read profiles from there.

### 8.2 Stand-only verification tasks (`plan.verify_only`)

A task whose whole point is to exercise the stand — "deploy the full
stand by profile X", "bring the stand up and confirm it works", "verify
this playbook" — produces NO product code, so it does NOT flow through
an implementer queue. There is deliberately NO task-less stand deploy:
every lease serves an auditable task (`stand lease --task` is required),
which is what anchors the stand's audit trail and the tested/verified
gate. So "just deploy the stand" and "test the stand" are the SAME FSM
path — a `verify_only` task — differing only in the verification DEPTH:

- **deploy-only** ("разверни стенд по профилю X"): the bar is readiness —
  SK deploys the profile and TESTER confirms the stand came up healthy
  (ssh / docker / health GET). No functional probes required.
- **behavioural test** ("проверь, что работает X"): the bar adds
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
2. **STAND-KEEPER** executes the profile on the lease and does readiness
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

So: deploy/readiness = STAND-KEEPER, behavioural verification = TESTER
(or EXPLORER), bug intake = `feature_inbox`, planning/routing = PLANNER.

---

## 9. Tested / verified — definition

For ANY task with `plan.stand_required: true`, "tested" / "verified"
means a **reproducible behavioral verification on the live stand**,
NOT just pytest green. Pytest with mocks is necessary but NOT
sufficient: mocks encode the author's mental model of how live tmux /
pty / systemd / agent tools behave, and cannot detect failures the
author did not anticipate.

TESTER's `tests` block on a `stand_required: true` task MUST include
`stand_evidence` with three fields:

1. **Reproduction steps** — exact commands to trigger the original
   failure mode on the live stand (e.g. for an agent-stall bug:
   `ssh violet@<host> 'tmux send-keys -t toy:dev "" && sleep 60 &&
   check agent heartbeat'`).
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

**TESTER vs STAND-KEEPER role boundary.** STAND-KEEPER's readiness
records prove infrastructure only (container UP, version, `/health` 200,
schema sane). They are NOT test results.
TESTER's `tests` block on a `scope: backend|ui` task MUST also
record:
- `tests.functional_probes` — list of commands TESTER ran AGAINST
  the prepared stand (curl, psql, UI clicks per scope).
- `tests.stand_evidence.tester_observations` — TESTER's verbatim
  probe output, DISTINCT from SK's `observed_with_fix`. Verbatim
  copies of SK's text are rejected at the CLI level (rubber-stamp
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
TESTER, ARCHITECT-REVIEWER, and STAND-KEEPER if `stand_required`.

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

## 10. Inbox mailbox (new)

`coordination/inbox/<role>/` is a per-role mailbox for cross-role messages
that do NOT need a task move. Use it for:

- a question from DEVELOPER to ARCHITECT-PLANNER mid-implementation,
- coordination signals from STAND-KEEPER ("stand is back up"),
- EXPLORER asking ARCHITECT-PLANNER to refresh `stand_target`.

Each message is a markdown file with front-matter:

```yaml
---
to_role: ARCHITECT-PLANNER
from_role: DEVELOPER
task_ref: <task-id or null>
asked_at: <ISO>
answered_at: null | <ISO>
---
```

followed by the question body. The recipient reads its inbox at the start
of every tick, replies (either by editing the file to add an answer block
and notifying via `from_role`'s inbox, or by acting), and deletes handled
messages.

Inbox is intentionally informal. It is not a substitute for task moves; it
is a way to ask a question without escalating to handback.

---

## 11. Watchdog (`greatminds watchdog`)

`greatminds watchdog` reports:

- orphaned intent files (older than `intent_orphan_seconds`),
- registry entries whose `pid` is no longer alive,
- tasks idle in active queues beyond the per-queue threshold,
- tasks idle in review queues beyond the per-queue threshold,
- orphan worktrees (no matching active task).

Heartbeat is **not** a watchdog concern (see §12).

The watchdog never moves files. `ARCHITECT-REVIEWER` is expected to run it
each tick and follow up on findings.

---

## 12. Heartbeats

A heartbeat is **not** a periodic-liveness signal. It is an
**in-flight-turn hang detector**, owned by `coordd`:

```
coordination/heartbeat.<role>
```

While `coordd` holds a driven role's run-lock — i.e. a turn is in flight —
the turn's subprocess is expected to advance that role's heartbeat. If the
run-lock has been held *and* the heartbeat has not advanced for longer than
`schema.heartbeat.hang_threshold_seconds`, the turn is considered hung.
`coordd` does **not** kill it; it escalates once to `MAINTAINER` (an inbox
ask), and `MAINTAINER` decides what to do. A turn that completes normally
releases the lock, so a cold heartbeat with no held lock is simply an idle
role between turns — not a hang.

---

## 12.5 Per-task worktrees

Each task gets its own working tree under
`<project_dir>/.worktrees/<task-id>/` on branch `task/<task-id>`.
Implementers (DEVELOPER / UI-DEVELOPER / TECHNICAL-WRITER)
`cd "$(greatminds worktree path <task-id>)"` before **editing** — the
CLI resolves the path from schema policy.

TESTER does **not** edit or execute in the worktree. TESTER's only
execution surface is SSH probes against the **deployed stand** (after
STAND-KEEPER rsyncs the worktree to the stand); evidence comes from the
stand, not a local run.

`uv run` / `uv run --active` is **forbidden for every role anywhere in
the repo**: `--active` syncs the cwd project into the *active* venv —
inside a `.worktrees/<id>/` that hijacks the fleet venv `.venv-coord`,
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

There is no file-lock model: operators look in `.worktrees/` for in-flight
code instead of looking for lock files. STAND-KEEPER rsyncs the worktree
(not the main project tree) when the active stand lease names the task and
worktree. Policy lives in `schema.yaml > worktrees:`.
Before cutting the 1.2.x → 1.3 release, MAINTAINER runs
`greatminds worktree assert-drained` to confirm no in-flight tasks
straddle the lock-era → worktree-era boundary.

## 13. Git rules

Default:

- `ARCHITECT-REVIEWER` is the only product-work committer.
- `MAINTAINER` commits **canon and infrastructure** changes only
  (schema.yaml, role docs, CLI source, plugin skills, MCP config,
  templates) — explicitly distinct from product-pipeline work, which
  flows through `ARCHITECT-REVIEWER`. MAINTAINER never commits
  product-task artifacts (plan / implementation / tests / reader /
  review blocks); the FSM owns those via per-role queues.
- Implementers, TESTER, READER, USER, EXPLORER, and STAND-KEEPER do not
  commit.

Allowed to everyone for inspection: `git status`, `git diff`, `git show`,
`git log`.

Forbidden unless a role and project policy explicitly allow it:
`git add`, `git commit`, `git push`, `git stash`, `git reset`,
`git restore`, `git checkout` against tracked content, `git rebase`,
`git revert`, force-push, branch/tag deletion.

No `git add .`; the committer stages exact paths only.

---

## 14. Non-goals

- No second bug-fix loop (bugs are `plan_kind: bugfix` product tasks).
- No `active_loop`.
- No REVIEW/FIX/UI_FIX roles (deprecated and removed long ago).
- No central daemon, broker, or database.
- No automatic conflict resolution.
- No hidden state outside the task files, `stand.status`, and the journal.
- No POSIX-permission enforcement of the stand gate or queue ownership.

---

## 15. Bootstrap

Every agent's system prompt is the single static `coordination/bootstrap.md`
(seeded from canon by `greatminds setup`). It is role-independent: the agent
learns its role from `$GREATMINDS_ROLE` and reads its own contract from
`schema.yaml > roles.<GREATMINDS_ROLE>`, plus `COORDINATE.md` and
`coordination/PROJECT.md`. coordd injects it as the system prompt for driven
turns; `greatminds start-agent` uses it for paned roles. The role list is
`schema.yaml > roles`.

To check the canon for unknown tokens or missing catalog entries:

```bash
<PROJECT_ROOT>/greatminds lint-tokens
```

To audit the live coordination filesystem:

```bash
<PROJECT_ROOT>/greatminds watchdog
<PROJECT_ROOT>/greatminds wake-check
<PROJECT_ROOT>/greatminds gate-check <task-id>
<PROJECT_ROOT>/greatminds agent status [ROLE]
<PROJECT_ROOT>/greatminds journal tail
```

## 16. Visual event markers

After a coordination action — `greatminds task mv`,
`greatminds task append-block`, or `greatminds inbox send` — emit the
matching one-line visual marker as the **LAST line** of your reply, so
an operator scrolling a pane sees state changes at a glance. The marker templates
(and their emoji) live in `schema.visual_events` — use them as the
source of truth; do not inline or invent emoji here, or prose and
schema drift. The marker comes AFTER any follow-up text, on its own
final line.

## Canon skill plugins

Procedural detail (recipes, gotchas, examples) lives in **Claude Code
Skills** under `src/greatminds/data/plugins/`. Each role-X plugin is
loaded only for the matching `GREATMINDS_ROLE`; the shared
`coordination-protocol` plugin is loaded by every role.

| Plugin | Audience | Highlights |
|---|---|---|
| `coordination-protocol` | all roles | fsm-mechanics, plan-block-protocol, impl-block-craft, iteration-and-blocking, stand-protocol, inbox-and-escalation |
| `role-architect-planner` | ARCHITECT-PLANNER | adr-template, trade-off-framework, task-decomposition |
| `role-tester` | TESTER | probe-craft, api-and-db-probes, ui-visual-verification |
| `role-stand-keeper` | STAND-KEEPER | stand-bring-up, fresh-db-volume-wipes, fault-isolation-on-stand |
| `role-reader` | READER | fresh-user-perspective, reality-vs-docs-audit, reader-review-block-craft, audit-path-vs-post-write-path |
| `role-architect-reviewer` | ARCHITECT-REVIEWER | evidence-chain-verification, architectural-review, review-block-craft, commit-and-push-protocol, wake-and-unblock |
| `role-explorer` | EXPLORER | exploratory-probing, bug-as-mini-task, re-verify-loop |
| `role-maintainer` | MAINTAINER | agent-lifecycle-and-diagnostics, canon-sync-and-cutover, maintainer-vs-planner-routing, infra-surface-separation |

This document stays as the **philosophy / invariants / queue map** for
the protocol. Skill files carry the procedural detail and are
auto-invoked by Claude based on context.

### Per-tool loading mechanism

Skill loading is tool-specific. The same role's procedural knowledge
reaches the agent via different mechanisms depending on which tool the
role is launched on:

- **Claude Code** — `greatminds start-agent <ROLE> claude` passes
  `--plugin-dir <canon>/plugins/coordination-protocol` and
  `--plugin-dir <canon>/plugins/role-<role>` (plus the project
  override under `coordination/plugins.local/project-overrides/`).
  Skill content is auto-invoked by Claude based on description-keyword
  match in the agent's working context.
- **Codex** — codex CLI has no `--plugin-dir` equivalent. The
  equivalent path is the codex **profile** mechanism: `greatminds setup`
  creates one per-role Codex home at
  `coordination/.codex-home/<role>/config.toml`, copied from
  `<canon>/codex/profiles/*.config.toml`. Each generated config contains
  `developer_instructions = """..."""` plus `[profiles.<role>]` with the
  model, approval, and sandbox settings. `greatminds start-agent <ROLE>
  codex` launches with `CODEX_HOME=<project>/coordination/.codex-home/<role>`
  and `--profile <role-lower>`, so codex reads that role-local
  `$CODEX_HOME/config.toml`; the old user-home per-role profile files
  are not part of the launch path. The profile body summarizes the role
  contract — it's not a full SKILL-format auto-invoke, but it brings the
  role-specific procedural posture into every codex session.
- **Cursor** — currently no per-role plugin/profile mechanism is
  wired; cursor roles get the bootstrap prompt only (which already
  contains the full role brief rendered from `<role>.md`).

See also: `plugins/README.md` for plugin layout; `mcp/canon.json`
for canon-wide MCP server set; `greatminds start-agent` for how plugins
/ profiles are wired per-launch.
