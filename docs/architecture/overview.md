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
- Role contracts in the effective schema define what each role may claim,
  write, and move. Print that contract with `greatminds project schema`.

The installed package schema is authoritative, including when an explicit
`GREATMINDS_CANON_DIR` selects an alternate canon. The project copy is a generated
mirror, not a policy override. CLI validation, daemon dispatch, and service
installation use the installed contract. Restart running processes after a
package/canon update so cached validation tables use that same contract.

`greatminds project schema --json` reports the source path, schema version,
SHA-256 content identity, document, and project mirror status.
`greatminds project schema --check` checks the mirror without changing it and
exits 2 when it is missing, unreadable, or differs from the effective schema.
Inspect differences before refreshing generated copies with `greatminds setup`.

When schema and prose disagree on mechanics, the effective schema wins.
