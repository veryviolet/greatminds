# Operations Runbook

This runbook is for operators and `MAINTAINER`. Product roles should use the
same diagnostic commands, but they should route infrastructure changes through
`MAINTAINER` instead of editing coordination state directly.

## CLI-Only Discipline

All mutations under `coordination/` go through the `greatminds` CLI. Do not
move task files with `mv`, edit inbox files by hand, append journal lines
directly, or touch heartbeat files manually. The CLI validates role ownership,
schema fields, transition readiness, intent files, journal lines, and heartbeat
side effects as one operation.

Common mutation commands:

```bash
greatminds task new --stream product --kind feature --scope backend --title "..."
greatminds task append-block implementation --id TASK_ID --field files=docs/index.md --field ready_for_test=true
greatminds task mv TASK_ID feature_test
greatminds inbox send ARCHITECT-PLANNER --kind ask --task TASK_ID --body "..."
greatminds stand lease --task TASK_ID --worktree "$(greatminds worktree path TASK_ID)" --profile full-deploy
greatminds stand release --lease-id LEASE_ID --result pass
```

Authoritative read-only checks:

```bash
greatminds task show TASK_ID
greatminds task list feature_docs
greatminds task paths
greatminds gate-check TASK_ID
greatminds wake-check
greatminds watchdog
greatminds agent status
greatminds journal tail
```

If a CLI result conflicts with a remembered state or a directory listing, trust
the CLI.

## Fleet Launch And Venvs

Launch the fleet through the fixed coordination environment:

```bash
./.venv-coord/bin/greatminds daemon start
./.venv-coord/bin/greatminds launch --target tmux
```

The fleet venv is intentionally stable. It runs the daemon and live agents.
The editable development venv is for changing and testing greatminds itself.
Do not launch a long-running fleet with `uv run`; `uv run --active` can write
editable paths from a task worktree into the active fleet venv and leave broken
`.pth` files after the worktree is removed.

When developing greatminds locally, use an isolated development venv:

```bash
unset VIRTUAL_ENV
uv venv .venv
uv pip install --python .venv/bin/python -e '.[docs]'
.venv/bin/greatminds --help
```

After upgrading the package used by the fleet, refresh the running processes:

```bash
./.venv-coord/bin/greatminds update
./.venv-coord/bin/greatminds restart --bootstrap
./.venv-coord/bin/greatminds agent status
```

## Court Fix

A court fix is a small, direct correction to coordination mechanics or docs
needed to unblock the fleet. Keep it narrow:

1. Confirm the problem with `greatminds watchdog`, `greatminds agent status`,
   `greatminds wake-check`, or `greatminds gate-check`.
2. Change only the affected canon, CLI, role doc, or project config.
3. Run the closest validation command.
4. Restart or bootstrap only the roles that need the new behavior.
5. Tell `ARCHITECT-PLANNER` when product work must be replanned.

Do not use a court fix to skip queue ownership or review gates.

## Venv Recovery

If agents fail to import `greatminds`, check which venv the live processes use:

```bash
greatminds agent status
```

If the fleet venv contains stale editable paths from a deleted worktree,
recreate or repair `.venv-coord`, reinstall the intended package version, then
restart the daemon and agents from that venv. Avoid `uv run` during recovery;
call the target venv's `greatminds` binary directly.

## Wake STAND-KEEPER

Stand work is lease-backed. A holder requests a lease with:

```bash
greatminds stand lease --task TASK_ID --worktree "$(greatminds worktree path TASK_ID)" --profile full-deploy
```

`coordd` wakes `STAND-KEEPER` on stand-state changes. If the lease stays in
`preparing`, inspect:

```bash
greatminds stand status
greatminds agent status STAND-KEEPER
greatminds watchdog
```

If the stand is down, `STAND-KEEPER` recovers it and runs:

```bash
greatminds stand up --reason "recovered ..."
```

The holder, not `STAND-KEEPER`, releases the lease after product probes:

```bash
greatminds stand release --lease-id LEASE_ID --result pass
```

## Stuck Driven Turns

Driven roles do not keep a persistent agent process. `coordd` creates one turn
per event and holds `coordination/.locks/driven-<role>.lock` for the duration
of that turn. New lock files contain JSON metadata such as `role`, `driver`,
`started_at`, `coordd_pid`, and `log_path`.

Use:

```bash
greatminds dashboard --once
greatminds watchdog
```

If the dashboard shows `stuck` or watchdog reports `STUCK DRIVEN TURNS`, treat
the turn as an infrastructure incident, not normal implementation progress.
Inspect the named `log_path`, the matching `.pending` marker, and coordd logs.
Do not assume a task is being worked just because a driven lock exists.

## Driven Retry Backoff

When a driven turn exits with a rate-limit, timeout, or execution error,
`coordd` records retry state in
`coordination/.locks/driven-<role>.retry.json`. The live scheduler uses this
state to re-drive the role after backoff and restores it after a coordd
restart. `greatminds dashboard --once` shows these roles as `backoff` or
`failed`; `greatminds watchdog` reports them under `DRIVEN RETRIES`.

`backoff` means no turn is running because coordd is intentionally waiting for
the next retry time. `failed` means bounded hard retries were exhausted and
auto-retry stopped after escalation. Inspect the retry detail and the latest
`.turns/<role>-*.log`; clear the root cause, then trigger a real queue/inbox
event so the role gets a fresh attempt.

## No-Git Deploy Payloads

A deployed payload without `.git` is valid only as a validation target. It is
not a legal source checkout for backend/UI implementation work that requires
per-task worktrees.

If a required code task is routed toward an implementer queue and the project
root is not a git repository, `greatminds task mv` must fail before the task
reaches the implementer. Recover by pointing Greatminds at a real source git
checkout or initializing the intended source project, then rerun the route.
Do not reintroduce stale `.git` pointers into deployed payload directories.
