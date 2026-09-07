# First local project

For a uv-managed project, install Greatminds as an isolated tool with its own
Python. Your project's Python version and dependencies stay separate:

```bash
uv tool install --python 3.13 greatminds
cd /path/to/your/project
greatminds setup
greatminds web --port 8765 --no-daemon
```

Open `http://127.0.0.1:8765`. If `greatminds` is not on your shell's PATH, run
`uv tool update-shell` and open a new terminal. You can also run each command
without a persistent tool installation using `uvx --python 3.13 greatminds`,
for example `uvx --python 3.13 greatminds setup`.

This works when the project itself requires Python 3.8: Greatminds runs on 3.13.
Do not add Greatminds to the project's dependencies. See the
[uv/Python 3.8 walkthrough](uv-project.md) for project checks and browser setup.

Setup creates an empty `coordination/execution.yaml` and runtime queues under
`.greatminds/`. Repeating it preserves your configuration and task data. Harness
installation, provider login and optional service installation are separate steps.
Setup does not install vendor plugins, rewrite git hooks or widen permissions.

For the browser workflow, continue with the [step-by-step setup](uv-project.md#4-configure-the-first-conversation).
The remaining sections describe the CLI workflow.

## Configure one ACP harness

Install an ACP harness or upstream adapter and complete its authentication using
that tool's supported login method. Consult the
[compatibility matrix](../architecture/acp-compatibility.md) for tested versions
and limitations. A configured executable is not proof of working authentication
or protocol compatibility.

Edit `coordination/execution.yaml` using your actual executable and version
labels. The placeholders below must be replaced before starting a conversation:

```yaml
version: 1
agents:
  local-agent:
    transport: acp
    argv: [/absolute/path/to/your-acp-agent]
    adapter_version: your-tested-adapter-version
    harness_version: your-tested-harness-version
bindings: {}
```

Select a roster before starting the daemon:

```bash
greatminds project presets
greatminds project preset local --agent local-agent
greatminds project preset local --agent local-agent --apply
```

The first preset command previews the complete contract without writing it.
`--apply` atomically fills empty bindings; repeating the same selection is safe.
Existing custom bindings are never replaced. Edit them explicitly if changing an
established roster, then restart the daemon to load the changed contract.

| Preset | Roles |
| --- | --- |
| `local` | Planner, developer, tester, reviewer |
| `ui` | Planner, UI developer, tester, reviewer |
| `docs` | Planner, technical writer, reader, reviewer |
| `deployed` | Planner, code/UI developers, tester, reviewer, live developer, explorer |
| `full` | All ten roles, including maintainer |

Planner, live developer and maintainer are on demand. Queue workers start only
when eligible work exists. All generated bindings use `permission: ask` and the
named manifest. Presets preserve commands and other execution settings; they do
not install harnesses or provision stands. Configure validation commands for your
project and optional stand policy separately. Local tasks explicitly declare
`plan.stand_required: false`; TESTER requests daemon commands and independently
assesses the implementation and evidence. Tasks requiring a stand retain their
actual deployed validation gates. The schema and transition rules are unchanged
by preset selection.

One manifest can serve multiple role bindings. Roles, model selection,
permissions, workspace, scheduling and account limits are configured separately.
Use `scheduling: queue` for a role that should receive eligible queued work.
The current schema retains each role's review and evidence gates; one configured
harness does not imply that one role can approve every workflow stage.

Validate the configuration without starting an agent:

```bash
greatminds project execution
greatminds daemon doctor --project-dir "$PWD" --json
```

Doctor checks configuration, declared environment references and executable
availability. Provider login and an actual ACP turn still need verification.
See the [execution contract](../architecture/execution-contract.md) for model,
mode, environment reference and capability requirements. Store secrets outside
the execution manifest and task files.

## Start the daemon and a conversation

In one terminal:

```bash
greatminds coordd --project-dir "$PWD"
```

In another terminal in the same project:

```bash
greatminds chat create planner
# Replace CONVERSATION_ID with the returned ID:
greatminds chat talk CONVERSATION_ID
```

The daemon owns the ACP session. The terminal displays streamed responses and
explicit permission choices. Detaching leaves the turn running; attaching again
does not resubmit your message. Use `chat interrupt CONVERSATION_ID REQUEST_ID` to request
cancellation or `chat close CONVERSATION_ID` to close admission and clean up the
conversation. For an existing workflow task, use
`chat create BINDING_ID --task TASK_ID` to bind its exact revision.

Tmux and IDE frontends are optional:

```bash
greatminds launch --target tmux
# Or generate ACP workspace tasks:
greatminds launch --target vscode
```

On Linux, you can use an optional systemd user service instead of the foreground
daemon:

```bash
greatminds daemon install --name my-project --project-dir "$PWD"
greatminds daemon start --project my-project
```

Use a single daemon owner for the project. See the
[operations runbook](../operations/runbook.md) for environment setup, updates and
recovery.

## Observe work and diagnose holds

```bash
greatminds run status
greatminds run events --follow
greatminds wake-check --json
greatminds watchdog
```

Run status explains assigned tasks, execution state, authentication/input waits
and dispatch holds. `run pause` stops new dispatch while existing work may finish;
`run resume` restores dispatch. `run cancel RUN_ID` requests bounded cancellation.
An explicit `run retry RUN_ID` authorizes a further attempt after you resolve its
cause; it does not make uncertain command effects disappear.

The daemon resumes tasks only when declared terminal dependencies, task readiness
and live-role gates allow it. ACP prompt completion alone does not verify a task.
Agents submit typed results; the domain service validates the assigned identity,
revision, decision and evidence before applying a workflow transition.

## Add deployed validation only when needed

Local ACP operation needs neither Ansible nor a remote stand. For YAML stand
profiles, install the optional dependency in the Greatminds environment:

```bash
python -m pip install 'greatminds[stands]'
```

This installs the validated Ansible range. It does not configure hosts, create
profiles or authorize deployment. Configure project profiles under
`coordination/stand-profiles/`, register them in `coordination/stand-profiles.yaml`,
and declare the authorized stand policy in `coordination/execution.yaml`.

```bash
greatminds stand profiles list
greatminds stand profiles doctor
```

Tasks declaring stand requirements still need valid lease and deployment
evidence. Omitting Ansible does not bypass those gates. See the
[execution contract](../architecture/execution-contract.md) for the deployment
ledger, evidence freshness and recovery of uncertain operations.

The [local preset acceptance scenario](../architecture/acp-compatibility.md#one-harness-local-preset-on-2026-09-07)
records a complete small code task with one real ACP harness, separate developer,
tester and reviewer runs, configured daemon checks and automatic merge. Its plan
was supplied as fixture input; it does not replace planning and test-design judgment
for your own project.
