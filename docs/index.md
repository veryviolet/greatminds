# greatminds

Greatminds coordinates coding agents through ACP. A local daemon assigns work,
owns sessions, supervises processes, runs configured commands and applies validated
workflow transitions. Agents perform reasoning and submit typed results. Task
files, events and evidence remain inspectable on the local filesystem.

## Start a project

Install Greatminds outside your project's Python environment:

```bash
uv tool install --python 3.13 greatminds
cd /path/to/your/project
greatminds setup
greatminds web --port 8765 --no-daemon
```

Open `http://127.0.0.1:8765`, configure an installed ACP executor and an interactive
planner in **Settings**, then start the daemon from the toolbar. Follow the
[existing-project setup guide](getting-started/uv-project.md) for executor
installation, authentication and exact settings, including projects on Python 3.8.

The [web workspace](getting-started/local-web.md) provides role tabs, Markdown chat,
Summary/Kanban dashboards, batch traces, executor settings and Ansible stand
operations. Choose a theme, interface size and English, Russian or Chinese.
The daemon continues working after the web server stops.

The [first-project guide](getting-started/first-project.md) also covers direct
configuration and CLI conversations. The [compatibility matrix](architecture/acp-compatibility.md)
records verified and unverified harness scenarios.

## Work and observe

In a second terminal, create a conversation using a configured binding:

```bash
greatminds chat create planner
# Use the returned conversation ID:
greatminds chat talk CONVERSATION_ID
```

For automatic work, configure queue-scheduled role bindings. The daemon claims
concrete task revisions and compiles their context. Completion requires valid
typed results and applicable evidence; a finished ACP prompt is not task approval.
Role, harness, model, workspace, permission policy and account are separate
configuration choices.

```bash
greatminds run status
greatminds run events --follow
greatminds wake-check --json
greatminds watchdog
```

Tmux, VS Code workspace tasks and a systemd user service are optional operator
surfaces. Local ACP operation does not require Ansible or a deployed stand.
Install `greatminds[stands]` only for Ansible-backed profiles, and configure their
explicit deployment policy. Tasks declaring stand requirements retain their
lease and evidence gates.

See the [execution contract](architecture/execution-contract.md),
[daemon architecture](architecture/daemon-and-agents.md),
[CLI reference](cli-reference/index.md) and
[operations runbook](operations/runbook.md).
