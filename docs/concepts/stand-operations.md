# Stand Operations

The stand is a singleton live environment shared by the agent fleet. Agents
coordinate access through a lease-backed state file.

## State File

The source of truth is:

```text
coordination/.stand/state.yaml
```

`greatminds stand status` reads that file and prints the current state, active
lease, pending FIFO queue, and recent transition history. Use the CLI output as
the source of truth; do not inspect or edit `state.yaml` directly.

The stand has four states:

| State | Meaning |
| --- | --- |
| `free` | No active lease. The next lease can be granted immediately. |
| `preparing` | `coordd` is deploying or restarting the stand for the active lease. |
| `ready` | The stand is ready for the lease holder to run probes. |
| `down` | Stand operation is paused because deploy or infrastructure recovery is needed. |

Each active lease carries a `lease_id`, task id, worktree path, profile,
holder role, grant timestamp, optional ready timestamp, and TTL. Pending
leases wait in FIFO order in the same state file.

## Lease Flow

The role that needs a live stand requests a lease:

```bash
greatminds stand lease --task <task-id> --worktree <path> --profile <profile>
```

`--worktree` must point at the task's isolated worktree under
`<project>/.worktrees/`, not the main fleet checkout. The usual form is:

```bash
greatminds stand lease \
  --task <task-id> \
  --worktree "$(greatminds worktree path <task-id>)" \
  --profile full-deploy
```

If the stand is `free`, the lease becomes active and the state moves to
`preparing`. If another lease is active, the new lease is appended to the FIFO
queue. The command prints the `lease_id`; the requester must keep that token.

`coordd` watches `coordination/.stand/state.yaml` and runs the active lease
profile. Operators can inspect progress with:

```bash
greatminds stand status
```

When state is `preparing`, `coordd` deploys from the lease worktree using the
lease profile. On success it records a deploy marker and transitions the
stand to ready, equivalent to:

```bash
greatminds stand ready --lease-id <lease-id>
```

That moves the state to `ready` and files an inbox-info message to the holder:
`stand lease <lease-id> ready; task=<task-id>`. The ready transition is valid
only after the configured stand profile has run and left its deploy marker.

The holder, usually `TESTER` or `EXPLORER`, then runs its own probes against
the prepared stand and releases the lease:

```bash
greatminds stand release --lease-id <lease-id> --result pass|fail|partial
```

Only the lease holder can release an active lease. Releasing moves the stand
back to `free`; `coordd` promotes the next queued lease on a later tick.

## TTL And Recovery

Leases have a default TTL from `coordination/schema.yaml`
(`stand.resource.lease`), currently
four hours, with a warning window before automatic release. The TTL is a
safety valve for abandoned leases; it is not a substitute for explicitly
releasing a lease after probes finish.

If deployment fails or the stand has an infrastructure incident,
the operator or maintainer runs:

```bash
greatminds stand down --reason "<operational reason>"
```

`down` pauses queue processing. After recovery:

```bash
greatminds stand up --reason "<recovery note>"
```

That returns the stand to `free` so queued work can resume.

## Evidence Boundary

The coordd deploy step only proves infrastructure readiness: process status, health
endpoint availability, remote reachability, GPU availability when requested,
and equivalent bring-up checks. It does not run product acceptance tests.

Functional verification belongs to the lease holder. For stand-required tasks,
`TESTER` records the lease evidence in the product task's `tests` block,
including `stand_evidence.lease_id`, result, commit, worktree fingerprint, and
its own `functional_probes` plus `stand_evidence.tester_observations`.
`greatminds gate-check <task-id>` reads that tests-block evidence first.

Use the lease commands for all stand operations. Stand readiness belongs in
`coordination/.stand/state.yaml`, and product validation evidence belongs in
the task's `tests` block.
