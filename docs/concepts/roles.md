# Roles

Each role owns a small part of the pipeline. Ownership is defined by queue
location and encoded in `schema.yaml`.

Product roles:

- `ARCHITECT-PLANNER`: intake, triage, planning, and routing.
- `DEVELOPER`: backend implementation.
- `UI-DEVELOPER`: UI implementation or direct UI rapid iteration.
- `TECHNICAL-WRITER`: documentation implementation.
- `TESTER`: validates implemented code and records test evidence.
- `READER`: reviews documentation as a fresh reader.
- `ARCHITECT-REVIEWER`: final review, blocked-task wake-up, commit policy.
- `EXPLORER`: live exploratory review and bug filing.
- `STAND-KEEPER`: stand requests, deployment evidence, and stand status.

System and entry roles:

- `USER`: files feedback or chats with planner-facing roles.
- `MAINTAINER`: infrastructure and fleet operations.

Every active role has a heartbeat file under `coordination/`. Stale heartbeats
are reported by `greatminds watchdog`.
