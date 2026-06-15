# CLI reference

The generated reference below comes from the installed Click commands. For
day-to-day coordination, use these groups instead of editing files under
`coordination/` by hand:

| Need | Command surface |
| --- | --- |
| Create, inspect, move, or annotate tasks | `greatminds task ...` |
| Send, read, or acknowledge inter-role messages | `greatminds inbox ...` |
| Acquire, prepare, inspect, or release the singleton stand | `greatminds stand ...` |
| Inspect transition history | `greatminds journal ...` |
| Inspect live agent pids, sessions, venvs, heartbeats, and input sockets | `greatminds agent status [ROLE]` |
| Watch chronological driven-agent turn events | `greatminds driven-log --follow` |
| Check stand-gate evidence | `greatminds gate-check <task-id>` |
| Check blocked-task readiness | `greatminds wake-check` |
| Check stale agents, stale tasks, and orphaned intents | `greatminds watchdog` |
| Launch, restart, or update a fleet | `greatminds launch`, `greatminds restart`, `greatminds update` |
| Inspect project docs and runtime paths | `greatminds project ...`, `greatminds task paths` |

::: mkdocs-click
    :module: greatminds.cli.main
    :command: cli
    :prog_name: greatminds
    :depth: 1
