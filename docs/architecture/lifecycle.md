# Lifecycle Model

greatminds separates a role's responsibility from the mechanics used to run
that role. The contract lives in `schema.yaml`; `coord.yaml` chooses the tool
and launch mode for a concrete project.

## Role Lifecycles

| Lifecycle | Meaning | Normal roles |
| --- | --- | --- |
| `interactive` | Human-paced chat. The role can ask follow-up questions and stay open for operator steering. | `USER`, `ARCHITECT-PLANNER` |
| `self-loop` | Autonomous health-check loop. The role wakes itself on a timer and may be woken early by `coordd`. | `MAINTAINER` |
| `driven` | One event creates one turn. The pane is idle between turns; `coordd` starts a fresh invocation when work lands. | implementers, reviewers, tester, reader, explorer, stand keeper |

The product pipeline uses driven workers so idle roles do not spend tokens
polling empty queues. The exception is `MAINTAINER`: it is a non-user-facing
self-loop watchdog because fleet recovery must still happen when no product
task is moving.

## Lifecycle And Tool Matrix

| Lifecycle | Tool | Turn mechanism | Between turns |
| --- | --- | --- | --- |
| `interactive` | `claude`, `codex`, `cursor` | User or planner-facing chat input | Session remains operator-facing |
| `self-loop` | `claude` | `/loop` plus `ScheduleWakeup`, with `coordd` early wake | Loop waits for the next health tick |
| `self-loop` | `codex` or `cursor` | Explicit loop plus shell sleep fallback, with `coordd` interrupt | Loop waits for the next health tick |
| `driven` | `claude` | `coordd` starts one `claude -p` or resume turn with rendered role bootstrap | Idle shell pane |
| `driven` | `codex` | `coordd` starts one fresh `codex app-server` stdio turn and persists the thread id | Idle shell pane |
| `driven` | `cursor` | `coordd` starts the configured per-turn command | Idle shell pane |

For driven roles, the rule is one event, one tick, then exit. They do not run
`/loop`, schedule their own wakeups, or short-poll queues. Their first action
is still a CLI call such as `greatminds inbox list` or
`greatminds task list <queue>`, which also refreshes the role heartbeat.

## Wake Delivery

`coordd` observes queue files, inbox files, and stand state. It delivers turns
or wake signals; it does not decide task transitions.

For non-driven `codex` and `cursor` roles, `coordd` finds the deepest sleeping
descendant and sends `SIGINT`. That interrupts the long sleep wrapper so the
next loop tick starts immediately. For non-driven `claude` roles, it sends the
configured tmux input.

For driven roles, `coordd` spawns a single turn. A role that has no inbox
message and no task in its owned queue exits without scheduling another turn;
the next filesystem event drives it again.

