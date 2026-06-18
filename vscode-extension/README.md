# greatminds VS Code extension

This extension is a VS Code cockpit for a greatminds project. It does not
implement the FSM itself; it calls the installed `greatminds` CLI and treats
the CLI as the backend API.

Current commands:

- `greatminds: Refresh`
- `greatminds: Open Dashboard`
- `greatminds: Follow Driven Log`
- `greatminds: Run coordd`
- `greatminds: Show Agent Tools`
- `greatminds: Show Stand Status`

Configuration:

- `greatminds.cliPath`: path to the `greatminds` executable. Defaults to
  `greatminds`.
