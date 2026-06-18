# First Project

Create or enter a repository, then run setup:

```bash
cd /path/to/project
greatminds setup --session myproject
```

`setup` creates editable project configuration under `coordination/`, runtime
state under `.greatminds/`, and local agent configuration files. It does not
overwrite an existing `coordination/coord.yaml`; edit that file when you want
different tools, windows, or launch modes.

## Agent tools and role mapping

List the installed tool capabilities at any time:

```bash
greatminds agent tools
greatminds agent tools --json
```

The packaged adapters support these tools:

| Tool | Use it for | Setup requirement |
| --- | --- | --- |
| `claude` | Claude Code chat, loop, and driven roles. | Install Claude Code, authenticate it for the OS user that runs the fleet, and keep the `claude` binary reachable from daemon shells. |
| `codex` | OpenAI Codex chat and driven roles. | Run `codex login` once for the machine account; optionally set `GREATMINDS_CODEX_HOME` when the login is not under `~/.codex`. |
| `cursor` | Cursor agent chat/loop panes and one-shot driven roles. | Install Cursor CLI / `cursor-agent`, authenticate it for the OS user, and keep it on `PATH`. Greatminds runs `cursor-agent` through a `systemd-run --user` scope in `cursor.slice` by default; tune `GREATMINDS_CURSOR_MEM_HIGH`, `GREATMINDS_CURSOR_MEM_MAX`, `GREATMINDS_CURSOR_CPU`, or `GREATMINDS_CURSOR_SLICE` when needed. |
| `cline` | Cline CLI chat/loop panes and one-shot driven roles. | Install and configure Cline CLI for the OS user that runs the fleet. |
| `gemini` | Gemini CLI chat/loop panes and one-shot driven roles. | Install Gemini CLI, authenticate/configure it for the OS user, and keep `gemini` on `PATH`. |
| `openhands` | OpenHands CLI chat panes and one-shot driven roles. | Install OpenHands CLI and configure its LLM/runtime environment before assigning driven roles. |

Window modes in `coordination/coord.yaml`:

| Mode | Meaning |
| --- | --- |
| `chat` | A live tmux pane for an operator-facing conversation. |
| `loop` | A resident watchdog pane that wakes on its own timer. |
| `staged` | A tmux pane with the start command pre-typed; the operator starts it manually when needed. |
| `driven` | No live pane; `coordd` starts one driven turn when work lands in the role's queue, inbox, or stand event stream. Claude and Codex use stateful drivers; Cursor, Cline, Gemini, and OpenHands use one-shot headless subprocess drivers. |

Role-to-tool assignment lives in `coordination/coord.yaml`. The default
template mixes Claude and Codex roles, but it is ordinary project config.
Change `tool:` per role, then restart the daemon and launch session:

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

`mode: chat` and `mode: loop` create live panes. `mode: driven` creates no
agent pane; `coordd` starts one driven turn when queue, inbox, or stand events
arrive.

## Claude local settings

During setup, greatminds writes or extends
`.claude/settings.local.json`. New files include the Stop hook,
`autoMode.allow: ["$defaults"]`, and the canonical
`permissions.allow` entries from the packaged schema under
`claude_settings.permissions.allow`.

Those allow rules let unattended Claude roles perform the git operations they
are authorized to run, such as reviewer commit, tag, push, merge, branch,
checkout, and worktree commands. Without explicit allow rules, Claude Code can
pause on an approval prompt that a driven or self-loop role cannot answer.

Project operators can add their own `permissions.allow` entries directly in
`.claude/settings.local.json`. Re-running `greatminds setup` unions the schema
defaults into the existing list, deduplicates them, and preserves operator-added
rules. For a valid existing file, setup leaves other top-level settings such as
custom hooks and `autoMode` untouched.

## Claude marketplace plugins

During setup, greatminds installs curated Claude marketplace plugins for each
Claude-hosted role from the packaged schema under `plugins.claude_marketplace`.
For example, the shipped schema can assign plugins such as `playwright`,
`sentry`, `postman`, or `sourcegraph` to the roles that use them.

The install step runs the equivalent of `claude plugin install <name>` for each
plugin assigned to that role. It is idempotent: plugins already present in
`claude plugin list` are preserved, failed installs are reported in the setup
summary, and setup continues with the remaining plugins.

Setup resolves the `claude` binary from `PATH` first, then checks common npm
install locations: `~/.local/bin/claude`, `~/.npm-global/bin/claude`, and
`/usr/local/bin/claude`. This covers non-login shells, SSH launches, and daemon
contexts where the interactive shell profile that adds npm binaries to `PATH`
has not been loaded.

If setup reports `claude binary not found in PATH or common locations`, plugin
installation is skipped for the affected roles and the plugin names are counted
as failed. Add Claude Code to `PATH`, or install it with
`npm install -g @anthropic-ai/claude-code`, then run `greatminds setup` again.

The setup summary separates marketplace plugin results into `installed`,
`pre-existing`, `dedupe-this-run`, and `failed`. `pre-existing` means the plugin
was already present before setup started; `dedupe-this-run` means another role
installed it earlier in the same setup run. Failed installs include the plugin
name in the summary, and setup also prints the first stderr line from the
underlying `claude plugin install` command.

To add project-local Claude plugins, create role files under
`coordination/plugins.local/`. Setup merges those files with the packaged
defaults:

```yaml
# coordination/plugins.local/tester.yaml
claude_marketplace: [playwright, sentry, postman, codspeed]
```

Keep empty lists for roles that should not receive marketplace plugins. Codex
marketplace lists are currently empty by design; Codex roles use generated
per-role profile sources plus the single machine Codex login instead of Claude
marketplace plugin installs. See
[Codex Profiles](../concepts/codex-profiles.md) for the generated layout and
launch path.

After setup, verify the installed Claude plugins with:

```bash
claude plugin list
```

## Project environment

`.greatminds/PROJECT.env` is the minimal place for machine-local
project-specific values. It is gitignored, loaded before each agent starts, and
passed to stand profiles as Ansible extra vars.

For a local smoke stand:

```bash
cat > .greatminds/PROJECT.env <<'EOF'
STAND_HOST=localhost
STAND_USER=violet
EOF
```

For a remote stand, make `STAND_HOST` an SSH config alias or comma-separated
aliases, and set `STAND_USER` to the remote account whose PATH should be used
by the stand playbook:

```bash
cat > .greatminds/PROJECT.env <<'EOF'
STAND_HOST=app-stand
STAND_USER=deploy
EOF
```

Add product-specific values there as well, such as service URLs, ports,
database names, deploy paths, or feature flags. Reference them in
`coordination/PROJECT.md`, MCP configs, skills, and stand profiles.

## Minimal stand profile

A stand is the live environment that agents lease for deployed validation.
The lease does not deploy by itself; `coordd` prepares the active lease by
running an Ansible YAML profile from `coordination/stand-profiles/`.

Setup copies reference profiles:

- `smoke-only`: reachability probe, useful first.
- `full-deploy`: rsync and install pattern for backend-style deployment.
- `vite-dev`: backend plus a Vite dev server for live UI iteration.

Setup also writes `coordination/stand-profiles.yaml`, the project-owned
registry of allowed profile names. A lease selects a registry key:

```bash
greatminds stand lease --task <task-id> --profile full-deploy
```

The selected key is stored in `.greatminds/.stand/state.yaml` as
`active_lease.profile`.
The registry entry chooses the YAML file to run:

```yaml
profiles:
  full-deploy:
    file: full-deploy.yaml
    purpose: Full deployed product validation on a stand.
    environment: stand
    used_for: [tester_validation, explorer_review, reviewer_validation]
    default_for: [feature_test, explorer, reviewer]
```

`used_for` describes what the profile can safely serve. `default_for` maps
common role intents to one registry key; each `default_for` token must be
claimed by at most one profile. The allowed tokens are defined in
the packaged schema copied to `.greatminds/schema.yaml` under
`stand_profile_registry`.

Inspect and validate the registry after edits:

```bash
greatminds stand profiles list
greatminds stand profiles doctor
```

To add a profile, add a registry entry and a matching YAML file under
`coordination/stand-profiles/`. For production, add an explicit production
entry with `environment: production`, `requires_explicit_user_approval: true`,
and an `allowed_roles` list such as `[ARCHITECT-REVIEWER, MAINTAINER]`.
Production leases must include an approval marker after the user has approved
that lease:

```bash
greatminds stand lease \
  --task <task-id> \
  --profile production \
  --profile-approval USER_APPROVED
```

The smallest useful `coordination/stand-profiles/smoke-only.yaml` is:

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

When a task needs live validation, the holder leases a profile:

```bash
greatminds stand lease \
  --task <task-id> \
  --worktree "$(greatminds worktree path <task-id>)" \
  --profile smoke-only
```

Use [Stand Operations](../concepts/stand-operations.md) for the complete lease,
profile, and evidence flow.

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

Each window in `coord.yaml` starts the configured role, tool, and mode. Planner
is the user-facing interactive role. MAINTAINER runs as a self-loop watchdog.
Worker roles are driven by `coordd`: their panes stay idle between turns, and
coordd starts one turn when their inbox, queue, or stand-state events change.

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
