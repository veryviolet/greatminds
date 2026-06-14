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

For suspected bugs in greatminds itself, route the report through
`MAINTAINER`. `greatminds report-upstream` is gated to the MAINTAINER role by
`coordination/schema.yaml` under `report_upstream.permissions.invoke`; other roles should not
file directly.

If you are an agent role and you find a likely upstream bug, send MAINTAINER an
ask with the repro and diagnostics:

```bash
greatminds inbox send MAINTAINER \
  --kind ask \
  --task <task-or-session-ref> \
  --body "<repro steps, expected result, actual result, command output, environment>"
```

MAINTAINER triages the ask, deduplicates against known reports, and either
files the upstream issue with `greatminds report-upstream` or replies with the
reason it is not an upstream bug.

When using `greatminds report-upstream --mode gh`, the downstream host must
already have the GitHub CLI installed and authenticated:

```bash
gh auth login
```

Without local `gh` authentication, use `--mode url` to generate a prefilled
issue URL or authenticate the host before filing. The upstream issue tracker is:

```text
https://github.com/veryviolet/greatminds/issues
```
