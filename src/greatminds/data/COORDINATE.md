# Multi-agent coordination protocol

This document is the contract for a file-based finite state machine that
coordinates Claude agents on a product. Mechanics (queue names, transitions,
required front-matter fields, watchdog thresholds) are defined in the
machine-readable `schema.yaml`. This document is the prose version: it
explains the philosophy and the invariants that hold the system together.

When `schema.yaml` and this prose disagree, `schema.yaml` is authoritative
for mechanics, and the prose is authoritative for the spirit of the
invariants. Fix whichever is wrong, do not paper over the conflict.

Project-specific values appear as `<TOKEN>` placeholders; their values live
in the installed `coordination/PROJECT.md`. Use `greatminds render-role <ROLE>` to
get a token-substituted bootstrap prompt; the script reads this canon plus
the local `PROJECT.md`.

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
- **No central authority.** Each role reads schema/inbox/queue and acts.
  There is no scheduler that decides what runs when.
- **Read-only observability.** New tools (`greatminds watchdog`, `greatminds wake-check`,
  `greatminds gate-check`) only read; they never move files. They produce reports
  for the appropriate role to act on.

---

## 2. Roles

See `schema.yaml` `roles:` for the full roster and per-role description.

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
  states.

Stand pipeline:

```
stand_requests/ -> stand_wip/ -> stand_done/
```

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
named artifact (a verified task, a stand_done evidence file), the current
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
- searches `stand_done/*.md` for entries whose
  `stand_result.evidence_for` list contains this task id,
- verifies the stand-side `commit` matches the task's implementation
  `base_commit` (prefix match either direction),
- prints `pass | fail | missing | n/a` (one word).

TESTER records the result in the `tests` block as:

```yaml
gate_check_result: pass | fail | missing | n/a
gate_check_at: <ISO>
gate_check_commit: <sha>
```

`ARCHITECT-REVIEWER` refuses to approve a stand-required task without
`gate_check_result: pass`. The gate is not a courtesy. Stand_done evidence
that is for the wrong task, against the wrong commit, or has
`result != pass` fails the gate.

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
   (recorded from a separate stand_result, or from the bug report,
   or from a pre-fix wheel build).
3. **Observed-with-fix** — output of step 1 AFTER the fix is
   deployed on the stand. Must visibly differ from
   observed-without-fix in a way that resolves the bug.

Without all three present, TESTER's `test_result` is NOT `pass`. It
is `partial` (or `fail`) with the missing evidence named, and the
task bounces back to DEVELOPER / PLANNER. The §8 `gate_check_pass`
rule still applies; this strict definition is **on top of**
`gate_check_pass`, not in place of it.

**0228: TESTER vs STAND-KEEPER role boundary.** `stand_result.
observed_with_fix` records SK's infra-readiness only (container
UP, version, `/health` 200, schema sane). It is NOT a test result.
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
`stand_done` carries `result=partial` or `result=fail` ONLY because of
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
must do differently — e.g. "TESTER: cite stand_done/<NEW> not
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

- stale heartbeats (older than `schema.watchdog.heartbeat_stale_seconds`),
- orphaned intent files (older than `intent_orphan_seconds`),
- tasks idle in active queues beyond the per-queue threshold,
- tasks idle in review queues beyond the per-queue threshold.

The watchdog never moves files. `ARCHITECT-REVIEWER` is expected to run it
each tick and follow up on findings.

---

## 12. Heartbeats

Every active agent touches its heartbeat at least once every five minutes:

```
coordination/heartbeat.<role>
```

See `schema.yaml roles.*.heartbeat` for the exact filename per role. The
FAST-mode UI-DEVELOPER uses `heartbeat.ui-developer.fast` so it does not
conflict with a parallel pipeline-mode UI-DEVELOPER.

A stale heartbeat means "probably stalled". Recovery is simply the same
role resuming and touching its heartbeat again.

---

## 12.5 Per-task worktrees (0185)

Each task gets its own working tree under
`<project_dir>/.worktrees/<task-id>/` on branch `task/<task-id>`.
Implementers + TESTER `cd "$(greatminds worktree path <task-id>)"`
before editing/testing — the CLI resolves the path from schema
policy.

Lifecycle:

- `greatminds task mv ... feature_dev|feature_ui_dev|feature_docs`
  creates the per-task worktree.
- The implementer works from `.worktrees/<task-id>/`, so unrelated
  tasks cannot contaminate the main checkout or each other's working
  trees.
- `greatminds task mv ... verified` by REVIEWER merges the task branch
  back with `--no-ff`, preserving an explicit task boundary in git
  history.
- `greatminds task mv ... archive` removes the task worktree.

This replaces the 0115/0166 file-lock model. Lock-release handling from
0166 is obsolete; operators should look in `.worktrees/` for in-flight
code instead of looking for lock files. STAND-KEEPER rsyncs the worktree
(not the main project tree) when a stand_request carries
`evidence_for: [<task-id>]`. Policy lives in `schema.yaml > worktrees:`.
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
- BOT-DEVELOPER follows `<BOT_COMMIT_POLICY>`.

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

Render and run a role's bootstrap prompt with `greatminds render-role`:

```bash
<PROJECT_ROOT>/greatminds render-role <ROLE> [--project-dir <dir>]
```

The output substitutes tokens from `coordination/PROJECT.md`. Either pipe
into your agent runner or copy the text. The complete role list lives in
`command_START.yaml`.

To check the canon for unknown tokens or missing catalog entries:

```bash
<PROJECT_ROOT>/greatminds lint-tokens
```

To audit the live coordination filesystem:

```bash
<PROJECT_ROOT>/greatminds watchdog
<PROJECT_ROOT>/greatminds wake-check
<PROJECT_ROOT>/greatminds gate-check <task-id>
```

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
