# greatminds

A local workspace for a team of coding agents. Talk to a planner or developer,
follow batch work, review tool permissions, and manage test stands from your
browser. A separate daemon coordinates the work and keeps running when you
close the UI.

Greatminds connects executors through **ACP**. OpenAI Codex, Claude Code and Grok have tested
integration paths. Roles are independent of executors: choose a harness, model,
reasoning level and permission policy for each role. Other ACP implementations
can be configured; see the [compatibility matrix](https://veryviolet.github.io/greatminds/architecture/acp-compatibility/)
for verified scope and limitations.

The daemon handles deterministic work: queue assignment, process lifecycle,
validation commands, evidence checks and workflow transitions. Agents handle
reasoning. Configuration, tasks and execution records live in your filesystem.

![Greatminds web workspace with Summary and Kanban controls](https://veryviolet.github.io/greatminds/assets/workspace-summary.jpg)

*Greatminds 3.3.0, dark theme. Screenshots use a demonstration project.*

## Start in your existing project

Greatminds needs **Linux and Python 3.11+**. Your project can keep Python 3.8,
another Python version, or a different language. Install Greatminds separately
with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install --python 3.13 greatminds
cd /path/to/your/project
greatminds setup
greatminds web --port 8765 --no-daemon
```

Open `http://127.0.0.1:8765`. In **Settings**, add your installed ACP executor and
an interactive **ARCHITECT-PLANNER** role. Save settings, click **Start daemon**,
and open **Planner** to begin a conversation.

You need an installed and authenticated harness before the first conversation.
The [step-by-step setup guide](https://veryviolet.github.io/greatminds/getting-started/uv-project/)
includes exact Codex installation commands, the settings values to enter, and
validation commands that use your project's own Python. Setup preserves your
project's `pyproject.toml`, `uv.lock`, `.python-version` and existing environment.

On subsequent visits:

```bash
cd /path/to/your/project
greatminds web --port 8765
```

This starts or attaches to the project daemon. Ctrl+C stops the web server;
agent work continues. Stop it explicitly with `greatminds daemon stop` or the
web toolbar. Each project has its own daemon and state; choose a different web
port when opening multiple projects. Systemd is optional.

## In the web workspace

- Interactive role tabs with Markdown, streamed responses and activity indicators.
  **Enter** sends; **Shift+Enter** inserts a new line.
- A dashboard with **Summary / Kanban** views and clickable task cards.
- Batch-run history with messages, tools, validation results and permission requests.
- Executor settings with model and reasoning selectors when supplied through ACP.
- **Stands** with machine access, inventories, Ansible playbooks and operation logs.
- Light and dark themes, **Compact / Normal / Large** interface sizes, and English,
  Russian and Simplified Chinese. Display preferences persist in your browser.
- The running web server's version directly below the product name.

The server binds to loopback by default and has no application authentication.
For remote access, place it behind your own access controls. See the
[web workspace guide](https://veryviolet.github.io/greatminds/getting-started/local-web/).

## Workflows and stands

Add queue-scheduled developer, tester and reviewer roles to run a task pipeline.
An agent response alone does not complete a task: the daemon checks typed results,
revision, permissions and evidence before applying transitions.

Ansible support is optional:

```bash
uv tool install --python 3.13 'greatminds[stands]'
```

Configure stand profiles and deployment policy before executing stand operations.
No LLM stand-keeper is required for these deterministic operations. See the
[execution contract](https://veryviolet.github.io/greatminds/architecture/execution-contract/)
for roles, workspaces, commands, permissions and deployment gates.

CLI, tmux and VS Code workflows are also available. Start with `greatminds --help`
and the [documentation](https://veryviolet.github.io/greatminds/).

## Updates and development

```bash
uv tool upgrade greatminds
```

Restart your web server to load the new version. Restart the daemon when runtime
changes need to take effect, after checking active work. See the
[upgrade guide](https://veryviolet.github.io/greatminds/getting-started/upgrading/)
and [release checklist](https://veryviolet.github.io/greatminds/recipes/cutting-a-release/).

Report Greatminds defects in the [issue tracker](https://github.com/veryviolet/greatminds/issues).
Bugs in a managed project belong in that project's tracker.

Apache-2.0. See [LICENSE](https://github.com/veryviolet/greatminds/blob/main/LICENSE).
