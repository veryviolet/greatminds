---
name: impl-block-craft
description: Use when writing the implementation block for a feature_dev / feature_ui_dev / feature_docs task before mv to feature_test. Covers the required fields (base_commit, declared_files, summary, ready_for_test), declared-files discipline (exact set, no globs), full git rev-parse HEAD discipline (no int-coerce trap), handoff semantics. Trigger on "implementation block", "declared_files", "base_commit", "ready_for_test", "mv to feature_test", "filing impl".
---

# impl-block craft

Implementer roles (DEVELOPER, UI-DEVELOPER, TECHNICAL-WRITER) file an
`implementation` block via `bin/task append-block implementation`
before `mv → feature_test` (or `feature_docs_review` for docs).

## Required fields

```yaml
- kind: implementation
  by_role: DEVELOPER | UI-DEVELOPER | TECHNICAL-WRITER
  at: <ISO>
  base_commit: <full git rev-parse HEAD — 40 chars>
  declared_files:
    - app/datasources/manager.py
    - app/datasources/api.py
    - tests/unit/test_datasources.py
  summary: |
    <one-paragraph summary of what changed and why, referencing plan-block intent>
  ready_for_test: true
```

## declared_files — exact set, no globs

`declared_files` MUST list the **exact** files this task changed in the
working tree, byte-for-byte the same as what `git diff --name-only`
shows. Do NOT use globs (`lib/foo/*`), do NOT optimistically list
files you "might" touch, do NOT omit files you changed.

Why this matters (feedback 0373/0374): ARCHITECT-REVIEWER commits at
approve time using **declared_files as the path list**. If the diff
contains files NOT in declared_files, AR's commit doesn't capture them
— they leak into the co-tenant working tree of the next task that
touches the same dir. If declared_files lists files NOT actually
changed, the commit silently includes whatever cruft is in those
paths.

Recipe:
```bash
git diff --name-only HEAD > /tmp/changed.txt
# Confirm /tmp/changed.txt matches your declared_files set EXACTLY.
# Add missing, remove extras.
```

## base_commit — full rev-parse HEAD

Capture `base_commit` at impl-write time with:

```bash
git rev-parse HEAD
```

Use the full 40-char SHA, NOT `head -c 12` (an all-digit truncated
prefix gets coerced to int by strict YAML and fails validation —
0377 incident). NOT a stale value from a sibling task. NOT the value
from the plan-block (plan's base_commit is when planning started; your
impl base_commit is when YOU branched).

For `stand_required: true` tasks, additionally set
`gate_check_commit` in the implementation block: copy it from the
`commit:` field of `stand_done/<id>.yaml` AFTER SK confirms readiness
(0366 nit) — NOT from `impl.base_commit`.

## ready_for_test gating

Set `ready_for_test: true` ONLY when:
- All changes complete; no TODOs left in the implementation
- declared_files matches `git diff --name-only` exactly
- For stand_required tasks: matching `stand_done/<id>.yaml` exists
  with the gate_check_commit you'll write
- Local pre-flight (lint/type/unit-test) passes if applicable to scope

If any of these is incomplete, leave `ready_for_test: false` and add a
follow-up iteration; do NOT mv yet.

## Handoff

```bash
bin/task append-block implementation --id <id> \
  --field base_commit="$(git rev-parse HEAD)" \
  --field declared_files=app/foo.py,app/bar.py,tests/unit/test_foo.py \
  --field ready_for_test=true \
  --body "<summary prose>"

bin/task mv <id> feature_test --reason "ready for verification"
```

`bin/task mv` checks that `ready_for_test=true` is set in the latest
implementation block; refuses otherwise.

## Iteration loop

If TESTER returns fail/partial, the task lands back in your queue with
a `tests` block describing what failed. Add an `iteration` block
explaining the fix, set `ready_for_test: true` again, mv → feature_test.
See `iteration-and-blocking` for details.

**Tokens used:** none.
