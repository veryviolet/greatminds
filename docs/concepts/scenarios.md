# Scenarios

greatminds uses three scenario labels in task plans. The label tells roles how
the work should move.

## Scenario A: product pipeline

The standard path for features, bug fixes, refactors, and docs:

```text
feature_inbox -> feature_plan -> feature_dev|feature_ui_dev|feature_docs
              -> feature_test|feature_docs_review -> feature_review -> verified
```

Use this when work should be planned, implemented, independently checked, and
reviewed before commit.

## Scenario B: intensive review

The planner opens a review session. `EXPLORER` uses a live stand, follows
scenarios, and files bugs back into the product pipeline.

Use this when the product needs a focused walk-through rather than a single
predefined change.

## Scenario C: UI rapid iteration

`UI-DEVELOPER` works in chat mode against a Vite-backed stand. Changes flow
through hot reload instead of the full per-change task pipeline.

Use this for short UI adjustment sessions where the user is actively steering
the outcome.
