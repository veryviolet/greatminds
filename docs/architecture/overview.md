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

- The packaged schema defines queues, roles, transitions, required fields,
  stand profile tokens, scenarios, and watchdog thresholds. Setup copies the
  runtime copy to `.greatminds/schema.yaml`.
- `.greatminds/COORDINATE.md` explains the invariants behind the schema.
- Role contracts in `.greatminds/schema.yaml` define what each role may claim,
  write, and move.

When schema and prose disagree on mechanics, `.greatminds/schema.yaml` wins.
