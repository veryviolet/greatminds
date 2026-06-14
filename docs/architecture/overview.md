# Architecture Overview

greatminds is built around a filesystem finite state machine:

- State lives in directories.
- A task handoff is a validated file move.
- Blocks inside task files are append-only evidence.
- Role ownership comes from queue location.
- Coordination messages live in per-role inbox directories.
- The daemon only nudges agents; it is not the source of task state.

This makes the project recoverable with normal filesystem inspection. If an
agent exits mid-transition, intent files and the journal show what it attempted.
If a daemon is down, the queues still describe the work.

## Contract sources

- `coordination/schema.yaml` defines queues, roles, transitions, required
  fields, stand profiles, scenarios, and watchdog thresholds.
- `coordination/COORDINATE.md` explains the invariants behind the schema.
- Role contracts in `coordination/schema.yaml` define what each role may claim,
  write, and move.

When schema and prose disagree on mechanics, `coordination/schema.yaml` wins.
