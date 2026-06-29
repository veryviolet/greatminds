# Roles

Each role owns a small part of the pipeline. Ownership is defined by queue
location and encoded in the packaged schema copied to `.greatminds/schema.yaml`.

Product roles:

- `ARCHITECT-PLANNER`: intake, triage, planning, and routing. Lifecycle:
  `interactive`.
- `DEVELOPER`: backend implementation. Lifecycle: `driven`.
- `UI-DEVELOPER`: UI implementation or direct UI rapid iteration. Pipeline
  lifecycle: `driven`.
- `LIVE-DEVELOPER`: USER-paced scenario-C work from `feature_live` on a leased
  stand. For that interactive path it authors the implementation block for
  backend or UI scope, marks it ready after live USER validation, and hands the
  sprint task directly to `feature_review`.
- `TECHNICAL-WRITER`: documentation implementation. Lifecycle: `driven`.
- `TESTER`: validates implemented code and records test evidence. Lifecycle:
  `driven`.
- `READER`: reviews documentation as a fresh reader. Lifecycle: `driven`.
- `ARCHITECT-REVIEWER`: final review, blocked-task wake-up, commit policy.
  Lifecycle: `driven`.
- `EXPLORER`: live exploratory review and bug filing. Lifecycle: `driven`.

System and entry roles:

- `USER`: files feedback or chats with planner-facing roles. Lifecycle:
  `interactive`.
- `MAINTAINER`: non-user-facing infrastructure and fleet operations.
  Lifecycle: `self-loop`; USER reaches it through planner-mediated inbox asks
  rather than direct chat. It handles daemon and agent recovery, venv repair,
  canon cutover, and escalation of FSM stalls to the planner.

Every active role has a heartbeat file under `.greatminds/`. Stale heartbeats
are reported by `greatminds watchdog`.

For lifecycle mechanics across tools, see
[Lifecycle Model](../architecture/lifecycle.md).
