# greatminds

greatminds coordinates a fleet of coding agents through a filesystem state
machine. A task's directory is its state, a move between directories is the
handoff, and appended blocks in the task file record the work that happened.

Use it when several agents need to share one repository without a database or a
central queue service. The default setup includes planning, implementation,
testing, documentation review, final review, stand evidence, inbox messages,
heartbeats, and a launcher for tmux-based fleets.

## What you get

- A generated `coord.yaml` that defines the agent windows, roles, tools, and
  modes for one project.
- A `coordination/` tree with queues such as `feature_inbox/`,
  `feature_plan/`, `feature_dev/`, `feature_docs/`, `feature_test/`,
  `feature_review/`, and `verified/`.
- A `greatminds` CLI for task moves, inbox messages, stand leases, role
  rendering, journal views, agent diagnostics, watchdog checks, and fleet
  launch.
- A per-project daemon that watches inboxes, queue files, and stand state,
  then wakes or drives the owning role.

## Quickstart

Install the package and bootstrap a project:

```bash
pip install greatminds
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup --session myproject
```

greatminds uses two primary agent tools:

- `claude`: Claude Code, including project-local settings and configured
  Claude marketplace plugins.
- `codex`: OpenAI Codex, using the single machine Codex login plus generated
  per-role profile sources under `coordination/.codex-home/<role>/`.

Window modes in `coord.yaml`:

- `chat`: a live tmux pane for an operator-facing conversation.
- `loop`: a resident watchdog pane that wakes on its own timer.
- `staged`: a tmux pane with the start command pre-typed, so the operator
  starts that role manually when needed.
- `driven`: no live pane; `coordd` starts one Claude or Codex turn when work
  lands in the role's queue, inbox, or stand event stream.

Choose which tool runs each role in `coord.yaml`:

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
windows:
  - name: planner
    role: ARCHITECT-PLANNER
    tool: codex
    mode: chat
  - name: dev
    role: DEVELOPER
    tool: claude
    mode: driven
  - name: reviewer
    role: ARCHITECT-REVIEWER
    tool: codex
    mode: driven
```

Put local project and stand variables in `coordination/PROJECT.env`:

```bash
cat > coordination/PROJECT.env <<'EOF'
STAND_HOST=localhost
STAND_USER=violet
EOF
```

The stand is a singleton live environment. `coordd` prepares it by running the
Ansible playbook selected by `coordination/stand-profiles.yaml` for the active
lease. Setup seeds reference profiles (`smoke-only`, `full-deploy`,
`vite-dev`), and projects add more entries to the registry when they need
additional stands such as production.

Start the daemon and launch the tmux fleet:

```bash
greatminds daemon install
greatminds daemon start
greatminds launch --target tmux
tmux a -t myproject
```

Next: [Installation](getting-started/installation.md) and
[First Project](getting-started/first-project.md).
