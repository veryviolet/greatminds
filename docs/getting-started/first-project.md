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

## Claude local settings

During setup, greatminds writes or extends
`.claude/settings.local.json`. New files include the Stop hook,
`autoMode.allow: ["$defaults"]`, and the canonical
`permissions.allow` entries from `schema.yaml` under
`claude_settings.permissions.allow`.

Those allow rules let unattended Claude loop roles perform the git operations
they are authorized to run, such as reviewer commit, tag, push, merge, branch,
checkout, and worktree commands. Without explicit allow rules, Claude Code can
pause on an approval prompt that a loop-mode role cannot answer.

Project operators can add their own `permissions.allow` entries directly in
`.claude/settings.local.json`. Re-running `greatminds setup` unions the schema
defaults into the existing list, deduplicates them, and preserves operator-added
rules. For a valid existing file, setup leaves other top-level settings such as
custom hooks and `autoMode` untouched.

## Claude marketplace plugins

During setup, greatminds installs curated Claude marketplace plugins for each
Claude-hosted role from `schema.yaml` under `plugins.claude_marketplace`.
For example, the shipped schema can assign plugins such as `playwright`,
`sentry`, `postman`, or `sourcegraph` to the roles that use them.

The install step runs the equivalent of `claude plugin install <name>` for each
plugin assigned to that role. It is idempotent: plugins already present in
`claude plugin list` are preserved, failed installs are reported in the setup
summary, and setup continues with the remaining plugins.

To change the curated set for a project, edit `schema.yaml`:

```yaml
plugins:
  claude_marketplace:
    DEVELOPER: [postman]
    UI-DEVELOPER: [playwright, chrome-devtools-mcp, postman]
    TESTER: [playwright, sentry, postman, codspeed]
```

Keep empty lists for roles that should not receive marketplace plugins. Codex
marketplace lists are currently empty by design; Codex roles use generated
per-role `CODEX_HOME` configs instead of Claude marketplace plugin installs.

After setup, verify the installed Claude plugins with:

```bash
claude plugin list
```

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
