# Local web workspace

Owner requirements (2026-09-07): local server on a configurable port, no
application authentication, interactive-role tabs, dashboards, settings toolbar,
and inspection of batch-agent turns. Current supported harnesses remain Codex,
Claude and Grok. Authentication or remote access can be supplied by an operator's
reverse proxy; the application itself has no login, accounts or session tokens.

Implementation:

1. Package a local HTTP server and static UI; expose `greatminds web --port PORT`.
   Use existing daemon/domain services for state and actions. Support attaching to
   an existing daemon and optionally starting an owned coordd child.
2. Provide overview/tasks, role-bound conversations, runs with recorded messages,
   tool activity and command evidence, pending one-time permissions, cancellation,
   dispatch pause/resume, and editable validated execution settings.
3. Persist bounded, redacted public agent activity outside the authoritative run
   metadata. Do not retain private reasoning. Preserve reconnect cursors and make
   missing or truncated history visible. Reading UI state must not launch agents.
4. Validate HTTP boundaries, stale settings, message idempotency and activity
   retention/privacy with meaningful tests. Exercise the real packaged UI in a
   browser against an isolated fixture project; no live inference is required.

Defaults: bind 127.0.0.1, port 8765, one project per server. Explicit `--host`
allows deployment behind an operator-controlled proxy. Same-origin browser write
checks are transport hygiene, not authentication. Static assets ship in the wheel.
