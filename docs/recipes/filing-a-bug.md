# Filing a Bug

Use product tasks for greatminds bugs:

```bash
greatminds task new \
  --stream product \
  --kind bugfix \
  --scope backend \
  --title "Fix task move validation"
```

The planner triages the report, writes a plan, and routes it to the owner queue.
Do not bypass planning by moving a task directly into an implementer queue.

## From a review session

During intensive review, `EXPLORER` files focused bug tasks into
`feature_inbox/`. The report should include:

- what scenario was being tested
- what happened
- what should have happened
- reproduction steps
- stand or command evidence when available

## Upstream issues

For bugs in greatminds itself, use the repository issue tracker:

```text
https://github.com/veryviolet/greatminds/issues
```
