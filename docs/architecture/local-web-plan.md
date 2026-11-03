# Local web workspace

Owner requirements (2026-09-07): local server on a configurable port, no
application authentication, interactive-role tabs, dashboards, settings toolbar,
and inspection of batch-agent turns. Current supported harnesses remain Codex,
Claude and Grok. Authentication or remote access can be supplied by an operator's
reverse proxy; the application itself has no login, accounts or session tokens.

Implementation:

1. Package a local HTTP server and static UI; expose `greatminds web --port PORT`.
   Use existing daemon/domain services for state and actions. Support attaching to
   an existing daemon and optionally starting the independent project daemon.
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
  invalid YAML can be repaired without exiting the web server, daemon control uses verified project process identity, and stopping the web
  server leaves the daemon running.
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


## Operator improvements (3.2.0)

Owner pool 5–9: sanitized streaming Markdown; ACP-advertised model/reasoning
choices; deterministic stand operations and Ansible controls; English/Russian/
Simplified Chinese UI. Stand Keeper remains a deterministic daemon responsibility,
not an additional model role. New users default to English; a per-user host setting
can default all local projects to Russian. See the local web guide for setup.


3.2.0 acceptance: 1553 regression tests passed (one skip), 12 HTTP tests and six
background lifecycle tests passed, and eight browser DOM tests passed. Real
installed Codex 1.10.0 and Claude ACP 0.75.1 adapters returned model and reasoning
choices without prompting (7/6 and 5/6 respectively). A temporary browser workspace
queued a connection check; after the web server was terminated, the independently
started daemon consumed and completed the durable request. Temporary services were
stopped after verification. Strict documentation and wheel builds passed.

## Planning repair and GUI measurements (3.4.0)

Standalone planner chats now submit durable task operations through the daemon.
The previous managed path could create intake but rejected `plan`/evidence/moves,
while `run submit` required an assigned task that a standalone chat did not have.
The repair preserves that assigned-result boundary and the conversation subject.
See [planning requests](execution-contract.md#standalone-planner-requests) for
credentials, ownership, revision, deduplication and recovery rules.

Deterministic ACP tests exercise the public CLI from a planner conversation,
several tasks, repeated delivery and restart, and a full planner → developer →
tester → reviewer → verified pipeline with three real configured unit-test runs.
Crash injection covers prepared, written, moved, journaled and committed task
operations. Wrong-role/revision and conflicting-work cases are rejected. These
fixtures verify orchestration; they do not evaluate an external model's ability
to interpret user approval or write a good plan.

The GUI read path no longer runs complete workflow/stand diagnostics on each
poll. Bounded caches validate file identity and timestamps; authoritative task
mutation checks are unchanged. ETags avoid repeated state transfers. Chats use
turn windows and event cursors; run history uses pages while retaining all active
runs. Selected run traces fetch only new activity. DOM updates retain existing
cards and expanded details, and hidden tabs stop polling.

[Recorded measurements and reproduction commands](evidence/web-performance-2026-11-02.json)
compare baseline `75f5e77` with this working tree:

| Measurement | Before | After |
| --- | ---: | ---: |
| Backend overview, 1000 tasks, median of 7 | 646.4 ms | 18.5 ms |
| Backend overview, first read | 659.2 ms | 192.4 ms |
| Backend overview, warm p95 | 684.9 ms | 18.9 ms |
| Chromium first overview, single sample | 347.1 ms | 124.6 ms |
| Chromium idle dashboard poll, median of 7 | 17.9 ms | 4.7 ms |
| Chromium run view, single sample | 106.7 ms | 13.8 ms |
| Chromium chat idle update, median of 7 | 13.2 ms | 8.8 ms |
| API bytes during browser scenario | 2,146,619 | 291,887 |

The backend fixture has 20 conversations, 100 stored runs and 100 turns per
conversation. The browser fixture has 1000 tasks, 1000 runs and 100 turns; it
renders the latest 100 runs initially instead of all 1000 and also checks loading
older chat turns. Changed task cards remain connected to the DOM. Chromium uses
intercepted APIs: its timings exclude backend/network latency and must not be
presented as end-to-end production latency or statistically established p95.

At 3000 tasks, backend overview median is 49.0 ms and warm p95 is 55.4 ms;
the first read is 575.5 ms. Cold reads do not meet a 200 ms state-response target
at that size. The source remains filesystem-backed, so cold parsing, very large
journals and active writes can still cost more than an unchanged cached poll.
No database, frontend framework or executor/model change is required for these
improvements. The benchmark scripts start no daemon or model; Chromium closes
its isolated contexts and process on completion.

Verification covered 1595 Python tests (two additional skips), 17 browser DOM
checks, the VS Code extension tests, generated-reference checks and a strict
MkDocs build. Socket tests ran with loopback access. Three process-inventory
checks fail in that host namespace because unrelated `/proc` entries cannot be
read; the same failures reproduce at baseline `75f5e77`. All eight migration-safety
tests, including those three, pass in the normal isolated namespace. A final
process inventory found no remaining processes in the temporary test projects.
These checks were performed before the 3.4.0 release. They do not by themselves
prove package publication.
