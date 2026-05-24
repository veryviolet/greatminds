# Installation

greatminds is a Python package with one console entry point:

```bash
pip install greatminds
greatminds --help
```

For development or documentation work inside a checkout, install the project in
editable mode:

```bash
pip install -e .
```

To build this documentation site locally, install the docs extra:

```bash
pip install -e '.[docs]'
mkdocs build --strict
```

## Requirements

- Python 3.11 or newer.
- A POSIX-like environment for the filesystem queue model.
- `tmux` when using `greatminds launch --target tmux`.
- `systemctl --user` when using `greatminds daemon` to supervise `coordd`.

## Package contents

The wheel ships the CLI, coordination schema, role prompts, queue templates,
Codex profiles, Claude Code plugins, and systemd user unit template. A project
does not need to vendor the canon files by hand; `greatminds setup` copies the
current package data into the project.
