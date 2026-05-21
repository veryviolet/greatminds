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

The full roster, claim sources, and heartbeats are in `schema.yaml`. Brief:

| Role                 | Category | What it does, in one line                                          |
|----------------------|----------|--------------------------------------------------------------------|
| ARCHITECT-PLANNER    | product  | Triage, planning, scenario-B sessions. Chat-capable.               |
| ARCHITECT-REVIEWER   | product  | Final review, wake blocked, commits.                               |
| DEVELOPER            | product  | Backend implementation.                                            |
| UI-DEVELOPER         | product  | UI implementation (pipeline mode + FAST chat mode for scenario C). |
| TECHNICAL-WRITER     | product  | Documentation implementation.                                      |
| TESTER               | product  | Validates code on the stand; runs `greatminds gate-check`.                |
| READER               | product  | Validates docs as a fresh reader.                                  |
| EXPLORER             | product  | Uses live product on stand (scenario B). Files bugs.               |
| STAND-KEEPER         | service  | Owns stand and `stand.status`. Two profiles: full-deploy, vite-dev.|
| USER                 | entry    | Files feedback or initiates chat with ARCHITECT-PLANNER.           |
| BOT-USER, BOT-DEVELOPER | bot   | Bot stream, intentionally untouched by this refactor.              |

The 2026-05 refactor split ARCHITECT into PLANNER + REVIEWER and added
EXPLORER. See per-role `.md` files for the full description.

---

## 3. Scenarios

Three top-level modes of operation. Each task carries `plan.mode: A | B | C`.

### Scenario A — feature / refactor (default)

Standard product pipeline. User chats with `ARCHITECT-PLANNER` to refine an
idea; planner writes a plan; implementers (DEVELOPER / UI-DEVELOPER /
TECHNICAL-WRITER) work in **parallel** on their respective scopes; TESTER
and READER validate; `ARCHITECT-REVIEWER` approves and commits.

### Scenario B — intensive review

Targeted exploration of a deployed function. `ARCHITECT-PLANNER` opens a
`review_sessions/<id>.md` and asks `STAND-KEEPER` to ensure freshness.
`EXPLORER` walks scenarios on the live system and files bugs as
`plan_kind: bugfix`. PLANNER fast-triages bugs into `feature_dev/` or
`feature_ui_dev/` without long planning. After fixes ship, EXPLORER re-runs
scenarios.

### Scenario C — UI rapid iteration

`STAND-KEEPER` brings up backend + Vite (profile `vite-dev`). The user
chats with `UI-DEVELOPER` started in FAST mode (separate heartbeat to avoid
contention with any pipeline-mode UI-DEVELOPER agent). No `feature_plan/`,
no `feature_test/` — Vite HMR feeds the change to the user's browser. An
optional summary task may be filed in `feature_review/` at session end for
audit.

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

## 9. Inbox mailbox (new)

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

## 10. Watchdog (`greatminds watchdog`)

`greatminds watchdog` reports:

- stale heartbeats (older than `schema.watchdog.heartbeat_stale_seconds`),
- orphaned intent files (older than `intent_orphan_seconds`),
- tasks idle in active queues beyond the per-queue threshold,
- tasks idle in review queues beyond the per-queue threshold.

The watchdog never moves files. `ARCHITECT-REVIEWER` is expected to run it
each tick and follow up on findings.

---

## 11. Heartbeats

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

## 12. Git rules

Default:

- `ARCHITECT-REVIEWER` is the only product-work committer.
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

## 13. Non-goals

- No second bug-fix loop (bugs are `plan_kind: bugfix` product tasks).
- No `active_loop`.
- No REVIEW/FIX/UI_FIX roles (deprecated and removed long ago).
- No central daemon, broker, or database.
- No automatic conflict resolution.
- No hidden state outside the task files, `stand.status`, and the journal.
- No POSIX-permission enforcement of the stand gate or queue ownership.

---

## 14. Bootstrap

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
Skills** under `/opt/coordination/plugins/`. Each role-X plugin is
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

See also: `plugins/README.md` for plugin layout; `mcp/canon.json`
for canon-wide MCP server set; `greatminds start-agent` for how plugins
are wired per-launch.
