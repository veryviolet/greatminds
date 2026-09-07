# greatminds

Greatminds coordinates coding agents through ACP. A local daemon owns task
assignment, sessions, process cleanup, validation commands, and workflow
transitions. Agents perform work that needs reasoning and submit typed results.
Task files and evidence remain on the filesystem; there is no database service.

The [local web workspace](docs/getting-started/local-web.md) provides role tabs,
dashboards, settings, and batch-run inspection without application authentication.
Start it with `greatminds web --project-dir /path/to/project --port 8765`.

## Quickstart

The managed daemon runs on Linux with Python 3.11+, `/proc` and pidfd support.
See the [host requirements](docs/getting-started/installation.md).

```bash
pip install greatminds  # or install in your preferred Python environment
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup
```

Setup creates `coordination/execution.yaml` with empty `agents` and `bindings`,
and the queue tree under `.greatminds/`. It does not install harnesses, change
authentication, install plugins or services, or choose a provider for you.
Repeated setup preserves your execution contract and task data.

Local ACP workflows do not require Ansible or a deployed stand. If you enable
YAML stand profiles, install the optional dependency in the same environment:

```bash
python -m pip install 'greatminds[stands]'
```

This adds the validated Ansible version range; it does not configure hosts or
authorize deployment. Required stand/evidence gates still apply to tasks that
declare them.

Add an ACP agent and a role binding to `coordination/execution.yaml`. This is a
configuration example: replace the executable path and version labels with your
installed adapter and harness, and complete that harness's authentication first.

```yaml
version: 1
agents:
  local-agent:
    transport: acp
    argv: [/absolute/path/to/your-acp-agent]
    adapter_version: your-tested-adapter-version
    harness_version: your-tested-harness-version
bindings:
  planner:
    agent: local-agent
    role: ARCHITECT-PLANNER
    scheduling: on-demand
    permission: ask
```

Role, harness, model, permissions, workspace and scheduling are independent.
Use `scheduling: queue` for automatic task assignment. Additional bindings can
use the same agent manifest or a different ACP implementation. See the
[execution contract](docs/architecture/execution-contract.md) for command,
worktree, model, account and stand policies.

An existing explicit contract can be supplied during initialization:

```bash
greatminds setup --project-dir /path/to/project --execution-config /path/to/execution.yaml
```

Start the daemon in one terminal:

```bash
greatminds coordd
```

On Linux with a systemd user manager, service installation is optional:

```bash
greatminds daemon install --name my-project
greatminds daemon start --project my-project
```

The registry associates the service name with the project directory. Without an
explicit name, installation uses an existing registered name or the directory
name. Names accept ASCII letters, digits, dots, underscores and hyphens (1–80
characters, starting with a letter or digit). Use an explicit name for directories
containing spaces or when another project has the same basename. Existing names
cannot be redirected to a different project. No `coord.yaml` is required.

The service uses a generic PATH containing the launcher directory, standard
system directories and user bin directories. For additional runtime locations,
set PATH in `.greatminds/PROJECT.env`, or declare an agent/command `environment`
reference for PATH in `execution.yaml`. Service installation does not run login
shells or discover vendor-specific executables.

`greatminds update` refreshes the ACP project and only already-installed services.
It uses `try-restart`, so inactive services stay inactive. It never installs a
service or opens a frontend. Restart a foreground daemon manually after updating.
`update --project NAME` selects the registered project even from another directory.

In another terminal, create an operator conversation with the configured binding:

```bash
greatminds chat create planner
# Use the conversation ID returned above:
greatminds chat talk CONVERSATION_ID
```

The daemon owns the ACP session. Detaching leaves the work running; reconnecting
does not replay completed prompts. `talk` displays streamed text and explicit
one-time permission choices. `chat close CONVERSATION_ID` requests cleanup.
Task-bound conversations use `chat create BINDING --task TASK_ID`.

```bash
greatminds run status
greatminds run events --follow
greatminds run pause
greatminds run resume
greatminds run cancel RUN_ID
greatminds run retry RUN_ID
```

## Local frontends

```bash
greatminds launch --target tmux
greatminds launch --target vscode
```

Tmux hosts the daemon and an operator shell. The VS Code target writes
`greatminds-acp.code-workspace` with process tasks for the daemon and operator
commands, preserving unrelated workspace settings. Neither frontend launches
native harness TUIs. The [VS Code extension](vscode-extension/README.md) supplies
New ACP Chat, Attach ACP Chat and Close ACP Chat commands.

## Harness compatibility

Claude Code and OpenAI Codex use upstream ACP adapters. Other harnesses may expose
ACP directly or through a bridge. Protocol initialization alone does not establish
working authentication, session continuity, model selection or tool permissions.
The [compatibility matrix](docs/architecture/acp-compatibility.md) distinguishes
verified scenarios from untested support for Codex, Claude, Grok, Qwen, Kimi,
Cline, Gemini, OpenHands and Cursor. The same client handles supported harnesses;
there is no fallback to a native transport.

## Tasks, evidence and stands

Task queues live under `.greatminds/`; editable project configuration lives under
`coordination/`. The installed schema is authoritative and is pinned to each run.
Successful ACP output alone never completes a workflow transition: the daemon
validates typed results, task revision, permissions and evidence first.

Configured validation commands run through the daemon with bounded output and
recorded receipts. Optional stand deployment uses an explicit policy in the
execution contract and profiles in `coordination/stand-profiles.yaml` and
`coordination/stand-profiles/`. Ansible-backed profiles remain available; setup
does not enable deployment or seed a deployment policy automatically.

```bash
greatminds stand profiles list
greatminds stand profiles doctor
```

## Development status

The primary setup, launch and daemon entrypoints use ACP. The modernization
acceptance covers Codex, Claude and Grok; see the
[acceptance record](docs/architecture/modernization-acceptance.md) for scope and
reproducible evidence. The [local web workspace](docs/getting-started/local-web.md)
is included in the 3.0 release. See the
[upgrade guide](docs/getting-started/upgrading.md) when moving from 2.x.

## Issues and license

Report Greatminds defects in the [project issue tracker](https://github.com/veryviolet/greatminds/issues).
Bugs in a project managed by Greatminds belong in that project's tracker.

Apache-2.0. See [LICENSE](LICENSE).
