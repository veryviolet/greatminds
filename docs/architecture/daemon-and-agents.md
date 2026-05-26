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

`coordd` watches `coordination/` for inbox and queue-file activity and nudges
idle agents. It does not own the task FSM and does not decide task transitions.
Its job is latency: when a producer moves work into another role's queue, the
consumer can react without waiting for a fallback polling interval.

On Linux, `coordd` arms an inotify watcher. New inbox files under
`coordination/inbox/<role>/` and new task files landing in watched queues trigger
the owning role's event-wake path. The wake mechanism is selected from
`schema.yaml` under `event_wake.by_tool`, keyed by the role window's
`coord.yaml` `tool:` value:

- `codex` and `cursor`: `coordd` finds the role's deepest sleeping descendant
  and sends `SIGINT`. That interrupts the long sleep wrapper so the next tick
  starts immediately.
- `claude`: `coordd` sends the configured `tmux send-keys` text plus Enter to
  the role's tmux pane. The default text is defined in
  `event_wake.tmux_send_keys.keys`.

This event path replaces short idle polling for normal queue and inbox changes.
Reaction time should be the daemon watcher interval plus one signal or tmux
keystroke, not the agent's long sleep value.

`coordd` does not push visual status markers into chat panes. Those markers are
per-agent utterances: after a successful task move, task block append, or inbox
send, the agent ends its own chat reply with the matching marker line.

### Event-wake troubleshooting

If a role does not react to new work, check these in order:

1. Confirm the daemon is running for the project with `greatminds daemon status`.
2. Confirm the role exists in `coord.yaml`, has the expected `tool:`, and has a
   live agent registry entry.
3. Confirm `schema.yaml` still maps that tool in `event_wake.by_tool`.
4. For `codex` or `cursor`, inspect whether the agent is actually inside a sleep
   descendant. `coordd` does not signal the agent process itself when no sleeping
   child exists.
5. For `claude`, confirm the tmux session and window name match `coord.yaml`.
   Chat-mode wakes are rate-limited by
   `event_wake.tmux_send_keys.rate_limit_seconds`, so a burst of messages may
   coalesce into one prompt.
6. Run `greatminds watchdog` to check dead pids, stale heartbeats, and orphaned
   intents.

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
