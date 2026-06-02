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

## Start here

Install the package, bootstrap a project, start the daemon, then launch the
agent windows:

```bash
pip install greatminds
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup --session myproject
greatminds daemon install
greatminds daemon start
greatminds launch --target tmux
tmux a -t myproject
```

Next: [Installation](getting-started/installation.md) and
[First Project](getting-started/first-project.md).
