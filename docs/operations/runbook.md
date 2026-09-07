# Operations Runbook

This runbook is for operators and `MAINTAINER`. Product roles should use the
same diagnostic commands, but they should route infrastructure changes through
`MAINTAINER` instead of editing coordination state directly.

## CLI-Only Discipline

All mutations under `.greatminds/` go through the `greatminds` CLI. Do not
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

## Fleet Launch And Environments

Run the ACP daemon in the project environment:

```bash
greatminds coordd --project-dir "$PWD"
```

Alternatively, install the optional user service once, then start it:

```bash
greatminds daemon install --project-dir "$PWD"
greatminds daemon start --project-dir "$PWD"
```

`greatminds launch --target tmux` opens daemon/operator surfaces; the VS Code
and Cursor IDE targets generate ACP workspace tasks. Interactive roles use
`greatminds chat`, backed by daemon-owned sessions. Configure role bindings in
`coordination/execution.yaml` before dispatch.

Foreground and service entrypoints apply configured environment layers in this
order: inherited process environment, `.greatminds/PROJECT.env`, then the selected
registration's captured environment. A foreground project with one registration
uses that registration; multiple registrations require `coordd --project NAME`.
An unregistered project uses no captured environment. Both entrypoints validate
file syntax before starting daemon work. Changes take effect on restart, not
mid-session. The inherited shell and systemd base environments may still differ.

Use a stable install for long-running fleets: pipx, `uv tool`, or a normal
project venv. The editable development venv is for changing and testing
greatminds itself. Do not launch a long-running fleet with `uv run`;
`uv run --active` can write editable paths from a task worktree into the active
environment and leave broken `.pth` files after the worktree is deleted.

When developing greatminds locally, use an isolated development venv:

```bash
unset VIRTUAL_ENV
uv venv .venv
uv pip install --python .venv/bin/python -e '.[docs]'
.venv/bin/greatminds --help
```

After upgrading the package used by the fleet, refresh the running processes:

```bash
greatminds update
greatminds agent status
```

## ACP configuration and authentication checks

```bash
greatminds daemon doctor --project-dir "$PWD" --json
greatminds run status --project-dir "$PWD"
```

Doctor checks the execution configuration, declared environment references and
executable availability without launching agents. It does not validate provider
credentials or establish live ACP compatibility. Run status shows durable
`waiting_auth`, `waiting_input`, failure and dispatch reasons.

Authenticate using the configured harness's supported login method as the OS
user that runs the daemon. Then restart the installed service to refresh its
declared environment, or restart the foreground daemon. Resolve the affected
run explicitly with `greatminds run retry RUN_ID` when authentication is ready;
missing authentication does not trigger an unbounded retry loop. Keep credentials
out of task files, manifests and diagnostic reports.

`greatminds wake-check` and `greatminds wake-check --json` inspect the same
maintenance gates used by the daemon, even when an execution file is absent.
`ready` requires terminal dependencies and task readiness; `wrong_terminal`
identifies a dependency that ended in another terminal queue. `waiting`, `cycle`,
`live_role_hold` and `gate_failed` explain why the daemon cannot resume a task.
Inspection does not move tasks; the daemon applies only authorized system resumes.

## Court Fix

A court fix is a small, direct correction to coordination mechanics or docs
needed to unblock the fleet. Keep it narrow:

1. Confirm the problem with `greatminds watchdog`, `greatminds agent status`,
   `greatminds wake-check`, or `greatminds gate-check`.
2. Change only the affected canon, CLI, role doc, or project config.
3. Run the closest validation command.
4. Let active runs finish or explicitly cancel them, then restart the daemon
   when execution configuration or code changes require it.
5. Tell `ARCHITECT-PLANNER` when product work must be replanned.

Do not use a court fix to skip queue ownership or review gates.

## Venv Recovery

If agents fail to import `greatminds`, check which venv the live processes use:

```bash
greatminds agent status
```

If the fleet environment contains stale editable paths from a deleted worktree,
recreate or repair that environment, reinstall the intended package version,
then restart the daemon and agents from it. Avoid `uv run` during recovery;
call the target environment's `greatminds` binary directly.

## Inspect Stand Lease Deploy

Stand work is lease-backed. A holder requests a lease with:

```bash
greatminds stand lease --task TASK_ID --worktree "$(greatminds worktree path TASK_ID)" --profile full-deploy
```

`coordd` handles stand-state changes and deploys the active lease profile. If
the lease stays in `preparing`, inspect:

```bash
greatminds stand status
greatminds watchdog
```

If the stand is down after an infrastructure fix, an operator with global
stand control runs:

```bash
greatminds stand up --reason "recovered ..."
```

The holder releases the lease after product probes:

```bash
greatminds stand release --lease-id LEASE_ID --result pass
```

## Stuck Driven Turns

Driven roles do not keep a persistent agent process. `coordd` creates one turn
per event and holds `.greatminds/.locks/driven-{role}.lock` for the duration
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
`.greatminds/.locks/driven-{role}.retry.json`. The live scheduler uses this
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

## Aggregate local diagnosis

```bash
greatminds run doctor --project-dir "$PWD"
greatminds run doctor --project-dir "$PWD" --json
```

This read-only report combines schema/mirror inspection, ACP prerequisites,
current run holds, assignment admission, dependency findings and unresolved
command/result/maintenance/deployment operations. It reuses the runtime services;
it does not run a harness, inspect provider login by launching a probe, repair
state or call systemctl. `daemon doctor` remains the focused prerequisite check.

JSON version 1 includes `checks`, `findings`, `summary` and `status`. Each finding
has a component, stable code, severity, selected evidence and an operator action.
A failed component does not hide independent results. `unavailable` means a
prerequisite prevented that check, not that the component is healthy. The command
exits 1 when error findings exist; warnings/info alone exit 0. Inspect the summary
and findings rather than treating exit 0 as proof of a running healthy daemon.

Authentication waits, semantic dependency holds, retry delays and uncertain
operation recovery remain separate findings. Use the suggested detailed command
before authorizing an operation. Repeating diagnosis does not consume model turns
or alter project files. Reports omit prompt text, command arguments/output and
raw exception messages; they retain task/run identifiers and diagnostic codes.

This report covers the named local checks. It is a best-effort observation across
stores, not an atomic health verdict or live compatibility test. Watchdog-specific
stale-task/intent/worktree scans and remote provider availability are not implied
by a `no_findings` result.
