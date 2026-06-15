# Stand Operations

The stand is a singleton live environment shared by the agent fleet. Agents
coordinate access through a lease-backed state file, and `coordd` prepares the
active lease by running an Ansible YAML profile.

A stand can be local (`STAND_HOST=localhost`) or remote (`STAND_HOST` as an SSH
config alias or comma-separated aliases). The profile decides what "deploy"
means for the project: a smoke probe, an rsync install, a Docker Compose
restart, a Vite dev server, or any other Ansible-native deployment sequence.
greatminds owns the lease/state/evidence protocol; the project owns the
playbook content.

## Project Environment

Put machine-local stand values in `.greatminds/PROJECT.env`:

```bash
STAND_HOST=localhost
STAND_USER=violet
```

`PROJECT.env` is sourced before agents start and passed to stand profiles as
Ansible extra vars. In a profile, `STAND_HOST` is readable as
`{{ STAND_HOST }}`, `STAND_USER` as `{{ STAND_USER }}`, and any custom key the
project adds is available the same way.

For a remote stand:

```bash
STAND_HOST=app-stand
STAND_USER=deploy
```

`app-stand` should resolve through the OS user's SSH config, known hosts, and
keys. Multi-node stands can use a comma list such as
`STAND_HOST=web-a,web-b`.

## Ansible Profiles

The project profile registry lives at:

```text
coordination/stand-profiles.yaml
```

Stand playbooks live under:

```text
coordination/stand-profiles/
```

The lease `--profile` value selects a registry key, not a raw filename. The
registry maps that key to a YAML playbook file:

```yaml
profiles:
  smoke-only:
    file: smoke-only.yaml
    purpose: Fast reachability and readiness check.
    environment: stand
    used_for: [deploy_only, warmup, quick_readiness]
    default_for: [verify_only]
```

For example, `--profile smoke-only` records `active_lease.profile:
smoke-only` and runs the registry entry's `file`, usually
`coordination/stand-profiles/smoke-only.yaml`. The same profile name can point
at a differently named file, such as `file: smoke.yaml`, when a project wants
that convention.

`used_for` is the capability list for the profile. `default_for` assigns common
role intents such as `feature_test`, `explorer`, `reviewer`,
`live_developer`, `production_deploy`, and `production_review` to a profile.
The vocabulary is defined in the packaged schema copied to
`.greatminds/schema.yaml` under `stand_profile_registry`. Each `default_for`
token can belong to only one profile, so automated role selection is
unambiguous.

Use the registry commands after edits:

```bash
greatminds stand profiles list
greatminds stand profiles doctor
```

`coordd` runs the profile with Ansible and injects:

- every key from `.greatminds/PROJECT.env`;
- the active lease id as `lease_id`;
- the task id as `task_id`;
- the isolated task worktree path as `worktree`.

A minimal smoke profile:

```yaml
---
- name: register stand node
  hosts: localhost
  gather_facts: false
  tasks:
    - name: add configured stand host
      ansible.builtin.add_host:
        name: "{{ STAND_HOST | default('localhost') }}"
        groups: stand_nodes
        ansible_connection: >-
          {{ 'local' if (STAND_HOST | default('localhost')) == 'localhost'
             else 'ssh' }}

- name: smoke stand
  hosts: stand_nodes
  gather_facts: false
  tasks:
    - name: remote shell works
      ansible.builtin.command: /bin/true
      changed_when: false
```

For a deploy profile, add tasks that copy or build from `{{ worktree }}` and
then run readiness probes. The shipped `full-deploy.yaml`, `vite-dev.yaml`, and
`smoke-only.yaml` files are reference profiles, not hidden deployment logic.
Register additional project profiles in `coordination/stand-profiles.yaml`.

For a production deployment or post-deploy review, create a profile with:

```yaml
profiles:
  production:
    file: production.yaml
    purpose: Production deployment and post-deploy verification.
    environment: production
    requires_explicit_user_approval: true
    allowed_roles: [ARCHITECT-REVIEWER, MAINTAINER]
    used_for: [production_deploy, production_post_deploy_review]
    default_for: [production_deploy, production_review]
```

The CLI enforces `allowed_roles` and requires an explicit approval marker:

```bash
greatminds stand lease \
  --task <task-id> \
  --profile production \
  --profile-approval USER_APPROVED
```

## State File

The source of truth is:

```text
.greatminds/.stand/state.yaml
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

Each active lease carries a `lease_id`, task id, worktree path, profile name,
profile file, holder role, grant timestamp, optional ready timestamp, and TTL.
Pending leases wait in FIFO order in the same state file.

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

`coordd` watches `.greatminds/.stand/state.yaml` and runs the active lease
profile file selected by `active_lease.profile`. Operators can inspect progress
with:

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

Leases have a default TTL from the packaged schema copied to
`.greatminds/schema.yaml` (`stand.resource.lease`), currently four hours, with
a warning window before automatic release. The TTL is a
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
`.greatminds/.stand/state.yaml`, and product validation evidence belongs in
the task's `tests` block.
