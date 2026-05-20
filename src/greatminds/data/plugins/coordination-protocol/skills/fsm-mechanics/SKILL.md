---
name: fsm-mechanics
description: Use whenever moving, modifying, or inspecting a task in the coordination FSM. Covers the file-based queue model (location = ownership), bin/* as the only mutation path, heartbeat as a side-effect, intent/journal discipline, and the PreToolUse hook that physically blocks raw mv/Edit on coordination files. Trigger on "task move", "bin/task", "queue ownership", "schema.yaml", "intent file", "journal.ndjson", "heartbeat", "claim".
---

# FSM mechanics

Coordination is a **file-based finite state machine**. Tasks are YAML
files that move between queue directories. Ownership is determined
entirely by **which directory** a file currently sits in — no
claim_filter, no external registry, no lock outside per-task fcntl.

## Owned queues per role

See `schema.yaml` for the authoritative mapping. Summary:

| Role | Owns intake queue(s) |
|---|---|
| ARCHITECT-PLANNER | `user_feedback/`, `feature_inbox/`, `feature_plan/` |
| ARCHITECT-REVIEWER | `feature_review/`, `feature_blocked/` |
| DEVELOPER | `feature_dev/` |
| UI-DEVELOPER | `feature_ui_dev/` |
| TECHNICAL-WRITER | `feature_docs/` |
| TESTER | `feature_test/` |
| READER | `feature_docs_review/` |
| EXPLORER | `review_sessions/` |
| STAND-KEEPER | `stand_requests/` |
| MAINTAINER | `inbox/maintainer/` |

Terminal queues: `verified/`, `archive/` — only ARCHITECT-REVIEWER
transitions tasks into these.

## Mutation paths — bin/* ONLY

Every mutation of a task file MUST go through one of:

- `bin/task` — generic verb interface (`new`, `mv`, `append-block`, `show`, `list`)
- `bin/inbox` — inter-role messaging (`send`, `list`, `ack`)
- `bin/stand` — thin wrapper for the `stand_request` stream
- `bin/plan` — one-shot PLANNER pipeline (triage → feature_plan → plan → route)

Raw `mv`, raw `Edit`, raw `Write` on any file under `coordination/`
(queues, inbox/, task YAMLs) is **physically blocked** by the
`PreToolUse` hook (`bin/stop_decide` rejects with a clear error).
Even if the hook is bypassed, the strict-schema validators inside the
bin/* scripts reject malformed transitions.

## Side effects you get for free

After a successful `bin/task <verb>` (or `bin/inbox` / `bin/stand`), the
following are guaranteed to have happened atomically (per-task
fcntl-locked):

- `heartbeat.<role>` touched — watchdog sees the role as alive
- `intent/<id>.intent` written for the duration of the operation
- `journal.ndjson` appended with one structured event line
- `notify_from_journal` (via PostToolUse hook) fires inbox wake-up
  messages for downstream roles

Do NOT touch any of these by hand. Specifically: do not
`date > heartbeat.<role>` from a /loop role — it's a side-effect of
bin/task work. Exception: chat-mode roles (ARCHITECT-PLANNER,
MAINTAINER) that don't move tasks each tick may touch their heartbeat
manually when they act.

## schema.yaml as source of truth

`schema.yaml` defines: queues, role permissions per queue, allowed
transitions (from→to with required block_kind and authoring role),
block-kind required fields, task kinds. `bin/*` consults it on every
mutation. If a transition you need is not in schema.yaml, do **not**
add a manual `mv` — escalate to MAINTAINER via `bin/inbox send
MAINTAINER --kind ask` to amend the schema.

## Quick references

```bash
# Inspect a task without mutating
bin/task show <id>

# Append a block (validates against schema)
bin/task append-block <kind> --id <id> --field key=value ... --body "text"

# Move (validates that current role + block state authorise it)
bin/task mv <id> <to-queue> --reason "<short reason>"

# Sweep stale intent / heartbeat / orphans
bin/watchdog --project-dir "${PROJECT_ROOT}"
```

**Tokens used:** PROJECT_ROOT (PROJECT.env, exported by start_agent).
