---
name: review-block-craft
description: Use when AR fills the review block — outcome (approved/rejected/partial), evidence_pointers, push_refs, rejection findings that let implementer act without guesswork, iteration cycle with implementer. Trigger on "review block", "outcome approved", "outcome rejected", "evidence_pointers", "push_refs", "AR review findings".
---

# Review-block craft

The `review` block is AR's output, written to the task right before
mv to verified/ (approve) or back to feature_dev/etc. (reject). It
documents the decision and provides traceable evidence.

## Required fields

```yaml
- kind: review
  by_role: ARCHITECT-REVIEWER
  at: <ISO>
  outcome: approved | rejected | partial
  evidence_pointers:
    plan: blocks[0]                                      # which plan block was the contract
    impl: blocks[3]                                      # the implementation block (or latest iteration)
    tests: blocks[4]
    reader_review: blocks[5]                             # if docs scope
    stand_done: stand_done/0042-...yaml                  # if stand_required
  commit:                                                # set when outcome=approved
    sha: <40-char SHA after AR commits the declared_files>
    repos:
      - { repo: canon, ref: main, push_status: pushed }   # if canon was touched
      - { repo: lattice, ref: main, push_status: pushed }
  body: |
    <synthesis: what was reviewed, what was checked, decision rationale>
```

## outcome semantics

| outcome | Next mv | Meaning |
|---|---|---|
| `approved` | `verified/` | All checks pass; AR has committed and pushed |
| `rejected` | `feature_dev/feature_ui_dev/feature_docs` per scope | Findings require implementer iteration |
| `partial` | stays in `feature_review/` | Parent task with some children verified, some pending — AR notes status |

`partial` is rare and specifically for multi-child parent tasks; for
single tasks, it's either `approved` or `rejected`.

## evidence_pointers — let future readers retrace

Cite the exact block indices / file paths used in the evidence chain.
This lets:
- Future maintenance (months later) — someone can find what was
  checked and why
- Audit by a fresh AR (if a verified task is later reopened)
- Tooling that walks the chain

Use `blocks[N]` indexing into the task's `blocks:` array (0-indexed
position in the YAML).

## Rejection findings — actionable, not vague

When `outcome: rejected`, body MUST give the implementer enough to act
without guesswork. Bad rejection:

> "The implementation doesn't look right. Please fix and re-submit."

Good rejection:

> Rejected. Two specific blockers:
>
> 1. declared_files (lines 12-15 of impl block) lists `app/foo.py`
>    and `tests/test_foo.py`, but `git diff --name-only base..HEAD`
>    also shows `app/foo_utils.py`. The actual diff has a third file
>    that's not declared. Either:
>    - Add `app/foo_utils.py` to declared_files (if intentional), or
>    - Revert that file's changes (if it was an accidental edit).
>
> 2. tests-block base_commit (`abc1234`) doesn't match impl's latest
>    iteration base_commit (`def5678`). TESTER ran against an older
>    SHA. After fixing #1, re-mv to feature_test for re-run against
>    current.

Numbered, specific, actionable. The implementer has a clear path.

## Iteration cycle with implementer

A typical reject → fix → re-submit cycle:

```
review (AR, outcome=rejected, body=findings)
  → mv to feature_dev (resume_to from rejection)
implementation iteration (DEV, responds_to=review, fix)
  → mv to feature_test
tests (TESTER, outcome=approved, base_commit matches new impl)
  → mv to feature_review
review (AR, outcome=approved if everything now lines up)
  → mv to verified
```

Each cycle adds blocks to the task; nothing is overwritten. The task's
history shows the full debugging arc.

## When body documents the approval

For `outcome: approved`, the body should briefly recap:

- What you checked (the 5 evidence-chain checks + architectural notes)
- Anything notable (architectural smells you let pass, follow-up
  tasks you spawned, etc.)
- The commit + push artifacts

Example:

```yaml
body: |
  Evidence chain: OK.
  - declared_files matches diff exactly (5 files).
  - impl base_commit a032302 == tests base_commit a032302 ✓
  - stand_required=true; gate_check_commit a032302 == stand_done/0042's commit ✓
  - reader_review N/A (backend scope).

  Architectural: clean. New import app.api → app.services.items
  follows the established service-layer pattern. Observability:
  logger.info in create_item is fine for this PR; structured
  logging follow-up filed as user_feedback/0223.

  Commit + push:
  - canon: no canon changes for this task
  - lattice: a032302..dc51b23 main → main (pushed)

  Approving → verified.
```

## push_refs format

For each repo the task touched and was committed to:

```yaml
commit:
  sha: dc51b23
  repos:
    - { repo: lattice, ref: main, push_status: pushed,
        push_proof: "a032302..dc51b23  main -> main" }
    - { repo: canon,   ref: main, push_status: not_touched }
```

`push_proof` is the exact line `git push` printed. This is the
verifiable artifact — if AR claims pushed but didn't, the line would
be missing or different. See `commit-and-push-protocol`.

## Don't

- Don't write `outcome: approved` if you haven't pushed. Per
  `commit-and-push-protocol`, unpushed verified = not done.
- Don't reject for stylistic preferences. Reject for evidence-chain
  failures or architectural blockers; everything else goes in body
  as notes.
- Don't combine multiple unrelated rejection reasons into one
  paragraph. Numbered list, each item one specific actionable.
- Don't approve "with caveats" by squeezing concerns into body. If
  it should ship, ship cleanly. If concerns are blocking, reject. If
  follow-up needed, spawn a follow-up task.

**Tokens used:** none.
