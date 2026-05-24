# greatminds

<p align="center">
File-based multi-agent coordination for agent fleets and task pipelines.
</p>

<p align="center">
  <a href="https://pypi.org/project/greatminds/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/greatminds.svg"></a>
  <a href="https://pypi.org/project/greatminds/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/greatminds.svg"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/pypi/l/greatminds.svg"></a>
  <a href="https://veryviolet.github.io/greatminds/"><img alt="Docs" src="https://img.shields.io/badge/docs-github_pages-blue.svg"></a>
</p>

greatminds runs a fleet of Claude, Codex, or Cursor agents on a shared
filesystem-based finite state machine. Tasks flow through queues such as
`feature_inbox/`, `feature_plan/`, `feature_dev/`, `feature_test/`, and
`verified/`; a small `coordd` daemon nudges agents when input appears. There is
no central broker and no database. Per-project setup writes `coord.yaml`;
the per-user daemon can supervise multiple projects on one machine.

## Quickstart

```bash
# install
pip install greatminds  # or: uv add greatminds

# bootstrap a project
mkdir -p /tmp/greatminds-demo
cd /tmp/greatminds-demo
greatminds setup --session myproject

# install the per-project daemon
greatminds daemon install
greatminds daemon start

# launch agents
greatminds launch --target tmux
tmux a -t myproject
```

The windows defined in `coord.yaml` boot inside one tmux session; each role
starts in chat or loop mode according to that file.

## Key Concepts

- **Queues**: [`feature_inbox/`, `feature_plan/`, `feature_dev/`, `verified/`](https://veryviolet.github.io/greatminds/concepts/queues/) - task state is its directory.
- **Roles**: [`ARCHITECT-PLANNER`, `DEVELOPER`, `TESTER`, and others](https://veryviolet.github.io/greatminds/concepts/roles/) - each role owns queues and a heartbeat file.
- **Scenarios A/B/C**: [standard pipeline, intensive review, and UI rapid iteration](https://veryviolet.github.io/greatminds/concepts/scenarios/).
- **Stand gate**: [stand-required tasks](https://veryviolet.github.io/greatminds/concepts/stand-gate/) need `STAND-KEEPER` evidence before review.
- **Inbox**: [per-role mailbox](https://veryviolet.github.io/greatminds/concepts/inbox/) for `ask`, `info`, and `wake` messages without moving tasks.

## Documentation

Full documentation: <https://veryviolet.github.io/greatminds/>

## Where to File Issues

```text
Bugs in greatminds: https://github.com/veryviolet/greatminds/issues
Bugs in a project you use greatminds in: that project's issue tracker.
```

## License

Apache-2.0. See [LICENSE](LICENSE).
