# greatminds

Greatminds coordinates coding agents through ACP. A local daemon assigns work,
owns sessions, supervises processes, runs configured commands and applies validated
workflow transitions. Agents perform reasoning and submit typed results. Task
files, events and evidence remain inspectable on the local filesystem.

## Start a project

```bash
python -m pip install greatminds
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup
```

Setup creates an empty `coordination/execution.yaml` and runtime queues under
`.greatminds/`. It preserves existing project data and configuration. Install and
authenticate your chosen ACP harness separately, then add a manifest and role
binding. The [first-project guide](getting-started/first-project.md) walks through
configuration and a daemon-owned conversation.

```bash
greatminds project execution
greatminds daemon doctor --json
greatminds coordd
```

Doctor validates configuration and executable/environment prerequisites without
starting an agent. It does not prove provider authentication or live protocol
support. The [compatibility matrix](architecture/acp-compatibility.md) records
verified and unverified harness scenarios.

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
