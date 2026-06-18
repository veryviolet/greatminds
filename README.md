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

greatminds runs a fleet of coding agents on a shared filesystem-based finite
state machine. Tasks flow through queues such as
`feature_inbox/`, `feature_plan/`, `feature_dev/`, `feature_test/`, and
`verified/`; a small `coordd` daemon nudges agents when input appears. There is
no central broker and no database. Per-project setup writes editable project
configuration under `coordination/` and runtime/system state under
`.greatminds/`; the per-user daemon can supervise multiple projects on one
machine.

## Quickstart

```bash
# install
pip install greatminds  # or: uv add greatminds

# bootstrap a project
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup --session myproject
```

List supported agent tools and their execution modes:

```bash
greatminds agent tools
greatminds agent tools --json
```

The packaged adapters support:

- `claude`: Claude Code. Used for chat, loop, and driven roles. Setup writes
  `.claude/settings.local.json` and installs configured Claude marketplace
  plugins for Claude-hosted roles.
- `codex`: OpenAI Codex. Used for chat and driven roles. Run `codex login`
  once for the machine account; generated files under
  `.greatminds/.codex-home/{role}/` are role config sources, not auth homes.
- `cursor`: Cursor agent. Used for chat/loop panes and one-shot driven turns.
- `cline`: Cline CLI. Used for chat/loop panes and one-shot driven turns.
- `gemini`: Gemini CLI. Used for chat/loop panes and one-shot driven turns.
- `openhands`: OpenHands CLI. Used for chat panes and one-shot driven turns;
  configure OpenHands/LiteLLM credentials on the machine before using it.

Window modes in `coordination/coord.yaml`:

- `chat`: a live tmux pane for an operator-facing conversation.
- `loop`: a resident watchdog pane that wakes on its own timer.
- `staged`: a tmux pane with the start command pre-typed, so the operator
  starts that role manually when needed.
- `driven`: no live pane; `coordd` starts one driven turn when work lands in
  the role's queue, inbox, or stand event stream. Claude and Codex use
  stateful drivers; Cursor, Cline, Gemini, and OpenHands use one-shot
  headless subprocess drivers.

Role-to-tool assignment lives in `coordination/coord.yaml`. Edit it after
setup when you want different tools for different roles:

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

Put machine-local project and stand variables in `.greatminds/PROJECT.env`.
It is gitignored, sourced before agent launch, and passed to stand profiles as
Ansible extra vars:

```bash
cat > .greatminds/PROJECT.env <<'EOF'
STAND_HOST=localhost
STAND_USER=violet
EOF
```

`STAND_HOST=localhost` is enough for a local smoke stand. For a remote stand,
set `STAND_HOST` to an SSH config alias or a comma-separated list of aliases,
and set `STAND_USER` to the remote account whose PATH should be used.

A stand is a singleton live environment. Agents lease it with
`greatminds stand lease`; `coordd` deploys the leased worktree by running an
Ansible playbook selected through `coordination/stand-profiles.yaml`. There is
no global current profile. The current profile is chosen for each lease with
`greatminds stand lease --profile {profile}`, stored in
`.greatminds/.stand/state.yaml` as `active_lease.profile`, and resolved through
the registry to a YAML file under `coordination/stand-profiles/`.

Setup seeds reference profiles named `full-deploy`, `vite-dev`, and
`smoke-only`. Add project profiles by adding registry entries and YAML files.
The `used_for` and `default_for` values are machine-readable tokens from
the packaged schema copied to `.greatminds/schema.yaml` under
`stand_profile_registry`; `default_for` tells roles which profile to choose for
common lease purposes:

```yaml
profiles:
  full-deploy:
    file: full-deploy.yaml
    purpose: Full deployed product validation on a stand.
    environment: stand
    used_for: [tester_validation, explorer_review, reviewer_validation]
    default_for: [feature_test, explorer, reviewer]
```

Inspect and validate the registry with:

```bash
greatminds stand profiles list
greatminds stand profiles doctor
```

For production, create an explicit registry entry with
`environment: production`, `requires_explicit_user_approval: true`, and
`allowed_roles` such as `[ARCHITECT-REVIEWER, MAINTAINER]`. Lease it only after
approval:

```bash
greatminds stand lease \
  --task TASK_ID \
  --profile production \
  --profile-approval USER_APPROVED
```

A minimal smoke playbook looks like this:

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

# launch agents in tmux
greatminds launch --target tmux
tmux a -t myproject

# or generate a VS Code workspace and cockpit tasks
greatminds launch --target vscode
```

The VS Code target writes `.vscode/tasks.json` and a `.code-workspace` file
with agent terminals plus operator tasks for dashboard, driven logs, coordd,
agent status, agent tools, and stand status. The repository also ships a
`vscode-extension/` cockpit that calls the `greatminds` CLI as its backend.

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
