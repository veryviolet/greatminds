# Transitions

Allowed transitions are encoded in `coordination/schema.yaml` in an installed
project. The CLI enforces the current
role, current queue, target queue, and required readiness evidence.

`verified/` is not an absolute sink. If later review finds that verified work
is wrong, obsolete, or already reverted, `ARCHITECT-REVIEWER` can append a
`rollback` block with a non-empty `reason` and move the task from `verified/`
to either `archive/` or `feature_review/`. Use `archive/` for withdrawn work
whose code state already matches the rollback; use `feature_review/` when the
task needs another amendment or review cycle.

Read the source schema when changing the workflow:

[schema.yaml on GitHub](https://github.com/veryviolet/greatminds/blob/main/src/greatminds/data/schema.yaml)

```yaml
--8<-- "src/greatminds/data/schema.yaml"
```
