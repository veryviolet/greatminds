# First Project

Create or enter a repository, then run setup:

```bash
cd /path/to/project
greatminds setup --session myproject
```

`setup` creates the coordination directories, writes `coord.yaml`, installs
local agent configuration files, and copies the canon project templates. It does
not overwrite an existing `coord.yaml`; edit that file when you want different
tools, windows, or launch modes.

## Start the daemon

The daemon is the process that watches inboxes and pushes wake text into idle
agents:

```bash
greatminds daemon install
greatminds daemon start
greatminds daemon status
```

The daemon instance name is derived from `coord.yaml: session`. That lets one
user run several projects on the same machine without a single global service
name colliding.

## Launch the fleet

```bash
greatminds launch --target tmux
tmux a -t myproject
```

Each window in `coord.yaml` starts the configured role, tool, and mode. Chat
roles wait for user input; loop roles poll their inbox and owned queues.

## File your first task

The normal product path starts with user feedback or an inbox task, then flows
through planning, implementation, test or reader review, and final review:

```bash
greatminds task new \
  --stream product \
  --kind feature \
  --scope backend \
  --title "Add a small feature"
```

From there, the planner owns triage and routing. Implementers do not claim from
`feature_plan/`; they claim only from their own queues.
