# Upgrading

Upgrade the package in the environment that runs your fleet, then restart the
daemon and agents so long-running processes import the new code.

```bash
pip install --upgrade greatminds
greatminds daemon restart
```

If your project uses a pinned virtual environment, run the upgrade inside that
environment. If the fleet is launched from tmux, restart the agent processes
after the package upgrade so their registries, pty sockets, and role prompts
match the installed version.

## Update notifications

When `coordd` is running, it periodically checks PyPI for a newer greatminds
release. The interval and target live in `schema.yaml` under `auto_update`;
the default interval is `14400` seconds, and the default target is
`MAINTAINER`.

The current mode is `notify_only`. If PyPI has a newer version, `coordd` sends
one inbox `info` message to MAINTAINER with the installed version, latest
version, release link, and the command to run. It does not upgrade the fleet by
itself. MAINTAINER chooses the timing and runs `greatminds update` manually.

## Before upgrading a fleet

- Check the changelog for CLI or coordination-contract changes.
- Record the currently installed version: `greatminds --version`.
- Make sure the project has no partially moved tasks or orphaned intents:
  `greatminds watchdog`.
- Keep the previous package version available for rollback.

## After upgrading

Run:

```bash
greatminds --help
greatminds daemon status
greatminds watchdog
```

Then smoke one or two low-risk roles before restarting a whole fleet.
