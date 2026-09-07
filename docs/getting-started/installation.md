# Installation

The managed daemon requires Python 3.11 or newer on Linux, with a readable
`/proc` filesystem and working pidfd operations. Process identity and restart
cleanup use these Linux interfaces. Native macOS and Windows daemon execution
are not supported; a Linux environment must satisfy the same requirements.
Harness compatibility is tracked separately from host support.

For a uv-managed project, use a separate tool environment. This also supports
working on projects whose Python version is below 3.11:

```bash
uv tool install --python 3.13 greatminds
greatminds --help
```

If the executable is not on PATH, run `uv tool update-shell` and open a new
terminal. Greatminds does not need to be a project dependency.

Alternatively, install Greatminds in a dedicated Python 3.11+ environment:

```bash
python -m pip install greatminds
greatminds --help
```

An ACP agent or adapter must be installed and authenticated separately. See the
[compatibility matrix](../architecture/acp-compatibility.md) for tested versions
and the [first project guide](first-project.md) for configuration and a first
conversation. Package installation alone does not establish provider access.

## Optional components

- `tmux` for `greatminds launch --target tmux`.
- A systemd user manager on Linux for `greatminds daemon install` and service
  integration using explicit `--systemd` controls. Normal daemon and web commands
  do not require a service manager. `greatminds coordd --foreground` is available
  for terminal debugging.
- The `stands` extra for Ansible-backed YAML stand profiles:

```bash
python -m pip install 'greatminds[stands]'
```

Local ACP conversations and queue dispatch do not require Ansible. Installing
this extra does not configure or deploy a stand.

## Development and documentation

Inside a source checkout:

```bash
python -m pip install -e .
```

To build the documentation:

```bash
python -m pip install -e '.[docs]'
mkdocs build --strict
```

## Project initialization

The wheel includes the CLI, ACP runtime, schema and package data. Run
`greatminds setup` in your project to create runtime queues under `.greatminds/`,
a schema mirror and an empty `coordination/execution.yaml`. Setup preserves an
existing execution contract and task data. Configure ACP manifests and role
bindings in that execution file before dispatching work.

Setup does not install harnesses, copy provider credentials, generate native
Codex profiles or Claude plugins, or install a systemd service. Optional stand
profiles must be selected and configured explicitly.
