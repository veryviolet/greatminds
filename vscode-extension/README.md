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
without recreation, close requests, and cancelled selection. A real extension-host
smoke test remains part of the modernization acceptance work.
