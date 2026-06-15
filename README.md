# greatminds

<p align="center">
File-based multi-agent coordination for agent fleets and task pipelines.
</p>

<p align="center">
  <a href="https://pypi.org/project/greatminds/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/greatminds.svg"></a>
  <a href="https://pypi.org/project/greatminds/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/greatminds.svg"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/pypi/l/greatminds.svg"></a>
  <a href="https://veryviolet.github.io/greatminds/"><img alt="Docs" src="https://img.shields.io/badge/docs-github_pages-blue.svg"></a>
</p>

greatminds runs a fleet of Claude Code and OpenAI Codex agents on a shared
filesystem-based finite state machine. Tasks flow through queues such as
`feature_inbox/`, `feature_plan/`, `feature_dev/`, `feature_test/`, and
`verified/`; a small `coordd` daemon nudges agents when input appears. There is
no central broker and no database. Per-project setup writes `coord.yaml`;
the per-user daemon can supervise multiple projects on one machine.

## Quickstart

```bash
# install
pip install greatminds  # or: uv add greatminds

# bootstrap a project
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup --session myproject
```

greatminds supports two primary agent tools:

- `claude`: Claude Code. Used for chat, loop, and driven roles. Setup writes
  `.claude/settings.local.json` and installs configured Claude marketplace
  plugins for Claude-hosted roles.
- `codex`: OpenAI Codex. Used for chat and driven roles. Run `codex login`
  once for the machine account; generated files under
  `coordination/.codex-home/<role>/` are role config sources, not auth homes.

Role-to-tool assignment lives in `coord.yaml`. Edit it after setup when you
want different tools for different roles:

| Role | Default tool | Default mode |
| --- | --- | --- |
| `ARCHITECT-PLANNER` | `codex` | `chat` |
| `MAINTAINER` | `claude` | `loop` |
| `LIVE-DEVELOPER` | `claude` | `staged` |
| `ARCHITECT-REVIEWER` | `codex` | `driven` |
| `DEVELOPER` | `claude` | `driven` |
| `UI-DEVELOPER` | `claude` | `driven` |
| `TECHNICAL-WRITER` | `codex` | `driven` |
| `TESTER` | `claude` | `driven` |
| `READER` | `claude` | `driven` |
| `EXPLORER` | `codex` | `driven` |

```yaml
session: myproject
project_dir: /tmp/greatminds-demo
windows:
  - name: planner
    role: ARCHITECT-PLANNER
    tool: codex
    mode: chat
  - name: maintainer
    role: MAINTAINER
    tool: claude
    mode: loop
  - name: dev
    role: DEVELOPER
    tool: claude
    mode: driven
  - name: reviewer
    role: ARCHITECT-REVIEWER
    tool: codex
    mode: driven
```

Put project and stand variables in `coordination/PROJECT.env`. It is
gitignored, sourced before agent launch, and passed to stand profiles as
Ansible extra vars:

```bash
cat > coordination/PROJECT.env <<'EOF'
STAND_HOST=localhost
STAND_USER=violet
EOF
```

`STAND_HOST=localhost` is enough for a local smoke stand. For a remote stand,
set `STAND_HOST` to an SSH config alias or a comma-separated list of aliases,
and set `STAND_USER` to the remote account whose PATH should be used.

A stand is a singleton live environment. Agents lease it with
`greatminds stand lease`; `coordd` deploys the leased worktree by running an
Ansible stand profile from `coordination/stand-profiles/<profile>.yaml`.
The default allowed profiles are `full-deploy`, `vite-dev`, and `smoke-only`.
A minimal smoke profile looks like this:

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

Then start the fleet:

```bash

# install the per-project daemon
greatminds daemon install
greatminds daemon start

# launch agents
greatminds launch --target tmux
tmux a -t myproject
```

The windows defined in `coord.yaml` boot inside one tmux session; each role
starts in chat, loop, staged, or driven mode according to that file. Driven
roles do not keep live panes; `coordd` starts one Claude or Codex turn when
their queue, inbox, or stand-state event changes.

## Key Concepts

- **Queues**: [`feature_inbox/`, `feature_plan/`, `feature_dev/`, `verified/`](https://veryviolet.github.io/greatminds/concepts/queues/) - task state is its directory.
- **Roles**: [`ARCHITECT-PLANNER`, `DEVELOPER`, `TESTER`, and others](https://veryviolet.github.io/greatminds/concepts/roles/) - each role owns queues and a heartbeat file.
- **Scenarios A/B/C**: [standard pipeline, intensive review, and UI rapid iteration](https://veryviolet.github.io/greatminds/concepts/scenarios/).
- **Stand**: [lease-backed Ansible deployment profiles](https://veryviolet.github.io/greatminds/concepts/stand-operations/) prepare the singleton live environment.
- **Stand gate**: [stand-required tasks](https://veryviolet.github.io/greatminds/concepts/stand-gate/) need lease-backed live-stand evidence before review.
- **Inbox**: [per-role mailbox](https://veryviolet.github.io/greatminds/concepts/inbox/) for `ask`, `info`, and `wake` messages without moving tasks.

## Documentation

Full documentation: <https://veryviolet.github.io/greatminds/>

## Where to File Issues

```text
Bugs in greatminds: https://github.com/veryviolet/greatminds/issues
Bugs in a project you use greatminds in: that project's issue tracker.
```

## License

Apache-2.0. See [LICENSE](LICENSE).
