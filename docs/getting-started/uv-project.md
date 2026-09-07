# Start in an existing uv project

Greatminds and your project use independent Python environments. This walkthrough
applies to a project pinned to Python 3.8; Greatminds uses Python 3.13.

## 1. Install Greatminds once

```bash
uv tool install --python 3.13 greatminds
```

This installs the CLI and daemon in uv's tool environment, outside your project's
`.venv`. It does not add Greatminds to `pyproject.toml` or `uv.lock`, or change
`.python-version`. If the command is not found, use `uv tool update-shell` and
open a new terminal. You can stay in your activated project environment.

## 2. Install an ACP executor once

For a first setup, Codex is one tested option. You need Node.js and npm for its
ACP adapter; they are separate from the Python environment. The acceptance runs
used Node.js 22.13.0. Install the tested adapter and CLI outside the repository:

```bash
npm install --prefix "$HOME/.local/share/greatminds/acp" --save-exact \
  @agentclientprotocol/codex-acp@1.10.0 @openai/codex@0.153.4
"$HOME/.local/share/greatminds/acp/node_modules/.bin/codex" --version
"$HOME/.local/share/greatminds/acp/node_modules/.bin/codex" login status
```

If login status reports that you are not authenticated, run the same Codex
executable with `login` and complete its sign-in flow. Existing Codex credentials
are used by the adapter. Greatminds does not copy them into the project.

Print the launch command you will paste into the web settings:

```bash
printf '["%s/.local/share/greatminds/acp/node_modules/.bin/codex-acp"]\n' "$HOME"
```

Copy the printed JSON array, including brackets and quotes. The settings form
expects an argument array, not a shell command; it does not expand `$HOME` or `~`.
For this installation, the adapter version is `1.10.0` and the harness version is
`0.153.4`. If you install different versions, use their actual values.

Claude and Grok are also supported through ACP; see the
[compatibility guide](../architecture/acp-compatibility.md). You only need one
executor to begin, and can add or change executors per role later.

## 3. Open the project

From the repository you want agents to work on:

```bash
cd /path/to/your/project
greatminds setup
greatminds web --port 8765 --no-daemon
```

Open `http://127.0.0.1:8765`. Setup creates `coordination/execution.yaml`, runtime
queues under `.greatminds/`, and runtime/worktree ignore rules. Repeating setup
preserves configuration and task data. The web command keeps running in the
terminal; Ctrl+C closes it. Change `--port` for another local project.

## 4. Configure the first conversation

The current web UI uses Russian labels; the descriptions below use their English
meanings.

1. Open **Settings** (the gear button) in the web toolbar.
2. Choose **Codex**, enter the name `codex`, paste the JSON launch command from
   step 2, enter adapter version `1.10.0` and harness version `0.153.4`, then click
   the add button. This stages the manifest; it is not saved yet.
3. Add the **ARCHITECT-PLANNER** role, bound to that executor. Keep interactive
   scheduling and permission policy **Ask**.
4. Click **Save settings**, then **Start daemon** in the toolbar.
5. Open the **Planner** tab, create a conversation and send your first message.
   For example: "Read the project and its AGENTS.md. Explain its structure and
   suggest a work plan. Do not change files yet."

Leave model and mode blank to use the harness defaults. Tool permission requests
appear in the UI when needed. A saved configuration does not verify provider
access: the first response confirms the actual model connection.

Optional CLI checks, from another terminal in the project:

```bash
greatminds project execution
greatminds daemon doctor --project-dir "$PWD" --json
```

These check configuration and executable availability without prompting a model.

The planner tab is enough for a first conversation. To enable the normal batch
pipeline, add DEVELOPER, TESTER and ARCHITECT-REVIEWER with queue scheduling,
or select the `local` preset through the CLI after saving an agent manifest:

```bash
greatminds project preset local --agent YOUR_AGENT_NAME --apply
```

The preset fills empty bindings; use the settings form for a roster already
containing a planner. Restart the daemon after saving execution settings.

## 5. Run checks with the project's Python

Greatminds uses its own Python for orchestration. Project validation commands
must invoke the project's tools. For a Python 3.8 project with pytest installed
in its normal uv dependency groups, add this mapping to the execution YAML
through the full configuration editor in **Settings**:

```yaml
commands:
  tests:
    argv: [uv, run, --locked, --python, '3.8', python, -m, pytest]
    roles: [DEVELOPER, TESTER, ARCHITECT-REVIEWER]
    timeout_seconds: 600
```

Merge it with existing commands instead of replacing them. If pytest is only
installed manually in your current `.venv`, fresh worktrees will not inherit it.
For a Python 3.8 project, you can instead use an isolated test-runner overlay:

```yaml
    argv: [uv, run, --locked, --python, '3.8', --with, 'pytest==8.3.5', python, -m, pytest]
```

This does not add pytest to the project manifest or lockfile. It can download the
test runner and synchronize the underlying environment from the existing lock.
The command is subject to the daemon's command authorization policy; declaring
it does not automatically authorize execution. If the project uses
unittest, replace `pytest` with `unittest, discover, -v` in the argument list.
For a custom uv group or another test runner, use the same command you normally
use for this project. The command starts in the assigned workspace, so uv selects
that workspace's project environment. `--locked` rejects a stale lockfile instead
of updating it; uv can synchronize its environment and install locked dependencies.

Keep the project's `.python-version` and `uv.lock` under version control. Queue
work uses task worktrees, and those need the same environment definition as the
main checkout. Git-based validation evidence requires a Git repository.

The separation has been exercised with an isolated Greatminds 3.0.0 uv tool on
Python 3.13 and a uv project pinned to 3.8: project tests verify Python 3.8 and
that Greatminds is absent from the project environment. Daemon command execution
uses the project interpreter even when VIRTUAL_ENV points at the tool environment.

## 6. Resume on another day

Setup and harness installation are one-time steps. Start from the same project:

```bash
cd /path/to/your/project
greatminds web --port 8765
```

This opens the saved configuration and starts or attaches to the project's daemon.
Select an existing conversation in its role tab to continue. Keep the terminal
running. Ctrl+C stops the web server and a daemon it owns; project history remains.

If the port is occupied, use another one, such as `--port 8766`, and open the
printed URL. Each project has its own server, port and runtime state.

## What changes in your repository

- `coordination/execution.yaml` stores executors, roles and validation commands.
- `.greatminds/` stores runtime queues, conversations and execution records.
- `.gitignore` receives rules for `.greatminds/` and `.worktrees/`.
- `pyproject.toml`, `uv.lock`, `.python-version` and the existing project environment
  are not modified by `greatminds setup`.

Review `git status` after setup. Configuration may contain machine-specific
executable paths; adapt them when sharing it with another host. Greatminds itself
stays in uv's tool environment. Starting the UI does not migrate the project's
Python or install Greatminds into its `.venv`.
