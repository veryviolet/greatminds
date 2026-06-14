# Daemon and Agents

`greatminds launch` starts the configured agent windows from the environment
where greatminds is installed, for example `greatminds launch --target tmux`.
Each window is defined in `coord.yaml` with:

- window name
- role
- tool (`claude`, `codex`, `cursor`, or `bash`)
- mode (`chat`, `loop`, or `driven`)

The role's lifecycle is declared in `coordination/schema.yaml`:

- `interactive`: human-paced chat. `ARCHITECT-PLANNER` is the normal
  user-facing planner.
- `self-loop`: an autonomous watchdog loop. `MAINTAINER` uses this to recover
  dead agents, restart coordd, and escalate FSM stalls without waiting for USER.
- `driven`: no persistent agent loop. The pane is idle between turns; `coordd`
  runs one turn when an inbox, queue, or stand-state event lands.

Driven dispatch requires both `coordination/schema.yaml` lifecycle and the installed
`coord.yaml` window mode to be `driven`. That gate lets existing fleets migrate
role by role.

## coordd

`coordd` watches `coordination/` for inbox, queue-file, and stand-state
activity. It does not own the task FSM and does not decide task transitions.
Its job is turn delivery: when a producer moves work into another role's queue,
the consumer can react immediately.

On Linux, `coordd` arms an inotify watcher. New inbox files under
`coordination/inbox/<role>/` and new task files landing in watched queues trigger
the owning role's event path. For non-driven roles, the wake mechanism is
selected from `coordination/schema.yaml` under `event_wake.by_tool`, keyed by the role
window's `coord.yaml` `tool:` value:

- `codex` and `cursor`: `coordd` finds the role's deepest sleeping descendant
  and sends `SIGINT`. That interrupts the long sleep wrapper so the next tick
  starts immediately.
- `claude`: `coordd` sends the configured `tmux send-keys` text plus Enter to
  the role's tmux pane. The default text is defined in
  `event_wake.tmux_send_keys.keys`.

For driven roles, `coordd` starts one turn instead of waking an existing loop:

- `claude` driven roles run one `claude -p` / resume turn with the rendered
  role bootstrap file.
- `codex` driven roles run one fresh `codex app-server` stdio process for the
  turn; the app-server thread id is persisted for continuity.

Driven roles do one tick, then exit. They do not schedule long sleeps or run a
persistent `/loop`; their next turn comes from the next inbox, queue, or stand
event.

This reactive path replaces short idle polling for normal queue and inbox
changes. Reaction time should be the daemon watcher interval plus one driven
spawn or wake signal, not a role's fallback sleep value.

`coordd` does not push visual status markers into chat panes. Those markers are
per-agent utterances: after a successful task move, task block append, or inbox
send, the agent ends its own chat reply with the matching marker line.

### Event-wake troubleshooting

If a role does not react to new work, check these in order:

1. Confirm the daemon is running for the project with `greatminds daemon status`.
2. Confirm the role exists in `coord.yaml`, has the expected `tool:`, and has a
   live agent registry entry.
3. Confirm the role's `coordination/schema.yaml` lifecycle and `coord.yaml` mode agree with
   the expected model. Driven roles require both values to be `driven`.
4. For a driven role, inspect coordd logs for the driven spawn result and check
   the per-role run lock under `coordination/.locks/`.
5. Confirm `coordination/schema.yaml` still maps that tool in `event_wake.by_tool` for
   non-driven roles.
6. For non-driven `codex` or `cursor`, inspect whether the agent is actually inside a sleep
   descendant. `coordd` does not signal the agent process itself when no sleeping
   child exists.
7. For non-driven `claude`, confirm the tmux session and window name match `coord.yaml`.
   Chat-mode wakes are rate-limited by
   `event_wake.tmux_send_keys.rate_limit_seconds`, so a burst of messages may
   coalesce into one prompt.
8. For driven `claude`, run `greatminds daemon doctor --project-dir "$PWD"`.
   It probes `claude -p` in daemon-equivalent env and reports expired OAuth
   credentials, missing refresh tokens, and missing captured agent env files.
   Repair auth with `claude setup-token` or `claude auth login` as the daemon's
   OS user, then restart the daemon.
9. Run `greatminds watchdog` to check dead pids, stale heartbeats, and orphaned
   intents.
10. Run `greatminds agent status [ROLE]` to inspect the recorded pid, liveness,
   session id, venv, heartbeat age, and input socket without reading registry
   files by hand.

## Visual event markers

Operators can scan live tmux panes by the marker at the end of an agent reply.
The templates live in `coordination/schema.yaml` under `visual_events`; docs and prompts
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
