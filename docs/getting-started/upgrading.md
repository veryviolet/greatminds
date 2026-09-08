# Upgrading

Run package updates from the project and Python environment used by Greatminds.
Inspect the available release first:

```bash
greatminds update --check
```

This queries PyPI without changing the package or project. Updates are operator
initiated; the ACP daemon does not currently send periodic release notifications.

## Moving to 3.0

The 3.0 release uses ACP for every managed executor. Define agent manifests and
role bindings in `coordination/execution.yaml`, including the installed adapter
and harness versions. Setup preserves task files and an existing execution
configuration; it does not choose harnesses or credentials. Verify this contract
before starting the daemon. Codex, Claude and Grok are the accepted harness scope.

The packaged [web workspace](local-web.md) starts with `greatminds web --port 8765`.
It has no application authentication and binds to localhost by default.

## Prepare active work

Inspect runs and stop new dispatch before changing the daemon's environment:

```bash
greatminds run status
greatminds run pause
```

Pause leaves existing work running. Let it finish or explicitly cancel selected
runs before restarting. Uncertain command or deployment effects need the
recovery procedures in the [operations runbook](../operations/runbook.md).
A daemon restart does not prove those effects safe to repeat.

## Apply the update

```bash
greatminds update
```

The command selects the detected environment manager, upgrades the package,
checks the installed version in a fresh Python process, and refreshes project
bootstrap state. A major version increase requires `--major`.
For an explicitly registered service project, use `--project PROJECT_NAME`.

Existing installed services have their units refreshed and receive
`systemctl --user try-restart`. Inactive services remain inactive. Update skips
missing service installations; install a service separately when you need one.
For a uv tool installation, use `uv tool upgrade greatminds`. Restart the web
server after upgrading. Independently managed background daemons also need an
explicit `greatminds daemon restart` to load runtime changes; check active work
first. Restart a foreground daemon manually using the updated environment. Harnesses
and ACP adapters have their own installation and update procedures.

To repeat only the project and installed-service refresh after managing the
package yourself:

```bash
greatminds update --post-pip
```

## Verify and resume

```bash
greatminds daemon doctor --project-dir "$PWD" --json
greatminds project schema --check
greatminds run status
greatminds watchdog
```

Doctor performs static checks; exercise an actual ACP conversation to verify
provider authentication and protocol behavior. Resolve reported holds before
resuming automatic dispatch with `greatminds run resume`.

The effective installed schema governs runtime policy. `.greatminds/schema.yaml`
is a diagnostic mirror, not a project policy override. Setup creates a missing
mirror but preserves an existing one. If `project schema --check` reports drift,
compare the mirror with `greatminds project schema`; after reviewing the change,
replace the mirror with that command's output. Restart the daemon after changing
the installed package or an explicit `GREATMINDS_CANON_DIR`. Existing runs retain
their pinned contracts.
