---
name: reader-review-block-craft
description: Use when filling the reader_review block — outcome (pass / partial / fail), structured findings (file / section / specific gap / severity), distinguishing actionable-for-WRITER from product-gap (user_feedback) findings, ack discipline. Trigger on "reader_review block", "review block fields", "findings format", "pass partial fail", "actionable for writer", "user_feedback escalation".
---

# Reader-review block craft

The `reader_review` block is READER's output, structured for two
downstream consumers: WRITER (post-write path) and PLANNER
(audit-only path). The structure must let them act without further
clarification.

## Required fields

```yaml
- kind: reader_review
  by_role: READER
  at: <ISO>
  outcome: pass | partial | fail
  scope: "<one-line description of what was reviewed>"
  findings:
    - severity: high | medium | low
      location: "<file:section or file:line-range>"
      claim: "<what the docs say, quoted or paraphrased tightly>"
      reality: "<what you actually observed; include probe command/output if relevant>"
      diff_type: <category from reality-vs-docs-audit>
      suggested_action: "<one-sentence: what would resolve this>"
  body: |
    <synthesis paragraph: overall judgment, what was checked but found OK, any caveats>
```

## outcome semantics

| outcome | Meaning | Routing (post-write) | Routing (audit-only) |
|---|---|---|---|
| `pass` | Docs match reality on the audited surface; no actionable findings | `mv → feature_review` | `mv → feature_review` |
| `partial` | Minor findings (low/medium); change is mostly correct but needs touch-up | post-write: `mv → feature_docs` (back to WRITER); audit: still `→ feature_review` (audit IS the deliverable) | `mv → feature_review` |
| `fail` | Significant divergence; change shouldn't ship as-is | `mv → feature_docs` (back to WRITER) | `mv → feature_review` (findings inform a new WRITER task) |

Note the audit-only specifics — see `audit-path-vs-post-write-path`.
For audit-only, **always** mv to feature_review regardless of outcome
— the audit IS the deliverable; findings inform what PLANNER spawns
next.

## Severity rubric

| Severity | Criterion |
|---|---|
| **high** | A reader following the docs reaches an incorrect / broken state. Blocks the documented flow. |
| **medium** | Reader hits friction (extra step needed, name mismatch, outdated info) but can recover with guesswork. |
| **low** | Cosmetic — typo, broken link, stale screenshot, minor inconsistency. |

Use severity sparingly — don't grade everything "medium" by default.
"high" is reserved for "this blocks the documented outcome".

## Actionable for WRITER vs product gap

Some divergences are docs being wrong about a correct product:
WRITER fix.

Some divergences are docs being CORRECT about an INCORRECT product:
the product is broken. WRITER can't fix it without code changes.
That's a `user_feedback` ticket for PLANNER:

```bash
bin/task new --stream product --in-queue user_feedback \
  --kind bugfix --scope backend \
  --title "endpoint X returns 500 instead of documented 422"
```

In the reader_review finding, note:

```yaml
findings:
  - severity: high
    location: "docs/reference/api.md, POST /items section"
    claim: "Doc says: POST /items with empty name returns 422 with detail 'name required'"
    reality: "POST returns 500 with traceback 'IntegrityError null violates constraint'. The validator that should produce 422 isn't reached; product bug, not docs bug."
    diff_type: product_gap
    suggested_action: "Filed user_feedback/0223 as product bugfix. After fix lands, docs should be re-verified — no docs change yet."
```

This distinction matters: WRITER can't fix it; PLANNER needs to spawn
a developer task.

## Quote vs paraphrase

In `claim:`, quote the docs literally for short claims (≤15 words);
paraphrase tightly for longer ones with a `[quoting:]` note:

```yaml
claim: "[quoting docs/setup.md:42-45] After running coord-init, the
       project is fully bootstrapped and ready to run bin/coord-tmux."
```

vs short:

```yaml
claim: "Doc step 3 says: \"Run `bin/foo --bar baz`\""
```

This gives WRITER precise pointers.

## Body — synthesis, not exhaustive list

The `body:` field is a paragraph or two summarising the audit. It's
NOT a re-listing of findings (those are structured above). Use it to
convey:
- What was scoped (and what was deliberately excluded)
- Overall judgment in plain English
- Patterns across multiple findings (e.g., "5 of 6 findings are
  outdated command names — likely a single rename that wasn't
  propagated")
- What WAS verified and found correct (so the next REVIEWER /
  PLANNER knows what's solid)
- Any limitations of the audit (couldn't access X, didn't run Y)

## Post-write vs audit-only — small differences

Post-write reader_review:
- Scope is usually narrow (the change WRITER just made)
- `outcome: pass` is normal and common; `fail` returns to WRITER
- Findings are typically about the change itself

Audit-only reader_review:
- Scope is broad (a whole surface)
- `outcome: pass` is rare (a full surface rarely has zero divergence)
- Many findings are normal; that's why the audit was commissioned
- ALWAYS mv → feature_review (audit IS the deliverable)

## Don't

- Don't write findings as if they're TODO items for yourself. They're
  for WRITER / PLANNER. Use third-person, declarative.
- Don't grade severity to "drive urgency" — grade by the rubric.
  Inflating severity erodes trust.
- Don't withhold findings to "go easy on the WRITER" — partial
  findings mean WRITER fixes the visible part and the rest lingers
  in your head as undocumented debt.
- Don't edit the docs yourself even when "it's just one typo". READER
  never writes.

**Tokens used:** none.
