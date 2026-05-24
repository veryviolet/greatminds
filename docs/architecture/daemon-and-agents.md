# Daemon and Agents

`greatminds launch` starts the configured agent windows. Each window is defined
in `coord.yaml` with:

- window name
- role
- tool (`claude`, `codex`, `cursor`, or `bash`)
- mode (`chat` or `loop`)

Loop-mode roles repeatedly check their inbox and the queues they own. Chat-mode
roles wait for direct user interaction.

## coordd

`coordd` watches inbox and journal activity and pushes wake text to idle agents.
It does not own the task FSM and does not decide task transitions. Its job is
latency: when a producer moves work into another role's queue, the consumer can
be nudged before its next scheduled polling interval.

## pty launch

The launcher wraps agent tools in a pty and records an input socket in the
agent registry. The socket lets `coordd` send actual input to the agent process
instead of writing display text to a terminal.

## Heartbeats

Most CLI calls update the caller role's heartbeat as a side effect. Watchdog
reports stale heartbeats, orphaned intents, dead pids, and stale tasks:

```bash
greatminds watchdog
```
