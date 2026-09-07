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

Start the independent ACP daemon from the project directory:

```bash
greatminds coordd --project-dir "$PWD"
```

The command returns after startup; closing the terminal does not stop the daemon.
Use `greatminds daemon status`, `stop`, or `restart` in the project. For logs,
read `.greatminds/.runtime/daemon.log`. See [daemon lifecycle](daemon-lifecycle.md).

Alternatively, install the optional systemd user service once, then start it:

```bash
greatminds daemon install --project-dir "$PWD"
greatminds daemon start --systemd --project-dir "$PWD"
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

## Inspecting ACP Runs

The daemon records queue assignments and conversation runs in
`.greatminds/.runtime/state.json`, including process identity, protocol outcome,
timing and pending controls. Conversation sessions may stay open for further
operator input. A process or a lock alone does not prove useful task progress.

Use:

```bash
greatminds dashboard --once
greatminds run status
greatminds run doctor --json
```

Inspect the run's outcome and domain receipts before retrying. Uncertain commands,
deployments and task mutations require their scoped recovery actions. Cancellation
uses `greatminds run cancel RUN_ID`; the daemon verifies and cleans tracked process
groups before releasing the run.

## Retries and Account Admission

Startup retries require evidence that no prompt or earlier protocol activity
started. They have a finite attempt budget and shared account backoff. Post-prompt
errors do not automatically replay work. `greatminds run status` reports task and
account admission separately; an explicit task retry cannot bypass an account hold.

For adapters with documented application-specific ACP error codes, a manifest may
configure `account_limit_errors` (see [manifest reference](../concepts/codex-profiles.md#explicit-account-limit-signals)).
`account_rate_limit` blocks new claims and subsequent conversation prompts until
the configured cooldown expires. `account_quota_exhausted` requires operator
resolution. Existing in-flight prompts may finish; no provider/model is switched.
All bindings sharing the account label in this project use the same durable hold.
Other projects and differently labeled accounts are independent.

Inspect `accounts` and `account_holds` in `run status`, or use the exact scoped
action emitted by `run doctor`. After independently checking account recovery:

```bash
greatminds run account-resume ACCOUNT --hold-id HOLD_ID --reason "recovery checked"
```

The hold identity prevents an older action from clearing a newer signal. Repeating
the same resolution is idempotent. Resume does not replay failed tasks or user
turns; decide separately whether to retry the task or enqueue a new message.
Removing a manifest rule or restarting the daemon does not erase a recorded hold.
Rate-limit expiry opens admission under normal concurrency limits; it does not
prove the provider is available. Generic errors and message text never create
provider quota state.

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
stores, not an atomic health verdict or live compatibility test. Stale-task, orphan-intent and orphan-worktree scans use the same functions as
`watchdog`. Remote provider availability is not implied by a `no_findings` result.

## Local diagnostic bundle

```bash
greatminds run doctor --project-dir "$PWD" --bundle /tmp/greatminds-diagnostics.json
```

The command writes a private JSON file locally; it performs no upload. Combine
`--json` with `--bundle` to retain the ordinary machine-readable doctor report
on stdout. Diagnostic errors still produce a bundle when collection is possible,
and the command keeps doctor's exit status. Use a new destination filename for
each export: existing files and symlinks are never replaced. The parent directory
must exist. Publication is atomic and the file has mode `0600`.

The versioned bundle contains installed Greatminds/ACP SDK/Python versions,
configuration fingerprints and binding limits, selected diagnostic findings,
recent run states/timings/metrics and event headers. Raw configuration, environment
values, executable arguments, task bodies, prompts, command output, result
payloads and event bodies are excluded. Task, run, binding and operation identifiers
become salted pseudonyms consistent within one bundle; each export uses a fresh
salt. The salt/mapping is not exported. Fingerprints, timestamps and states remain
useful diagnostic metadata; this is not a guarantee that a project is unidentifiable.

Defaults retain up to 100 recent runs, 200 recent events and 200 findings, with
errors prioritized. Summary counts cover the full report. `limits` and `truncated`
make the selected windows explicit; an unreadable runtime has unknown truncation
values. Total serialized export is limited to 5 MiB. Collection records
unavailable components rather than copying malformed raw files. Missing metrics
stay null. Version labels describe installed libraries, not a live harness probe.

The bundle and doctor inspection are best-effort observations across stores.
Collecting a bundle does not pause work, authorize retry or resolve uncertain
side effects. It includes the named diagnostic checks, not a copy of all project
history or a proof of live provider compatibility.

Filesystem findings use the effective schema's queue kinds and watchdog thresholds.
Parking queues preserve task/worktree association; terminal queues do not hide
orphan candidates unless worktree policy explicitly retains verified/archived
task worktrees. A finding is a reason to inspect, not permission to delete a
worktree or intent. Unreadable directories yield an unavailable/failed component
instead of an empty healthy result. Files that disappear during a concurrent
transition are skipped; repeated observation can see the new location.

## Explicit recovery actions

Applicable `run doctor` findings include `recovery_actions` with an argv array,
project environment/cwd, effect, preconditions and required options. The text
view prints project-scoped command suggestions. These descriptions do not execute
repairs. A frontend must preserve the project environment and require the stated
operator input; command services revalidate their preconditions when invoked.
No action automatically clears run credentials to gain operator authority.

- Uncertain commands: inspect `run command-status REQUEST_ID`, then use
  `run command-resolve REQUEST_ID --reason EXPLANATION` after inspecting effects.
  Resolution does not rerun a command or create passing command evidence.
- System maintenance: `run repair --operation OPERATION_ID` requests daemon
  reconciliation with revision/dependency/gate rechecks. `--abandon --reason`
  cancels only an unresolved, uncommitted intent whose source remains a regular
  file and destination does not exist. It preserves task files.
- Deployment attempts: inspect `stand deployment-status`; use
  `stand deployment-recover ATTEMPT_ID` for tracked process cleanup when needed.
  `stand deployment-resolve ATTEMPT_ID --reason EXPLANATION` requires confirmed
  cleanup and no live tracked group, and records an operator assessment. It does
  not deploy or mark the stand ready. Exclusive deployment locking still applies.

Repeating a completed resolution/abandonment with the same explanation returns
the original receipt without adding another event. A different explanation is
rejected rather than rewriting evidence. A repeated requested maintenance repair
returns the existing prepared/applied receipt; if reconciliation fails again,
a new explicit request can ask the daemon to recheck corrected conditions.

Mutating recovery controls reject either `GREATMINDS_RUN_ID` or
`GREATMINDS_RUN_TOKEN`: a partial agent credential is not operator context.
Other role and deployment controls remain in force. For findings without a
supported bounded action, inspect the relevant evidence instead of deleting
state or synthesizing a successful receipt. Diagnostic exports omit executable
recovery descriptors containing local project paths and raw operation IDs.
