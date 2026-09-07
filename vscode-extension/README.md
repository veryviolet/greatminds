# greatminds VS Code extension

This extension is a VS Code cockpit for a greatminds project. It does not
implement the FSM itself; it calls the installed `greatminds` CLI and treats
the CLI as the backend API.

Current commands:

- `greatminds: Refresh`
- `greatminds: Open Dashboard`
- `greatminds: Follow ACP Run Events`
- `greatminds: Run coordd`
- `greatminds: Show Agent Tools`
- `greatminds: Show Stand Status`
- `greatminds: New ACP Chat`
- `greatminds: Attach ACP Chat`
- `greatminds: Close ACP Chat`

For ACP chat, configure `coordination/execution.yaml` and run the project daemon.
New ACP Chat selects an existing role binding and optionally an exact task ID.
Attach ACP Chat selects a saved open conversation. Both open `greatminds chat talk`
as the terminal process; the extension does not own or launch the harness process.
Closing the terminal detaches the client. Close ACP Chat asks the daemon to cancel
work and release the session while preserving its journal.

The extension reads binding/conversation metadata through `chat bindings` and
`chat list`; prompt text is excluded from those lists. CLI executable, project
path, and conversation identity are supplied as separate terminal arguments via
the [VS Code TerminalOptions API](https://code.visualstudio.com/api/references/vscode-api#TerminalOptions).
Use an absolute `greatminds.cliPath` when the CLI is absent from the editor's PATH.
Commands operate on the first workspace folder, as do the existing cockpit commands.

Configuration:

- `greatminds.cliPath`: path to the `greatminds` executable. Defaults to
  `greatminds`.

Development checks:

```bash
npm test
```

The test harness runs under Node.js and mocks the VS Code API. It verifies the
CLI backend invocation, Agent Tools tree rendering, and cockpit terminal
commands without requiring a graphical VS Code session.
The ACP tests cover role/task selection, argv handling with spaces, attaching
without recreation, close requests, and cancelled selection.

A separate Linux smoke runs inside the real Extension Development Host using the
[official test runner entry point](https://code.visualstudio.com/api/working-with-extensions/testing-extension#advanced-setup-your-own-runner):

```bash
python tools/vscode_host_smoke.py --code /path/to/supported/code --cli /path/to/greatminds
```

Run from the repository root with Python, a VS Code version satisfying the package
engine requirement, and `xvfb-run` installed. The launcher creates a temporary
workspace/profile and CLI symlink containing spaces and shell metacharacters. It
loads this development extension, exercises CLI metadata commands, and observes
actual event-stream, daemon and dashboard terminal processes. Closing each terminal
must stop its process. The profile disables updates and telemetry; no harness is
configured. Logs and the JSON result remain in the printed temporary directory.
The launcher bounds the run and terminates its process group on timeout.

VS Code 1.92.2 passed this smoke on Linux x86_64; see
[recorded evidence](../docs/architecture/evidence/vscode-extension-host-2026-09-07.json).
Quick-pick interactions and live ACP chat remain covered by their separate tests,
not by this host smoke. All cockpit terminals now invoke the configured CLI with
separate argv, just like chat terminals; CLI paths are never sent as shell text.
