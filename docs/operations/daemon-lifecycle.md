# Independent project daemon

CLI and web controls manage the same daemon for a canonical project directory.
No systemd, registration or service installation is required for normal use.

```bash
cd /path/to/project
greatminds daemon start
greatminds daemon status
greatminds web --port 8765
```

`start` waits for recovery and readiness before returning. `greatminds coordd`
is another way to start the same independent daemon. Repeating either command
reports the existing process. Separate projects run separate daemons.

Closing the browser, stopping the web server, or closing the launching terminal
does not stop this daemon or its agents. Start/stop buttons in any web instance
operate on the same project process, including one started from the CLI.

## Stop and restart

```bash
greatminds daemon stop
greatminds daemon restart
```

Stop requests orderly shutdown and waits for the daemon to finish its active
operations and clean up agents. If this takes too long, it reports a timeout;
it does not silently escalate to killing processes. Inspect status and logs
before retrying. Restart waits for shutdown before starting a replacement.
Broken execution configuration does not prevent stopping an identified daemon.

Start and restart validate configuration before launching. Startup failure or
readiness timeout is an error, not a successful detached launch. After a timeout,
check status before retrying: initialization may still be in progress.

## Identity, status and logs

A kernel file lock held throughout supervisor execution prevents duplicate
owners, including simultaneous starts and paths through symlinks. Control
operations also serialize through a project lock. Lock files are never deleted
to force admission; the kernel releases their locks on process exit.

Status reports the held supervisor lock, process identity, readiness/heartbeat,
configuration changes requiring restart, and the log path. PID, boot identity
and process start time are checked; signals use pidfds to avoid PID reuse races.
Missing or invalid owner identity is reported as unknown and is not signalled.
A heartbeat older than ten seconds is unresponsive, not proof of process exit.

Detached stdout, stderr and daemon logs are saved privately to
`.greatminds/.runtime/daemon.log`, with three rotated backups and a 2 MiB limit
per file. Logs are independent of the starting client. Foreground output stays
in its terminal.

A crash releases the lock automatically. The next start runs the existing
recovery protocol for unfinished work and agent processes; uncertain effects
remain subject to recovery gates. A detached process does not automatically
restart after a crash or machine reboot.

## Foreground diagnostics

```bash
greatminds coordd --foreground --project-dir /path/to/project
```

This explicit mode stays in the terminal and exits on Ctrl+C. It uses the same
exclusive supervisor lock, so it cannot run alongside another project daemon.
`--once` remains a foreground single-pass command for diagnostics and tests.

## Optional systemd integration

Use this only when you want a user service manager to restart a failed daemon or
start it at the user manager's default target. Stop a manually started daemon
before enabling this alternative lifecycle owner:

```bash
greatminds daemon stop
greatminds daemon install --name my-project
greatminds daemon start --systemd --project my-project
greatminds daemon status --systemd --project my-project
greatminds daemon stop --systemd --project my-project
```

The generated unit runs `coordd --foreground`. Systemd owns restart policy: use
its explicit controls to stop it, otherwise its configured restart policy may
start it again after a direct process stop. Host/user-manager configuration
determines whether it starts before login and survives logout.
