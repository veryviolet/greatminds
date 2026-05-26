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

`coordd` does not push visual status markers into chat panes. Those markers are
per-agent utterances: after a successful task move, task block append, or inbox
send, the agent ends its own chat reply with the matching marker line.

## Visual event markers

Operators can scan live tmux panes by the marker at the end of an agent reply.
The templates live in `schema.yaml` under `visual_events`; docs and prompts
describe the contract, but the schema is the source of truth for the exact emoji
and markdown shape.

- `CLAIMED`: an agent moved a task into an implementation or ownership queue it
  is taking over.
- `FINISHED`: an implementer moved completed work to the next review or test
  queue.
- `ACCEPTED`: a reviewer moved the task to `verified`.
- `REJECTED`: review or test feedback sent the task back for more work, or a
  failure/partial result was recorded.
- `SENT`: an agent sent an inbox message to another role.

Because these are emitted by the acting agent, they appear only when the agent
successfully completes the CLI action and replies. A daemon-pushed dashboard or
status overlay is not part of this behavior; that belongs to the future 1.3.0
dashboard work.

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
