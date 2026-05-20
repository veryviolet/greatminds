---
name: evidence-chain-verification
description: Use when AR reviews a task in feature_review to cross-check the evidence chain — plan-block → impl-block → tests-block (+ reader_review for docs) → stand_done (if stand_required). Covers gate_check_commit alignment, declared-files vs actual diff, test base_commit matching, READER outcome coverage. Trigger on "evidence chain", "verify evidence", "gate_check_commit match", "declared_files vs diff", "test base_commit", "reader_review coverage".
---

# Evidence chain verification

When a task lands in `feature_review/`, AR must verify that the chain
of evidence is **internally consistent** before approving into
`verified/`. A coherent chain is what makes the verified state
trustworthy.

## The chain

For a typical task:

```
plan (PLANNER)
   ↓ specifies: base_commit, scope, mode, stand_required, ready_for_implementation
impl (DEVELOPER / UI-DEVELOPER / TECHNICAL-WRITER)
   ↓ adds: own base_commit (post-fix), declared_files, gate_check_commit if stand_required
[iterations: 0..N]
   ↓ each: responds_to=tests, new base_commit, updated declared_files
tests (TESTER)
   ↓ outcome, base_commit (matches impl's latest), probes ran
[reader_review (READER) if docs scope]
   ↓ outcome, scope, findings
review (AR)
   ↓ outcome=approved, commit:, push_refs:
verified/<id>.yaml
```

Each arrow MUST satisfy a check.

## Check 1 — declared_files vs actual diff

```bash
# What did the implementer claim?
bin/task show <id> | yq -r '.blocks[] | select(.kind=="implementation") | .declared_files[]'

# What did they actually change relative to impl's base_commit?
git diff --name-only <impl_base_commit>..HEAD
```

These two lists MUST match (set equality). Mismatch modes:
- declared lists files NOT in diff → implementer over-claimed; either
  the file wasn't actually changed (drop from declared) or the change
  was somewhere else (correct base_commit).
- diff has files NOT in declared → implementer missed something;
  those changes would NOT be committed by AR (commit-by-paths
  discipline) and leak into co-tenant working tree. REJECT.

The mismatch is a blocker. Author REJECT with the diff so implementer
sees both lists.

## Check 2 — gate_check_commit alignment (stand_required tasks)

If plan says `stand_required: true`:

```bash
# Find the matching stand_done
ls coordination/stand_done/<id>*.yaml

# Extract its commit:
grep '^commit:' coordination/stand_done/<id>-*.yaml | awk '{print $2}'

# Compare to impl-block's gate_check_commit
bin/task show <id> | yq -r '.blocks[] | select(.kind=="implementation") | .gate_check_commit'
```

These MUST be equal. Mismatch means: impl-block was filed against a
different SHA than what SK actually deployed for verification. The
tests don't reflect the deployed code. REJECT or ask SK for fresh
stand_done at impl's commit.

## Check 3 — test base_commit matches impl's latest

```bash
# Latest impl block's base_commit
bin/task show <id> | yq -r '
  [.blocks[] | select(.kind=="implementation" or .kind=="iteration")] |
  reverse | .[0].base_commit
'

# Tests-block base_commit
bin/task show <id> | yq -r '.blocks[] | select(.kind=="tests") | .base_commit'
```

These MUST be equal. Mismatch means: TESTER ran against an older SHA
than the final implementation. Tests don't cover the actual code that
will land. REJECT (TESTER re-runs against current).

## Check 4 — reader_review present and covers the change (docs scope)

For tasks with `scope: docs`:

```bash
bin/task show <id> | yq -r '.blocks[] | select(.kind=="reader_review")'
```

Must exist. Its `scope:` field should reasonably cover what WRITER
changed (the implementation's declared_files). If WRITER changed
`docs/admin/identity.md` but READER's reader_review scope is
`docs/user-guide/quickstart.md`, the review doesn't cover the change.

Audit-only tasks (no WRITER) — verify the reader_review exists with
findings; outcome (pass/partial/fail) doesn't gate approval (audit
IS the deliverable; PLANNER will spawn writer tasks from findings).

## Check 5 — claim chain matches plan intent

Read the parent task's plan body (if this is a sub-task) and the
sub-task's plan + impl. Does the sub-task's implementation actually
deliver what the parent plan asked for?

This is judgment, not mechanical. Concrete failure modes:
- Plan asked for "API endpoint that does X+Y+Z"; impl delivered X+Y
  but skipped Z silently. → REJECT; WRITER didn't ship Z.
- Plan asked for "FastAPI route"; impl added a gRPC method. → likely
  REJECT or send back to clarification.
- Plan asked for "fix the bug described in inbox/0142"; impl added a
  new feature that's tangentially related. → likely REJECT.

## Cross-cutting: timestamps in order

The chain's blocks should have monotonically increasing timestamps
(plan < impl < tests < reader_review < your review). Out-of-order
timestamps indicate either clock skew (forgivable, mention in review)
or evidence fabrication (concerning — investigate). Common case:
TESTER ran before impl's latest iteration → outdated tests, see
Check 3.

## When to fail vs approve

Approve when:
- All five checks pass
- No glaring architectural smell (see `architectural-review`)
- Push protocol can be cleanly executed (see `commit-and-push-protocol`)

Reject (`outcome: rejected`, mv back to implementer queue) when:
- Any check fails AND fix requires implementer action

Ask for clarification (inbox ask to PLANNER) when:
- The plan-block itself is ambiguous about what success looks like
- The impl is correct by one reading and broken by another

Partial-approve (`outcome: partial`) when:
- Multi-child parent where some children are verified but not all;
  AR notes which children still need work, parent stays in
  feature_review

## Don't

- Don't approve without each check (it's a small, fast pass — do it).
- Don't reject for stylistic preferences masquerading as
  architectural concerns. Style is the implementer's call.
- Don't ask for clarification on every uncertainty — try the
  reasonable interpretation; if it's clearly defective, reject with
  specifics.

**Tokens used:** none.
