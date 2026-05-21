---
name: wake-and-unblock
description: Use when AR runs the per-tick wake sweep — greatminds wake-check to find blocked tasks whose dependencies are now verified, and the special role of AR as the only one who can unstick permanent jams (terminal-queue rule violations). Trigger on "wake_check", "greatminds wake-check", "feature_blocked", "unblock", "dependencies verified", "stuck task", "AR unstick".
---

# Wake-and-unblock

AR owns the feature_blocked queue along with feature_review. Two
related operations:
1. Routine: wake tasks whose dependencies became verified.
2. Repair: unstick permanently-jammed tasks (terminal-queue rule
   violations).

## Routine wake sweep

At each tick, run `greatminds wake-check`:

```bash
greatminds wake-check
```

Output looks like:

```
ready-to-wake:
  feature_blocked/0042-foo-task.yaml
    deps: verified/0040-prereq-a.yaml (exists ✓)
          verified/0041-prereq-b.yaml (exists ✓)
    resume_to: feature_dev

blocked:
  feature_blocked/0050-bar-task.yaml
    deps: verified/0049-not-yet.yaml (missing — still in feature_test)
    resume_to: feature_ui_dev
```

For each ready-to-wake task, mv it to its `resume_to`:

```bash
greatminds task mv 0042-foo-task feature_dev --reason "deps 0040+0041 verified — woken"
```

That's it. Implementer's loop picks it up next tick.

## Wake_check output meanings

- `ready-to-wake`: all dependencies' verified/<id>.yaml files exist
  → safe to mv. No further check needed; the implementer + TESTER +
  AR chain will revalidate.
- `blocked` (not all deps verified): leave alone. Will become ready
  when remaining deps land.
- `(missing)`: dependency path is literally not findable on disk —
  this is the permanent-jam case, see below.

## Permanent jams — terminal-queue rule violations

If wake_check reports `(missing)` for a dependency, the blocked task
referenced a non-terminal queue path (e.g., `feature_dev/<id>.yaml`)
which has since moved on. The literal path no longer exists. The task
will never wake by itself.

Per the protocol (feedback 0410), `dependencies:` MUST be
`verified/<id>.yaml` ONLY. The implementer who filed the blocked
block violated this. AR is the only role authorised to repair.

Repair procedure:

```bash
# 1. Identify the stuck task
ls coordination/feature_blocked/

# 2. Inspect its blocked block — find the bad dependency
greatminds task show <stuck-id> | grep -A5 'kind: blocked'

# 3. Find where the dep actually went (likely verified/, possibly
#    archive/ if it was archived for being misplanned)
ls coordination/verified/ coordination/archive/ | grep <dep-id>

# 4. AR rewrites the blocked block to point to the correct
#    terminal-queue path. Use greatminds task append-block to add a
#    REPAIR block; do not edit the blocked block in place.
greatminds task append-block blocked --id <stuck-id> \
  --field dependencies=verified/<correct-dep-id>.yaml \
  --field resume_to=<original-resume-target> \
  --body "REPAIR: prior blocked block referenced feature_dev/<id> which migrated to verified. Replacing with terminal-queue path. (Per feedback_blocked_dep_terminal_path; this is AR-only repair.)"

# 5. Re-run wake_check to confirm it's now ready
greatminds wake-check
# Should now list this task in ready-to-wake

# 6. mv as normal
greatminds task mv <stuck-id> <resume_to> --reason "post-AR-repair: deps verified"
```

The new blocked block ADDS to history (doesn't erase the bad one) —
audit trail preserves what the implementer did, and AR's repair.

## When to refuse repair

If the dependency referenced doesn't exist ANYWHERE (not in verified/,
not in archive/, not in any active queue), the dep was never real.
Escalate to PLANNER — the original plan probably needed a sub-task
that was never created, or the dependency reference was a hallucinated
task id.

```bash
greatminds inbox send ARCHITECT-PLANNER --kind ask \
  --task <stuck-id> \
  --about "feature_blocked task references nonexistent dep" \
  --body "Task <stuck-id> in feature_blocked declared dependency
  verified/<id-that-doesn't-exist>.yaml. I cannot find this dep in
  any queue or archive. Either the original plan needed a sub-task
  that was never created, or the dep id was wrong from the start.
  Please clarify."
```

Don't guess. Don't fabricate a dep. Wait for PLANNER guidance.

## Coordination with PLANNER (chain repair beyond AR's scope)

Some blockages are bigger than a path-rewrite — the dependency chain
itself was wrong. Example: task A depends on task B, but B was
SUPPOSED to depend on A. Circular. AR's repair scope is path-level;
chain-level redesign goes to PLANNER.

Escalate via inbox; PLANNER restructures (potentially archiving
some tasks, creating new ones, re-planning), then AR repairs paths
when PLANNER's plan stabilises.

## Tick discipline for AR

AR's tick pattern is roughly:
1. `greatminds wake-check` — wake what's ready, escalate jams
2. Process feature_review/ tasks in oldest-first order (evidence-chain
   verification, architectural review, commit+push, mv to verified)
3. Process feature_blocked/ repairs if any (see above)
4. Read inbox if any (asks from other roles or coordd)

That's the steady-state. heartbeat is updated as a side effect of
greatminds task / greatminds inbox / greatminds wake-check; AR doesn't touch it directly.

## Don't

- Don't mv tasks out of feature_blocked manually if wake_check didn't
  list them ready. The deps aren't satisfied; mv'ing breaks the
  chain.
- Don't edit the blocked block in place (no Edit on coordination
  files). Use `greatminds task append-block blocked` to add a REPAIR block;
  the old block stays as history.
- Don't fabricate verified/<id>.yaml entries by `touch`ing them to
  "satisfy" the wake_check. That's lying to the protocol; downstream
  agents reading the verified history will hit nonexistent / empty
  YAMLs.

**Tokens used:** none.
