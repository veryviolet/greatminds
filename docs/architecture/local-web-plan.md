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

## Delivery and acceptance

Implemented on 2026-09-07. The UI and server are packaged in the Python wheel;
`greatminds web --help` exposes project, host, port and daemon lifecycle options.
The implementation uses the existing conversation, permission, command, run and
configuration services. The additional public batch journal is bounded and a
journal write failure does not change execution success or workflow authority.

Verified with local ACP fixtures (no provider inference):

- Browser: queued message before daemon start, start/stop controls, response,
  continuation after page reload, paused admission of a new role, cancellation of
  its queued turn, validated settings save/restart notice, staged agent discard,
  task YAML, batch messages and verified command output.
- Focused HTTP/supervisor tests (37 passed) and both three-role
  pipelines through `verified` passed, covering one-time permission replies, stale settings,
  duplicate delivery, output redaction across chunks, bounded retention, and
  non-critical activity storage failure.
- Isolated wheel installation: assets match source, configurable/ephemeral port,
  invalid YAML can be repaired without exiting the web server, external daemon
  attachment does not confer stop ownership, SIGTERM stops the owned daemon.
- Strict MkDocs build, generated contract reference check, JavaScript syntax
  check, and `git diff --check` passed.

The browser was exercised on a disposable fixture project. Codex, Claude and
Grok use the same configured ACP bindings; this UI change did not repeat their
live provider acceptance probes. Public batch history starts with new runs;
existing runs retain their prior state/results/evidence. Dispatch pause stops
new runs, while already-open interactive sessions remain usable.

Final regression: **1546 existing/core tests passed**, plus **10 web workspace
tests passed** (included in the focused run). The HTTP tests need loopback socket
access. The remaining suite ran in its normal process namespace: running the
entire suite outside that namespace initially produced three migration-safety
failures from unreadable host `/proc` entries; all three passed in the normal
isolated regression. Every test ran in the appropriate environment.
