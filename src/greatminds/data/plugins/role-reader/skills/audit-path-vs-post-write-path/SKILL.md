---
name: audit-path-vs-post-write-path
description: "Use when deciding the next mv for a READER-claimed task — the two intake paths (post-write review vs independent audit) have DIFFERENT routing rules. Post-write path — pass goes to feature_review, fail goes to feature_docs. Audit-only path — ALWAYS feature_review regardless of outcome (the audit IS the deliverable). Trigger on audit-only, post-write review, where to mv, feature_review vs feature_docs, READER routing, audit deliverable, reader_review mv."
---

# Audit-path vs post-write-path

READER has two intake paths with sharply different routing rules.
Confusing them is the most common READER mistake — and `greatminds task`
will sometimes REFUSE the wrong mv, which is the safety net but
slows things down. Get the routing right.

## How to tell which path you're on

Look at the latest `plan` block in the task's YAML:

| plan.audit_only field | Path |
|---|---|
| `audit_only: true` | **Audit-only** path |
| Not present, or `audit_only: false` | **Post-write** path |

You can also tell from history:
- Audit-only: task came directly to feature_docs_review FROM
  feature_plan (PLANNER routed via `greatminds plan --audit-only`)
- Post-write: task came to feature_docs_review FROM feature_docs
  (after WRITER moved it)

The `feature_docs_review` queue holds BOTH kinds. The `plan` block
disambiguates.

## Post-write path — what WRITER just changed

Mental model: WRITER changed docs to do/cover X. Did they do it
correctly?

Scope: narrow — the change itself. You read the diff (or the
`implementation.declared_files` from WRITER's impl block) and check:
- Does the change actually do X as the plan-block specifies?
- Are the new bits accurate (commands work, examples runnable)?
- Does the change introduce any new issues elsewhere?

Routing by outcome:
- **`outcome: pass`** → `mv → feature_review` (REVIEWER assembles
  parent if any, then verified)
- **`outcome: partial` or `fail`** → `mv → feature_docs` (back to
  WRITER with reader_review findings; WRITER iterates)

```bash
greatminds task append-block reader_review --id <id> \
  --field outcome=pass \
  --field scope="<scope desc>" \
  --body "..."

# pass:
greatminds task mv <id> feature_review --reason "docs change approved"

# fail/partial:
greatminds task mv <id> feature_docs --reason "back to WRITER per findings"
```

## Audit-only path — full-surface vs reality

Mental model: PLANNER doesn't know what's wrong with docs surface X
— they're asking you to investigate. Your reader_review IS the
deliverable.

Scope: broad — the whole surface named in the plan-block body.

Routing by outcome — KEY DIFFERENCE: **ALWAYS** `mv → feature_review`,
regardless of `outcome:` (pass, partial, or fail). Why:

- There's no WRITER waiting to take it back to. PLANNER routed
  directly to you bypassing WRITER. There's no `feature_docs` task to
  bounce back to.
- The audit findings ARE the deliverable. REVIEWER verifies that the
  audit was conducted properly (you actually probed, findings are
  structured), not whether the findings are positive.
- Negative findings (`outcome: fail`) are valuable too — they tell
  PLANNER to spawn a new WRITER task with specific fixes.

```bash
greatminds task append-block reader_review --id <id> \
  --field outcome=fail \
  --field scope="docs/admin-guide/identity-and-remotes.md" \
  --body "..."

greatminds task mv <id> feature_review --reason "audit complete; findings recorded"
```

If you try `greatminds task mv <id> feature_docs` on an audit-only task,
`greatminds task` will refuse: "task is audit-only (no write plan); WRITER
can't act on it". That's the safety net mentioned above — heed it,
re-route to feature_review.

## What PLANNER does AFTER an audit

PLANNER reads your verified audit and decides:
- If `outcome: pass`: nothing to do. Docs OK, audit confirms.
- If `outcome: partial / fail`: PLANNER spawns a **separate** WRITER
  task using `greatminds plan --scope docs --assignee-role TECHNICAL-WRITER`
  with concrete edits derived from your findings. The WRITER task
  cites your verified audit as its source-of-requirements.

Your audit doesn't have to enumerate fixes — that's PLANNER's
decomposition step. You just record the divergence.

## Common confusions

### "I'm on audit-only but my outcome is pass; can I mv to feature_docs?"

No. There's no WRITER plan; there's nothing for WRITER to do. mv to
feature_review with `outcome: pass`; REVIEWER will verify the audit
and the task lands in verified/.

### "I'm on post-write and my outcome is pass but I have minor findings"

Either:
- Findings are truly low-severity, don't block ship → `outcome: pass`,
  mv → feature_review, list minor findings in body as "future
  improvements".
- Findings are non-trivial → `outcome: partial`, mv → feature_docs,
  WRITER addresses.

Don't downgrade severity to ship faster. WRITER appreciates honest
findings; PLANNER appreciates accurate severity.

### "I'm on audit-only, want to also fix one minor typo I saw"

No. READER never edits docs. The fix goes into your findings; PLANNER
spawns a WRITER task; WRITER edits.

## Dependency on stand access

For audit-only tasks that need a deployed stand to verify reality:
- File `greatminds stand request --evidence-for <id>` describing what state
  you need.
- Wait for SK's stand_done before continuing audit.
- Reference SK's stand_done commit in your findings (so REVIEWER can
  reproduce).

For post-write tasks, usually you don't need stand access — you're
checking the WRITER's change against the docs source-of-truth, not
against deployed reality. (Exception: if WRITER's change documents UI
behaviour, you may need to verify against the deployed UI.)

## Don't

- Don't try to fold audit findings into a "fix" yourself.
- Don't `mv → feature_docs` on an audit-only task. `greatminds task` will
  refuse and you'll have wasted time.
- Don't mark `outcome: pass` on a long audit if you didn't actually
  audit the whole scope — narrow the scope honestly and note what
  wasn't covered.

**Tokens used:** none.
