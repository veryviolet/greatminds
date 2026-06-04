# Changelog

All notable changes to **greatminds** are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; versions
follow [SemVer](https://semver.org/) once 1.0.0 ships.

## 1.5.14 — 2026-06-04

Fix: driven claude roles never ran under the daemon; the dashboard
contradicted itself on driven roles; a daemon restart stranded a queued
task.

### Fixed

- **Driven turns spawn the tool by its REAL absolute path.** coordd runs
  as a systemd-user daemon with a minimal PATH (no `~/.local/bin`), so a
  bare `claude` / `codex` argv[0] failed to spawn — the claude-driven
  roles (TESTER / DEVELOPER / UI-DEVELOPER / READER) silently never ran
  (only codex roles survived, via their own app-server unit). New
  `_resolve_tool_bin` resolves the real path (PATH → the user's LOGIN
  shell `command -v` → bare), so it works even for a non-standard install
  location; driven argv[0] and the subprocess PATH use it.
- **coordd reconciles its backlog on startup.** coordd was purely
  inotify-reactive, so a task already sitting in a queue when it
  (re)started — or whose event was consumed by a turn that failed to
  spawn — was never driven. On start it now drives one turn for each
  driven role that has pending work in a queue it claims from and isn't
  mid-turn. A daemon restart (e.g. after `update`) now picks up a hanging
  task instead of stranding it.

### Dashboard

- **Driven roles are no longer shown as `dead` + `running turn` at
  once.** STATE was derived from a persistent registry pid, which driven
  roles don't have — so a role running a turn read as "dead". STATE is
  now coherent with the driven model: `running` during a live turn (lock
  + live pid/heartbeat), `idle` between turns (normal — coordd drives on
  events), never `dead`. A stale run-lock from a killed coordd no longer
  reads as "running" forever.
- **TASKS table aligns; the ID column shows just the task number.** The
  full slug (`0001-verify-full-deploy-…`) overflowed the ID column and
  broke alignment; it now shows `0001`.

### Internal

- Removed dead code (`role_has_pending_work`, an unused `*.md`-only
  pending-work scan from the legacy-MD era; redundant local `shutil` /
  `glob` imports).

## 1.5.13 — 2026-06-04

Fix: `update` could report success without actually upgrading (and needed
two runs to converge) when a foreign virtualenv was on PATH.

### Fixed

- **`update` resolves its own binary from the running venv, not PATH.**
  The self-replace used `shutil.which("greatminds")`, which is PATH-first
  — a foreign activated virtualenv (e.g. an unrelated `.venv-coord`
  leaking onto PATH, common when you have one project's venv active while
  running `uv run` in another) could shadow the project's greatminds, so
  the self-replace exec'd the WRONG version and the upgrade appeared to
  "need two runs". `_greatminds_bin` now pins to `sys.executable`'s
  sibling (the env update is installed in).
- **`update` verifies the upgrade actually landed.** It printed a
  hardcoded `✓ … (old → new)` and `done: greatminds at <old>` even when
  the venv hadn't changed (the in-process `__version__` is stale right
  after a same-run upgrade). It now reads the REAL installed version in a
  fresh subprocess, forces one `uv sync --reinstall-package` pass if uv
  lagged, and FAILS LOUDLY if the version didn't change — instead of a
  fake success that silently needs a second run. The final "done" line
  reports the freshly-read version.

## 1.5.12 — 2026-06-04

Fix: a plain restart silently destroyed driven-agent sessions.

### Fixed

- **Default `greatminds restart` preserves session continuity.** For a
  dead agent it did `reg_path.unlink()` on `<role>.json` — but the
  driven-claude session UUID is stored IN that .json (coordd writes
  `reg["session_id"]`), and a driven role's pid is dead between turns, so
  every default restart wiped its session and force-freshed it on the
  next turn. Session DESTRUCTION must be reserved for the explicit
  `--reset` flag, never a side effect of a plain restart (or of
  `greatminds update`, which restarts on every run since 1.5.10). The
  dead-agent path now clears only the volatile `pid` / `input_sock` and
  PRESERVES `session_id` (and the claude/codex session-id sidecar files,
  which it never touched); `--reset` remains the only path that drops a
  session.

## 1.5.11 — 2026-06-04

### Added

- **`greatminds task list <queue> --json`.** `task list` only printed
  filenames; an agent inspecting a queue (a codex PLANNER hit this) has
  no raw access to `coordination/`, so it guessed a `--json` flag and
  errored. Added it — emits a JSON array of `{id, title, queue, file}`
  per task (malformed files degrade to the filename stem, never crash
  the listing). Plain output is unchanged (filenames only). Mirrors
  `agent status --json`.

## 1.5.10 — 2026-06-04

Fix: `update` skipped the migration when the package was already current.

### Fixed

- **`greatminds update` reconciles config even when the package is up to
  date.** 1.5.9 added the project-config migration to `update`, but only
  in the post-pip phase reached after an actual version bump — `update`
  did an early "already up to date" exit before it. So a fleet already on
  the latest package but with stale config (old all-paned coord.yaml,
  missing queues, leftover artifacts) ran `update` and migrated nothing.
  `update` now falls through to the migration + daemon/agent restart phase
  in-process when no bump is needed (no self-replace), so it ALWAYS brings
  the project config to the installed version. (`greatminds migrate`
  remains available standalone.)

## 1.5.9 — 2026-06-04

Make the fleet's working branch cleanly project-configurable, and make
`upgrade` migrate the whole project (not just the package).

### Fixed

- **`worktrees.default_branch` is now per-project configurable via
  `coord.yaml`.** 1.5.8 added the setting but `load_worktree_policy` read
  it only from the canon (package) `schema.yaml` — which ships `main`, is
  shared across every project on the host, and is overwritten on each
  upgrade. So a project could not actually pin its own branch. The policy
  now overlays a `worktrees:` override from the project's `coord.yaml`
  (project-local, never overwritten) on top of the canon defaults. To run
  a fleet on another branch: put `worktrees: { default_branch: <branch> }`
  in `coord.yaml`.

### Added

- **`greatminds migrate`** + **`greatminds update` now migrates the whole
  project.** Previously `update` bumped the package but left the project's
  config stale (e.g. a pre-driven `coord.yaml` with every role paned → 10
  tmux windows on launch; missing `feature_live`; leftover
  `command_START.yaml` / per-role docs / `bot_*` queues). `update` now runs
  a migration step (and it is also available standalone as `greatminds
  migrate`, for fleets already on the new package but with stale config):
  refresh canon (`setup`: schema / COORDINATE / bootstrap / missing queues
  / `.gitignore`), migrate `coord.yaml` from the old all-paned model to the
  current driven model (workers → `driven`, add `dashboard` + `live`),
  preserving session / `project_dir` / the `worktrees` override / custom
  role-less windows and backing up the old file, and remove artifacts
  deleted in newer versions (`command_START.yaml`, per-role `<ROLE>.md`
  docs, empty `bot_*` queues).

## 1.5.8 — 2026-06-04

Feature: configurable worktree base/merge branch.

### Added

- **`worktrees.default_branch`** (default `main`). The per-task worktree
  merge was hardcoded to `main` — `worktree_merge` did `git checkout
  main` → `git pull --ff-only origin main` → merge the task branch into
  main, and the base-commit last-resort fallback did `git rev-parse
  main`. A project therefore could NOT run its coordination on any
  branch other than main: every completed task's merge forced the work
  back onto main. The branch is now read from
  `schema.worktrees.default_branch` and used in both places; defaulting
  to `main` keeps existing fleets unchanged. coordd / setup / stand-deploy
  never referenced main and are untouched.

## 1.5.7 — 2026-06-04

Canon: insist on full-contract reading, add a no-code stand-verification
FSM path, and a read-only PROJECT.md surface.

### Added

- **`plan.verify_only` → `feature_plan → feature_test`.** A no-code
  stand/playbook task (e.g. "deploy the full stand by profile X",
  "verify this playbook") had no clean FSM path — `feature_test` was
  reachable only from an implementer queue with an `implementation`
  block. PLANNER now routes a `verify_only` plan straight to TESTER, who
  leases a stand, runs the profile/probe, and records stand evidence
  (mirrors the READER `audit_only` path). "Deploy the stand" and "test
  the stand" are the SAME path differing only in verification depth
  (readiness-only vs functional probes) — documented in COORDINATE §8.2.
  There is deliberately NO task-less stand deploy: every lease serves an
  auditable task, which is what anchors the tested/verified gate.
- **`greatminds project show`** — read-only print of
  `coordination/PROJECT.md`. Closes a protocol gap: the contract requires
  agents to read PROJECT.md, but it lives under `coordination/` and the
  mutations-via-CLI rule left no sanctioned CLI to obtain it.

### Changed

- **bootstrap.md now insists on reading the contract IN FULL.** The old
  wording ("YOUR contract is `roles.<ROLE>`") let agents — codex
  especially — skim to their role section and stop, then propose moves
  the FSM forbids. It now requires reading the whole `schema.yaml` every
  tick (glossary, the full queue graph, and the exact transitions of the
  path the task is on); the role block is necessary but not sufficient.
- **The "CLI-only" rule is scoped to MUTATIONS.** Reading the canon docs
  (`schema.yaml`, `COORDINATE.md`, `bootstrap.md`, `coordination/PROJECT.md`)
  directly is now explicitly allowed — they are docs, not FSM state.
  Only mutations + queue/inbox/task ops go through the CLI.

## 1.5.6 — 2026-06-04

Fix: MAINTAINER tick cadence + runtime files leaking into git.

### Fixed

- **MAINTAINER self-loop cadence is pinned to 1 hour in canon.** The
  1.5.0 refactor dropped the per-role docs (where the hourly recovery
  cadence used to live) and never carried it into the schema, so a fresh
  MAINTAINER fell back to the agent's own ~5-minute default loop — and
  any operator "set it to an hour" was lost on the next session /
  re-bootstrap. Added `schema.roles.MAINTAINER.self_loop_wake_seconds:
  3600`, and the `self-loop` lifecycle + `bootstrap.md` now instruct the
  agent to re-arm for that configured cadence (shorter only while
  mid-recovery) rather than inventing a faster interval.
- **`setup` gitignores per-role tool runtime.** The seeded
  `coordination/.gitignore` excluded heartbeats / registry / locks but
  NOT `.codex-home*/` (codex sessions, logs, and an embedded plugin git
  repo), `.turns/` (driven-turn logs), or `.stand/` (live lease state +
  deploy logs) — so a `git add` of `coordination/` swept codex runtime
  (and a nested git repo) into the project. Added those three to the
  generated `.gitignore`.

## 1.5.5 — 2026-06-04

Fix: `greatminds setup` never created the `feature_live` queue.

### Fixed

- **`setup` now creates the `feature_live` queue.** `feature_live` was
  added to `schema.queues` with the LIVE-DEVELOPER role in 1.5.0, but the
  hardcoded `setup.QUEUES` list was never updated — so every fresh
  `greatminds setup` from 1.5.0 through 1.5.4 silently omitted the queue
  (surfaced when re-bootstrapping a real fleet). Added `feature_live` to
  the list and pinned it against the schema with a new drift test
  (`test_setup_queues_match_schema_task_queues`): `setup.QUEUES` must now
  exactly equal the task-holding queues declared in the schema, so this
  class of drift can't recur.

## 1.5.4 — 2026-06-04

Feature: a live fleet status dashboard + tmux status-line config on launch.

### Added

- **`greatminds dashboard`** — a read-only, non-scrolling console table
  of the fleet at a glance: per-agent activity (alive/idle/working/
  running-turn, inferred from registry liveness + heartbeat freshness +
  driven run-lock + the task in the role's owned queue), active tasks by
  FSM state, and the singleton stand. Pure observer — holds no role,
  registers nothing, sends no wakes; safe to kill/restart. Refreshes in
  place (`--interval`, default 2s); `--once` prints a single frame;
  `--color/--no-color` (auto-detects a TTY). The `dashboard` coord.yaml
  window (`mode: dashboard`) now auto-runs it on `greatminds launch`.

### Fixed

- **tmux status line is configured on launch.** A fresh host without the
  operator's `~/.tmux.conf` got tmux's default `status-left-length` of
  10, which truncated a session name like `greatminds-dev` so the
  clipped `[greatminds` title collided with the window list
  (`0:planner` …). `launch` now sets `status-left-length` to fit the
  full `[<session>] ` title exactly (`len(session)+4`) and applies the
  fleet status colors (purple bg / white fg, current window
  bold+underscored) per-session, so every fleet looks right regardless
  of personal tmux config.

## 1.5.3 — 2026-06-04

Patch: waking an idle codex/cursor TUI quit the agent (Ctrl-C to the shell).

### Fixed

- **The event-wake SIGINT no longer kills interactive codex TUIs.** The
  wake primitive (`_send_enter.press_enter`, and the parallel
  `coordd.sigint_sleeping_descendant`) SIGINTs the deepest descendant of
  an agent to break a blocking `bash sleep` backoff timer so a queued
  Enter gets read. The guard was `leaf != agent_pid` — sufficient for
  claude (whose deepest descendant IS the agent process, so it was
  skipped), but WRONG for codex/cursor: those are multi-process (node →
  engine), so the deepest descendant is the LIVE ENGINE, not a sleep.
  SIGINT there is Ctrl-C → codex quits to the shell and the next
  `WAKE_TEXT` ("continue your tick") lands in bash (`-bash: continue:
  ...`). The leaf's `comm` was computed from the first commit but only
  ever used in the diagnostic string — never gated on. Both paths now
  SIGINT only when the leaf is an actual `sleep` process; a live engine
  is woken by the send-keys Enter alone (idle TUIs read input directly,
  so no interrupt is needed). The bug bit IDLE agents (stale heartbeat);
  busy agents were masked by the fresh-heartbeat push guard.

## 1.5.2 — 2026-06-04

Patch: the documented task-withdraw path was broken — a withdrawn task
could never be archived.

### Fixed

- **`feature_blocked → archive` (withdraw) no longer collides with the
  resume wildcard.** `transitions_for` resolved `any_resume_to_queue`
  against *any* concrete `to_q`, including the terminal `archive` queue.
  So `feature_blocked → archive` matched BOTH the exact withdraw row
  (`requires: feature_blocked_withdrawn_reason`) AND the resume wildcard
  (`requires: all_dependencies_exist_per_wake_check`), and
  `enforce_schema_requires` runs the requires from every matching row —
  so the resume path's wake-check fired on the withdraw path. A
  withdrawn task carries a never-resolving sentinel dependency by design,
  making the wake-check (and therefore archive) impossible. The two
  paths are mutually exclusive. `any_resume_to_queue` now matches only
  non-terminal queues, so a parked task can be withdrawn-archived (the
  canonical PLANNER-blocks → REVIEWER-archives cleanup path) without its
  sentinel dependency having to resolve.

## 1.5.1 — 2026-06-03

Patch: two issues surfaced operating the 1.5.0 fleet live.

### Fixed

- **`greatminds agent status` accepts multiple roles.** It was
  single-role-only; a freshly-restarted PLANNER ran
  `agent status STAND-KEEPER MAINTAINER TESTER` and got an
  "unexpected extra arguments" error. The command now takes any number
  of role args (and still defaults to every registered role).
- **MAINTAINER may now `stand reclaim`.** `schema.roles.MAINTAINER`
  carries `reclaim_stale_stand_lease_past_ttl_with_dead_holder` as a
  recovery duty, but the `stand reclaim` CLI gate allowed only
  STAND-KEEPER / ARCHITECT-PLANNER — so MAINTAINER could not actually
  close stale-lease recovery asks. Added MAINTAINER to the gate
  (the safety guards are unchanged: only an expired lease with a
  dead/absent holder is freed).

## 1.5.0 — 2026-06-03

Major canon consolidation + a new interactive role. Live-verified on a
real avatar fleet (driven turns bootstrap from the static prompt, claim,
implement, advance the FSM).

### Added

- **LIVE-DEVELOPER** — an interactive, USER-paced role for working a
  single task live with the USER on a leased stand. PLANNER routes a
  plan marked `interactive: true` to the new `feature_live` queue;
  LIVE-DEVELOPER leases a stand, deploys to its own lease during the
  session, works live, and on USER approval hands to `feature_review`
  as a sprint task (`approved_sprint` review outcome; TESTER skipped —
  the USER is the live validator). New `staged` launch mode (the pane's
  start command is pre-typed, the USER starts it). Replaces the old
  UI-DEVELOPER FAST chat variant; scenario C is now LIVE-DEVELOPER. Ships
  a `vite-dev` stand-profile example (backend + Vite HMR).

### Changed

- **Single static bootstrap.** An agent's whole instruction surface is
  now `COORDINATE.md` (philosophy) + `schema.yaml` (machine contract +
  glossary) + `coordination/PROJECT.md` (project specifics) + its role
  from `$GREATMINDS_ROLE`. The system prompt is one role-independent
  `coordination/bootstrap.md`; the per-role rendered prompt is gone.
- **schema.yaml is the token source of truth** — a `glossary` section
  now defines roles / lifecycles / wake_mechanisms / queue_kinds /
  queues / stand_profiles / stand_probes / intake_disciplines; the rest
  references tokens. Heartbeat is redefined as coordd's in-flight-turn
  hang detector (not a watchdog liveness scan).

### Removed

- The prompt-generation layer: `render-role` + `role contract` CLIs,
  `command_START.yaml`, and all `roles/*.md`.
- The unused **bot** stream (BOT-USER / BOT-DEVELOPER roles, `bot_*`
  queues), the dead `stand_enums` block + `task new --request-type` /
  `--profile` plumbing, the stalled-agent sweep + its config, and the
  vestigial stale-kick constants/log in coordd.
- All numbered task-id and past-version references from the canon docs.

## 1.4.2 — 2026-06-02

Patch release: SAFETY + the recovery-chain fixes the on-stand
validation surfaced after 1.4.1 shipped.

### Fixed

- **0349 SAFETY: EXPLORER host-destructive actions must target the
  avatar stand only.** EXPLORER had no machine-target boundary, and a
  recent campaign ran `kill` against local coordd on the dev fleet —
  host-destructive work hit the local greatminds-dev fleet instead of
  the avatar stand. The codex profile now spells out: "the stand" =
  the AVATAR host (deployed `/srv/greatminds-stand`), a DIFFERENT
  machine from EXPLORER's own process host. Every host-destructive /
  lifecycle / recovery command (kill, pkill, kill -TERM/-KILL/-SIGINT,
  systemctl stop/restart/kill, killing coordd / driven-worker /
  codex-app-server, logout, reboot) MUST run on the avatar via
  `ssh <STAND_HOST>`. Strictly forbidden against localhost or the
  local greatminds-dev fleet (greatminds-daemon@greatminds-dev +
  coordd). "The local host is NOT the stand; never damage it."
  Unresolvable destructive command → do not run; file a blocker. The
  0344 stand-anchor is preserved (ssh remains allowed for on-stand
  operation); the 0331-era no-host blanket is not re-added.

- **0345 watchdog ignores non-role heartbeat files + coordd
  periodically reaps orphan intents.** On a healthy reactive fleet
  watchdog flagged `heartbeat.planner` + `heartbeat.review_sessions`
  as stale, but those names are a tmux WINDOW name and a QUEUE name,
  not role heartbeats — the per-lifecycle threshold could not resolve
  them and fell back to the 600s default. Filename resolution now
  SEGREGATES non-role files (window / queue / unknown stems) into a
  single info line ("heartbeats: N non-role/legacy file(s) ignored:
  ..."). Orphan intents also piled up because the reaper existed but
  nothing ran it automatically; the safe reaping core is extracted
  into `intent_clean.reap_orphan_intents` and coordd runs it
  periodically (default 300s), wrapped so it never crashes the loop.

- **0346 coordd systemd unit `Restart=always` so external kills
  resurrect coordd.** The coordd systemd template unit used
  `Restart=on-failure`. coordd catches SIGTERM and returns cleanly,
  so an external `kill -TERM` produced exit 0/SUCCESS and on-failure
  did NOT restart the killed coordd (`NRestarts=0`, unit went dead).
  Now `Restart=always` (RestartSec=2) in both the shipped canon unit
  and the inline fallback body. Systemd resurrects coordd after any
  external kill / crash; a deliberate `systemctl --user stop` or
  `greatminds daemon stop` is still honored (systemd never
  auto-restarts a commanded stop regardless of `Restart=`), so update
  / restart flows are unaffected.

- **0347 coordd Step-2 inbox-scan drives migrated driven roles via
  the shared driver.** coordd drove driven roles only from the
  queue/.stand path; the inbox-scan woke every role via
  sigint_deepest_descendant / press_enter. A driven role's pane is
  idle bash between turns, so an inbox wake of a killed driven codex
  worker sigint'd a dead pane — nothing re-drove or re-registered it
  (agent status stayed not-registered). Extracted the dispatch
  decision into `_maybe_drive_driven_role`: lifecycle==driven AND
  coord.yaml window mode==driven → route through the driver
  (claude_driver for tool=claude, codex_driver for tool=codex); else
  None and the caller falls back to the legacy wake. `_route_queue_event`
  and the Step-2 inbox-scan both use the shared helper. A killed
  driven codex worker re-registers on the next event via a
  force-fresh turn.

- **0348 `greatminds restart` skips driven windows + reports them as
  coordd-managed.** restart's `_relaunch` built the launch command
  with the coord.yaml window mode verbatim, so a driven role got
  `greatminds start-agent <ROLE> codex --mode driven`; `--mode` is
  `Choice(loop|chat)` and errored, restart exited non-zero, the role
  was left MISSING. launch.py already SKIPS mode==driven windows;
  restart now mirrors that — `_relaunch` skips driven windows (recovery
  is the next coordd event; --reset still clears session files), and
  `_verify` reports driven roles as `driven (coordd-managed)` and
  excludes them from total/fail (pre-0348 they were flagged MISSING
  on a healthy driven fleet).

### Operator migration

- Re-seed `coordination/.codex-home/explorer/` so the 0349 + 0344
  clauses reach the LIVE EXPLORER. `greatminds setup` seeds the
  per-role codex homes once without overwrite, so MAINTAINER /
  STAND-KEEPER must refresh `coordination/.codex-home/explorer/` from
  the corrected shipped profile after the .venv-coord clean-wheel
  upgrade. Preserve `auth.json` in the per-role home during the
  refresh.
- Run `greatminds daemon install` (or `greatminds daemon repair`) +
  `systemctl --user daemon-reload` so the existing greatminds-daemon
  unit picks up the 0346 `Restart=always` directive.

## 1.4.1 — 2026-06-02

Patch release: brings the shipped EXPLORER codex profile in line with
the 0336 stand-anchor. Until this patch lands on the fleet, EXPLORER's
seeded codex profile keeps injecting a stale web-only boundary that
overrides the canon and blocks on-stand validation.

### Fixed

- **0344 EXPLORER codex profile carries the 0336 stand-anchor.** The
  shipped `src/greatminds/data/codex/profiles/explorer.config.toml`
  `developer_instructions` still carried the stale web-only boundary
  ("NEVER ssh into stand hosts. NEVER docker/docker compose. NEVER
  ls/cat host filesystem. REST API + DB ... ONLY"), which the
  running EXPLORER read at every turn via the seeded
  `coordination/.codex-home/explorer/` layer. Replaced with the 0336
  stand-anchor: EXPLORER always works ON the stand as a real user
  whatever the product shape — HTTP/browser for web, ssh + host CLI
  for host/CLI products, wherever deployed; host-destructive +
  recovery scenarios are EXPLORER's on the disposable stand;
  off-stand and local substitutes strictly forbidden. The
  coordination-access CLI-only rule is retained as a separate,
  correctly-scoped clause (it scopes `coordination/` I/O via the
  greatminds CLI; it is NOT about probing the deployed product).
  `[profiles.explorer]` table unchanged.

  Operator migration: `greatminds setup` seeds
  `coordination/.codex-home/explorer/` once without overwrite. For
  the LIVE EXPLORER to stop injecting the stale boundary,
  MAINTAINER / STAND-KEEPER must re-seed / refresh
  `coordination/.codex-home/explorer/` from the corrected shipped
  profile after upgrading the fleet to 1.4.1 (or the next on-avatar
  deploy regenerates it).

## 1.4.0 — 2026-06-02

Minor release closing the 0311 Phase 5 stand-robustness work + the DOD2
operator-CLI batch, on top of the 1.3.12 reactive-fleet base. Highlights:
per-lifecycle watchdog thresholds, codex 0.135 profile-v2 split, the
CLI-only coordination-access rule wired to the actual agent prompt
surface, a journal-tail / agent-status / task id-intake unification, a
coordd .stand watch-up-front + STAND-KEEPER auto-wake, an expired-lease
reclaim + queue-head auto-promote, the canon TESTER-local-exec ban, the
EXPLORER stand-only contract restore, and a full reactive-fleet
documentation refresh.

### Added

- **0337 CLI-only coordination access rule reaches the render-role
  agent surface (DOD2).** New `schema.coordination_access` declares
  `rule=coordination_access_via_greatminds_cli_only`,
  `forbidden=raw_ls_cat_grep_sed_edit_on_coordination`, and the
  canonical CLI surfaces. `cli/render_role.py` appends the rendered
  rule after the role body so `greatminds render-role <ROLE>` carries
  it for every role — every driven turn and every `start-agent`
  injects the rule into the prompt. iter-1 wired it only to
  `role_contract.render_contract` which the agent path never calls;
  iter-2 closes that wiring blind spot.

- **0338 `greatminds journal tail` — read-only filterable view of
  `coordination/journal.ndjson` (DOD2).** `-n N` (default 20),
  `--role R` matches both the `actor` and `role` fields
  (case-insensitive), `--task T` exact / prefix / leading-zero seq
  (`0338` matches `0338-foo`), `--project-dir`. Read-only: opens
  for reading only; bad JSON lines skipped; missing journal → clear
  error.

- **0339 `greatminds agent status` — per-role pid / alive /
  session_id / venv / heartbeat_age / input_sock (DOD2).** Replaces
  raw `cat .agent_registry/<role>.json` with a clean contract
  surface. Alive via `os.kill(pid, 0)` (PermissionError treated as
  alive — never report a healthy holder dead). Venv read from
  `/proc/<pid>/environ` of the live process (dead pid / non-Linux
  → null). `--json` for machine-readable consumers. No-arg lists
  every registered role; `<ROLE>` returns one (unregistered still
  emits a stable not-registered record).

- **0342 `greatminds stand reclaim` — TTL+holder-alive reaper for
  expired singleton leases.** STAND-KEEPER / ARCHITECT-PLANNER only.
  Frees a lease only when BOTH the lease is past `ttl_seconds` AND
  the holder agent is dead/absent (registry pid via `os.kill(pid, 0)`;
  absent registry → not alive). Conservative: unparseable ttl or
  PermissionError → NOT reclaimable. Refuses (exit 3) for no lease /
  in-TTL / holder-alive / non-SK/PLANNER caller / mismatched
  `--lease-id`. Closes the dead-holder + expired-lease permanent
  singleton lock.

### Changed

- **0326 unify `greatminds task` subcommand id intake (DOD2).**
  `find_task` now accepts short id, full filename, and absolute /
  cwd-relative / coordination-relative path in addition to the
  existing forms; every id-taking subcommand resolves the same forms
  with one error shape (`task <X> not found`). `task validate` gains
  a positional ID (with `--id` / `--file` back-compat). `task paths`
  gains an optional ID printing that task's queue + path; bare
  `task paths` still prints global coordination paths. `show / mv /
  append-block` inherit the new forms via `find_task`.

- **0330 per-lifecycle heartbeat stale thresholds for non-continuous
  roles (Phase 5 of 0311).** Watchdog used a single 600s window for
  every role, so alive-but-idle self-loop / driven / interactive
  roles were falsely flagged stale while their pids were live.
  `schema.watchdog.heartbeat_stale_seconds_by_lifecycle` now widens
  the window per lifecycle: `self-loop: 4200` (MAINTAINER ~1h cadence
  + margin), `driven: 14400` (4h — workers idle between events),
  `interactive: 86400` (24h — human-paced). Global 600s retained as
  continuous-signal default; explicit per-role override still wins.
  Dead-pid registry scan remains the authoritative event-driven
  liveness signal — a dead pid is still flagged regardless of
  heartbeat age.

- **0332 codex 0.135 profile-v2 — `greatminds setup` emits the
  per-role codex split layout (Phase 5 of 0311).** codex 0.135.0
  rejects `--profile <role>` when `CODEX_HOME/config.toml` carries a
  `[profiles.<role>]` table or top-level `profile=` selector. Setup
  now splits the shipped `codex/profiles/<role>.config.toml` at the
  `[profiles.<role>]` marker — everything before becomes the base
  `config.toml`; the keys after become a sibling `<role>.config.toml`
  layer. `start_agent.py --profile <role>` unchanged (reads the
  layer); idempotent re-run preserves an operator-edited base.

- **0333 canon forbids TESTER local execution + `uv run --active`
  fleet-wide.** Root-cause fix for the recurring `.venv-coord`
  editable contamination (fleet-wide `ModuleNotFoundError:
  greatminds` after worktree prune). COORDINATE.md §12.5: the
  cd-into-worktree line no longer names TESTER nor says
  editing/testing; implementers cd in to EDIT only; TESTER does NOT
  edit or execute in the worktree — its only execution surface is
  SSH probes against the deployed stand after STAND-KEEPER rsync.
  Fleet-wide ban: `uv run` / `uv run --active` is forbidden for
  every role anywhere in the repo (hijacks the active venv →
  dangling `.pth` on prune). `schema.yaml`
  `TESTER.forbidden_actions += run_local_tests,
  uv_run_or_active_against_fleet_venv` (machine-readable; rides the
  role-contract render).

- **0336 revert 0331 — restore the EXPLORER stand-only contract
  (Phase 5 of 0311).** 0331 inverted EXPLORER: it forbade the stand
  host and forced local CLI/REST-only ("black-box"), the opposite
  of EXPLORER's role (operate ON the live system as a real user).
  Surgical removal of the 0331 schema / canon / template / test
  additions. New "Stand anchor (absolute, non-negotiable)" section
  in EXPLORER.md codifies that EXPLORER always operates ON THE STAND
  with whatever access shape the product carries (HTTP / SSH /
  wherever it is actually deployed); host-destructive + recovery
  on the disposable stand; off-stand and local substitutes
  strictly forbidden. `test_explorer_stand_anchor_0336.py` pins
  the anchor.

- **0340 DOD2 full reactive-fleet documentation refresh.** New
  `docs/architecture/lifecycle.md` (lifecycle × tool matrix +
  recovery chain) and `docs/operations/runbook.md` (CLI-only
  discipline, fleet-venv direct-binary launch, court-fix /
  venv-recovery / wake-SK / diagnostics). Updates across
  `docs/architecture/daemon-and-agents`,
  `docs/cli-reference/index`, `docs/concepts/{inbox, queues, roles,
  scenarios, stand-operations}`, `docs/index`,
  `docs/recipes/e2e-testing`, and `mkdocs.yml`. Canon
  `src/greatminds/data/COORDINATE.md` §12 migrated to stand-lease
  wording; new root `COORDINATE.md` publication mirror; eight role
  canons migrated from `stand_request` / `evidence_for` to
  `greatminds stand lease` / `stand release`.

### Fixed

- **0341 coordd watches `.stand` up front so lifecycle changes wake
  STAND-KEEPER.** Root cause for "stand transitions don't wake
  STAND-KEEPER without a manual nudge":
  `_InotifyWatcher._add_initial_watches` skipped any dir absent at
  startup, but `.stand/` is created lazily by the first stand lease.
  When coordd started BEFORE any lease (fresh deploy / restart —
  the common case), the `.stand` watch never attached. coordd now
  creates `.stand` up front and attaches its watch even when absent
  at startup; the first `state.yaml` write surfaces a routed `.stand`
  event like any other queue→owner event. With SK now mode=driven a
  `.stand` event runs an SK driven turn via the existing driven
  branch.

- **0343 freeing the singleton auto-promotes the next FIFO queue
  entry.** The `stand release` docstring promised "pops the next
  FIFO queue entry" but freeing the singleton did NOT promote the
  head; the stand sat free with pending queued leases never
  auto-activating. New `promote_head_on_free(state, by_role)` helper
  pops the head, grants it (`granted_at` now, `ready_at=None`), sets
  `active_lease`, drains the head from the queue, clears
  `down_reason`, records `free→preparing`. Wired into `stand release`
  (active→free) and `stand up` (down→free) AFTER their →free
  record_transition; `stand down` intentionally does NOT promote
  (incident halt).

## 1.3.12 — 2026-06-01

Closes the 0311 reactive-fleet umbrella (Phases 1–4): every role
declares a lifecycle in schema; MAINTAINER is a non-user-facing
self-loop watchdog with auto_mode-allowed recovery commands; all
claude + codex workers are coordd-driven (one turn per event over
`claude -p`/`--resume` or a fresh `codex app-server` stdio); canon
prose + public mkdocs site mirror the model.

### Added

- **0312 schema role contracts gain per-role lifecycle field (Phase 1a).**
  `schema.roles.<ROLE>.lifecycle` ∈ {`interactive`, `self-loop`,
  `driven`}. PLANNER + USER interactive; MAINTAINER self-loop;
  8 workers + BOT-\* driven. `greatminds role contract <ROLE>` renders
  the line.

- **0313 MAINTAINER recovery commands in `auto_mode.allow` (Phase 1c).**
  Schema `claude_settings.auto_mode.allow` gains `greatminds restart/
  daemon/start-agent`, `kill`, and `systemctl --user` patterns so
  MAINTAINER's self-loop recovery survives the classifier ceiling
  without a USER present.

- **0314 MAINTAINER self-loop watchdog tick (Phase 1b).** Flips
  MAINTAINER from chat to a self-loop health-check tick that
  auto-restarts dead workers and coordd and escalates queue/FSM
  stalls + upstream-bug candidates to PLANNER. Non-user-facing —
  USER reaches infra topics through PLANNER. `data/command_START.yaml`
  + `coord.yaml.template` + `MAINTAINER.md` reframed; recovery
  commands are already allow-listed.

- **0315 coordd driver core spawns `claude --resume` for driven roles
  (Phase 2a).** For lifecycle=driven + tool=claude coordd RUNS each
  turn via `claude --resume <sid> -p "continue your tick"` instead of
  waking a persistent agent. Pane is idle bash between turns; per-role
  run-lock prevents overlapping turns and sets a pending marker for
  mid-turn events; `_route_queue_event` falls through to legacy wake
  on missing session id / non-driven / non-claude.

- **0316 role contract via `--append-system-prompt-file` (Phase 2b).**
  Each driven turn is a fresh `claude -p` call, so the role contract
  lives in the system prompt: `setup._seed_role_bootstraps` writes
  `coordination/.bootstrap/<role>.md` from the existing `render_role`
  for every role; the 0315 driver passes the file as
  `--append-system-prompt-file`.

- **0317 driven driver session-reset policy at turn threshold (Phase
  2c).** `SESSION_RESET_TURN_THRESHOLD` (default 50, env override
  `COORDD_SESSION_RESET_TURNS`). At/above the threshold the next turn
  starts fresh (no `--resume`) with the bootstrap still riding, and
  the per-role `driven_turn_count` resets to 1. Caps `claude --resume`
  history growth.

- **0318 migrate READER to driven lifecycle (Phase 2d — first low-risk
  pilot).** `coord.yaml.template` reader window mode loop → driven,
  bootstrap launch /loop → driven. `_route_queue_event` driven path
  now also checks coord.yaml window mode (lifecycle=driven AND
  tool=claude AND window_mode=driven); missing session_id forces a
  fresh first turn instead of falling back to wake.

- **0319 migrate DEVELOPER + UI-DEVELOPER + TESTER + STAND-KEEPER to
  driven (Phase 2e).** Batches the remaining four claude workers
  onto the driven driver. PLANNER (interactive) + MAINTAINER
  (self-loop) + codex roles unchanged.

- **0320 codex app-server as managed `systemd --user` unit (Phase
  3a).** `greatminds-appserver@<fleet>.service` template carries an
  absolute-node ExecStart + `Environment=PATH` that leads with the
  node bin dir, so codex's `#!/usr/bin/env node` shebang resolves
  under systemd's minimal PATH. `greatminds daemon install` enables
  the unit when the fleet has driven+codex roles. (The driver in
  0321 ultimately uses stdio per turn rather than the WS socket;
  the unit is retained as harmless infrastructure for now.)

- **0321 coordd codex driver via stdio app-server per-turn (Phase
  3b).** For lifecycle=driven + tool=codex coordd spawns a fresh
  `codex app-server` over stdio per event and speaks the
  line-delimited JSON-RPC (`initialize` →
  `thread/start | thread/resume` → `turn/start` → wait
  `turn/completed` → exit). Per-role run-lock + pending marker;
  async daemon thread releases the lock on completion and re-fires
  one pending event. Pane is idle bash between turns.

- **0323 migrate EXPLORER (first codex worker) to driven (Phase 3c).**
  `coord.yaml.template` explorer window mode loop → driven; bootstrap
  launch /loop → driven with codex stdio-per-turn wording. The
  generic codex driver (0321) routes the new dispatch with no coordd
  code change.

- **0324 migrate TECHNICAL-WRITER (last codex worker) to driven
  (Phase 3d).** Same config-only flip as 0323. With this both codex
  workers run driven; ARCHITECT-REVIEWER stays non-driven for now.

### Changed

- **0325 canon docs for the reactive-fleet lifecycle model (Phase 4).**
  `COORDINATE.md` §2.1 (new) carries the lifecycle vocabulary + the
  lifecycle × tool dispatch matrix + the systemd → coordd → MAINTAINER
  → worker/coordd/PLANNER escalation chain. Per-role canon files gain
  a Runtime lifecycle section; ARCHITECT-REVIEWER explicitly carries
  the legacy-launch fallback wording. Public mkdocs pages extend the
  window mode enum to `chat|loop|driven`, bifurcate the coordd wake
  vs driven-spawn paths, annotate every role with its lifecycle, and
  update the permissions paragraph from "loop-mode role" to "driven or
  self-loop role".

### Operator migration

- Pre-1.3.12 fleets need a one-time `greatminds setup` regen against
  their existing project (or hand-edit `coord.yaml` window modes for
  the affected roles) before the driven dispatch takes effect — setup
  never overwrites an existing `coord.yaml`. Run `greatminds daemon
  repair --project <name>` if the systemd `--user` unit was installed
  pre-1.3.11.

## 1.3.11 — 2026-06-01

Eight verified fixes covering the REVIEWER merge direction, the
schema/gate-check stand_evidence contract, dispatch/lease coherence,
worktree-isolation enforcement at the CLI, uniform session-start
canon-read across role canons, SK heartbeat refresh during long
ansible runs, daemon install systemd enablement + repair subcommand,
and the launch/restart wrapper-loop removal.

### Added

- **0303 task append-block enforces cwd under per-task worktree.**
  `schema.worktrees.required_for_task_kinds` was declarative only;
  implementers could silently edit main while filing impl/tests blocks
  (TESTER's lease then rsync'd a stale worktree, multiple stand_downs).
  New `_enforce_worktree_isolation_for_block` runs at append-block
  entry — refuses with exit_code=2 + the `cd "$(greatminds worktree
  path <id>)"` recipe + `GREATMINDS_SKIP_WORKTREE_CHECK=1` escape
  hatch. Skip cases preserve current flows (non-code blocks,
  docs/research tasks, env-var override).

- **0304 uniform session-start canon-read block across 8 role canons.**
  Only MAINTAINER had explicit canon-read guidance at session start;
  fresh-install agents for the other 8 roles could (and did) skip
  re-reading COORDINATE.md / schema. Each of DEVELOPER, UI-DEVELOPER,
  TESTER, READER, TECHNICAL-WRITER, STAND-KEEPER, EXPLORER,
  BOT-DEVELOPER now carries a uniform `## Session start (0304)`
  section + inline-invariants block. MAINTAINER.md intentionally
  untouched; the existing Does step 1 pattern is regression-locked.

- **0307 daemon install enables unit + `greatminds daemon repair`.**
  `greatminds daemon install` now runs `systemctl --user enable`
  for the resolved template instance so the symlink lands under
  `default.target.wants/` and the unit auto-starts after logout /
  shutdown. New `greatminds daemon repair --project <name>` is the
  one-shot fix for existing pre-fix fleets — stricter than install
  (nonzero exit propagates because enable is the whole point).

### Fixed

- **0300 worktree_merge pulls origin/main before merging task
  branch.** Pins the REVIEWER verified-merge direction. The current
  code already did checkout main → merge task/<id> correctly; this
  adds the regression-net tests pinning the order plus the missing
  `git pull --ff-only origin main` step between checkout and merge so
  main advances when origin is strictly ahead.

- **0301 stand_evidence required_subfields list matches gate_check
  expectations.** `schema.tests_block_validation.stand_evidence.
  required_subfields` listed 3 prose fields but gate_check also
  required `lease_id`, `result`, `commit` — every well-formed lease
  release hit "missing" at the feature_test → feature_review gate.
  Schema now enumerates all six; `cli/task.py` validator reads from
  schema with a defensive 3-field fallback for partial installs.

- **0302 dispatch_profile cross-checks `spec.name` against
  `lease_meta['profile']`.** Pre-fix SK could silently run a
  spec-loaded-by-other-means against an unrelated lease.
  `dispatch_profile` now refuses with exit_code=2 +
  GreatMindsError naming both values before any subprocess so a
  misrouted dispatch can never invoke the wrong playbook.

- **0305 SK heartbeat refresh during long ansible runs (Fix B).**
  Watchdog dead-pid asks were accumulating while SK was waiting on
  multi-minute ansible deploys. New `_start_heartbeat_refresher`
  daemon thread touches `heartbeat.<role>` every 30s during the
  subprocess; `execute_yaml_profile` wraps `subprocess.run` in
  try/finally so the refresher stops on every exit path. Fix A
  (coordd wake on `state.yaml` writes) was already in production.

- **0308 launch sends `greatminds start-agent` directly + restart
  mirrors the same sequence.** Wrapper-loop install path replaced
  with `tmux send-keys C-u` + the launch command + Enter on each
  pane; restart uses the same shared builder so launch and resurrect
  share one code path. `_wrapper_loop` + `CIRCUIT_BREAKER_*`
  retained as dormant symbols for legacy fixtures; the wrapper's
  built-in counter is removed (failing agents retry naturally on
  the next restart; a watchdog-side counter lands as a follow-up).

## 1.3.10 — 2026-05-27

Fixes two operator-facing bugs in `greatminds update` so the command
works correctly on projects managed by uv / poetry / pipenv and stops
resurrecting torn-down fleet sessions.

### Fixed

- **0299 update branches by `detect_env_setup` + skips fleet restart
  when tmux session absent.** `update` used to call pip even when the
  project lived under uv/poetry/pipenv, installing into the wrong env
  layer and then losing on the uv-lock mismatch on the next install.
  The pip-step is now replaced by a `detect_env_setup` dispatch:
  - `env_type=uv` → `uv lock --upgrade-package greatminds && uv sync`
  - `env_type=poetry` → `poetry update greatminds`
  - `env_type=pipenv` → `pipenv update greatminds`
  - `env_type=conda` or fallback `None` → `<py> -m pip install …`

  The fleet-restart step also called tmux send-keys unconditionally,
  resurrecting state on hosts where the operator had torn the tmux
  down. `_step_restart_agents` now gates on
  `_tmux_session_present(name)` (reads session name from
  `coord.yaml`, checks tmux PATH + `has-session` rc, swallows
  TimeoutExpired); when the session is absent it logs a skip-info
  and returns without touching tmux.

## 1.3.9 — 2026-05-27

Critical fix for the chat-mode UserPromptSubmit deadlock that
shipped across the 1.3.x line. Operators on existing projects should
upgrade promptly.

### Fixed

- **0298 stop-decide `user-prompt-submit` phase no longer emits
  `decision: block`.** `greatminds stop-decide` returned the same
  `{decision: block, reason, systemMessage}` payload for both
  `phase=stop` (correct — Stop hook drains inbox between turns) and
  `phase=user-prompt-submit` (catastrophic — the UserPromptSubmit hook
  with `decision: block` rejects EVERY USER prompt, leaving chat-mode
  roles like PLANNER and MAINTAINER unreachable until an operator
  hand-edits the inbox directory). `stop_decide.py` now branches on
  phase: `user-prompt-submit` emits only `{"systemMessage": msg}` so
  the informational notice surfaces but USER's prompt passes through;
  `phase=stop` unchanged. Test assertions in
  `test_user_prompt_submit_hook_0236.py` that pinned the bug were
  rewritten to the post-0298 contract.

## 1.3.8 — 2026-05-27

Adds PLANNER's machine-readable contract for the stand-profile
mechanism so fresh-install PLANNER agents pick up the workflow at
tick start without an operator ping. Closes the role-contract gap
left after the 0276 stand-profile umbrella shipped.

### Added

- **0297 PLANNER role contract gains stand-profile coordination.**
  `schema.roles.ARCHITECT-PLANNER` now declares
  `coordinate_stand_profile_tasks_via_schema_and_canon` +
  `file_schema_extension_task_on_lease_enum_block` responsibilities
  and three new `event_triggers`:
  `on_stand_down_yaml_playbook_error` (file YAML bugfix from SK's
  inbox-info), `on_stand_down_md_interpretation_error` (same flow for
  MD prose), `on_stand_lease_enum_block` (file schema-extension task
  before the dependent lease proceeds). `ARCHITECT-PLANNER.md` carries
  a short cross-reference paragraph — no duplicate prose.

## 1.3.7 — 2026-05-27

Closes the 0276 stand-profile umbrella DoD with the MD-cycle live-green
on real avatar.

### Added

- **0295 liveness-prose MD-only canon template (Phase I of 0276).**
  New `data/templates/stand-profiles/liveness-prose.md` with no YAML
  twin so `load_profile` resolves to `format='md'` and SK dispatches
  through `execute_md_profile` (not `execute_yaml_profile`). Frontmatter
  declares `deploy_prerequisites_only: false`; body references
  `${host}`, `${user}`, `${deploy_path}`, `${task_id}`, `${lease_id}`
  to exercise the substitution path on a real lease. Tests pin the
  NO-YAML-TWIN invariant as a regression net so a future yaml file with
  the same stem can't silently short-circuit the MD path.

## 1.3.6 — 2026-05-27

Closes the 0276 stand-profile umbrella with the live-integration phase
plus a chain of canon-playbook robustness fixes empirically proven on
real avatar deploys, and pushes the schema enum so md-only profiles can
be leased.

### Added

- **0284 end-to-end stand-profile cycle tests + collection-free rsync
  (Phase H of 0276).** New `test_stand_profile_end_to_end_0284.py`
  exercises the YAML cycle (loader → dispatch argv → prereq tag) and
  the MD cycle (loader → `${var}` substitution → `PREREQ_ONLY_NOTICE`
  injection) plus a real `ansible-playbook --syntax-check` against the
  seeded canon playbook. The canon `full-deploy.yaml` `synchronize`
  task is replaced with `delegate_to: localhost` +
  `ansible.builtin.command: rsync …`, dropping the
  `ansible.posix` collection requirement.

- **0291 SK `stand down` auto-notifies PLANNER inbox.** New
  `schema.stand_keeper.notifications` map (`on_down: ARCHITECT-PLANNER`).
  `stand_down` mutator captures the active lease's `task` + `lease_id`,
  files `"stand down: <reason> (lease_id=<id>)"` to PLANNER's inbox
  with `task_ref` populated. PLANNER no longer polls `state.yaml` to
  discover incidents.

- **0296 schema `stand.resource.profiles_allowed` += `liveness-prose`.**
  Enum extended and the canon `liveness-prose.md` template shipped in
  the same commit so md-only profiles can be leased without an enum
  rejection.

### Fixed

- **0292 `full-deploy.yaml` install step uses `uv pip`.** `uv venv`
  does not install pip, so `.venv-coord/bin/pip` failed rc=2 on every
  fresh deploy. Replacement uses `uv pip install --python
  .venv-coord/bin/python --force-reinstall …` — no pip-in-venv
  required.

- **0293 `full-deploy.yaml` pre-build wheel cleanup avoids glob
  collision.** Second-or-later deploys left a prior wheel in `dist/`,
  causing `uv pip install dist/greatminds-*.whl` to refuse with
  "ambiguous glob". New pre-build `ansible.builtin.shell: rm -f
  dist/greatminds-*.whl` task ordered before `uv build` so the install
  glob always resolves to exactly the freshly-built wheel.

## 1.3.5 — 2026-05-27

Adds schema-driven role contracts so any agent can read its own
workflow from `schema.yaml` at tick start, and plugs the orphan
`active_lease` leak that produced `state=down + active_lease={...}`
contradictions across the stand transitions.

### Added

- **0288 schema-driven role contracts for all 13 roles + CLI.**
  `schema.roles.<ROLE>` now carries `responsibilities`,
  `forbidden_actions`, and (for product roles) `event_triggers`
  (`on_<event>` → ordered step list). Steps are short verbs —
  CLI commands like `stand_lease` / `stand_ready` /
  `mv_to_feature_review`, or logical placeholders the LLM
  resolves. New `greatminds role contract <ROLE>` + `greatminds
  role list` CLI surface the rendered contract. Existing
  `roles/*.md` prose remains; doc-shrinkage follow-up deferred.

### Fixed

- **0289 stand release/down/up nullify `active_lease`.** Pre-fix
  `stand down` left the triggering lease set, and `stand up` did
  not defensively clean older state files — producing
  `state=down + active_lease={...}` contradictions visible in
  `stand status`. Mutators now set `active_lease=None` on both
  transitions alongside their existing `down_reason` mutations;
  `stand release` already cleared correctly and is now
  regression-locked.

## 1.3.4 — 2026-05-27

Followup patch tightening the SK runtime gate so the stand-profile
dispatch contract is actually enforced.

### Fixed

- **0286 three-layer deploy lock.** SK could still mark state=ready
  without invoking ansible-playbook because the `is_deploy_safe`
  classifier landed in 1.3.3 had no callsites in the runtime path.
  Three independent layers now enforce the contract end-to-end:
  (1) `execute_yaml_profile` gates on `is_deploy_safe` at top of
  function — unsafe → rc=126, no subprocess, reason captured in
  marker. (2) Both executors drop a per-lease marker at
  `<coord>/.stand/deploy-<lease_id>.log`; timeouts (124),
  FileNotFoundError (127), and refusals (126) all record markers so
  failure modes surface via rc, not "no evidence". (3) `greatminds
  stand ready --lease-id` refuses with exit_code=2 + actionable
  message when the marker is absent. STAND-KEEPER.md §2 codifies the
  `dispatch_profile` MUST-precede contract.

## 1.3.3 — 2026-05-27

Stand-profile mechanism (0276 umbrella, Phases A-G) plus a deploy-safety
fix that unblocks Phase H avatar verification.

### Added

- **0277 stand_profile schema section + canon convention (Phase A).**
  `schema.stand_profile` declares directory
  (`coordination/stand-profiles`), formats (yaml/md), lookup pattern,
  dialects (ansible-playbook subset / free prose), yaml required +
  optional fields, and `deploy_prerequisites_only_flag`. COORDINATE.md
  §8.1 documents file-name convention, YAML-wins-on-conflict, and
  references `schema.stand_profile` as source of truth.

- **0278 stand_profile loader/parser (Phase B).** New
  `cli/stand_profile.py` exposes `ProfileSpec` + `load_profile` +
  `profile_paths`. Lookup precedence: yaml → md → error naming both
  paths. `deploy_prerequisites_only` extracted uniformly from
  `yaml.vars` or md frontmatter. Malformed MD frontmatter silently
  falls back to full body.

- **0279 SK execution path — YAML→ansible-playbook + MD→prose (Phase
  C).** `cli/stand_executor.py` adds `execute_yaml_profile`,
  `execute_md_profile`, and `dispatch_profile`. YAML synthesizes
  inventory + extra-vars (via `@<json-file>` so shell metacharacters
  survive) and shells out to `ansible-playbook`. MD substitutes
  `${var}` via `string.Template.safe_substitute` (unknown vars stay
  literal). STAND-KEEPER.md §Does Step 2 extended.

- **0280 ansible-core hard dep (Phase D).** `pyproject.toml` pins
  `ansible-core>=2.16,<2.18`; `setup` runs a sanity check at the end
  of its run that warns (but does not abort) if ansible-playbook is
  missing — MD-only operators stay unblocked.

- **0281 full-deploy + smoke-only presets (Phase E).** Four canon
  preset files under `data/templates/stand-profiles/` (yaml + md for
  each). `setup._seed_stand_profiles` copies them into
  `<coord>/stand-profiles/` idempotently. Loader side-fix:
  `_load_yaml_profile` now accepts both list-of-plays (real ansible
  playbook) and mapping (single-play short-hand) at the top level.

- **0282 canon updates — STAND-KEEPER + TESTER + COORDINATE.md (Phase
  F).** STAND-KEEPER workflow anchors on `load_profile + dispatch`,
  success/failure via `stand ready/down`, `deploy_prerequisites_only`
  semantics. TESTER scope tightens to probe-only with a
  deploy-pipeline carve-out. COORDINATE.md §8.1 publishes profile
  ownership, consumers, format choice.

- **0283 `deploy_prerequisites_only` flag end-to-end (Phase G).**
  `greatminds stand lease --deploy-prerequisites-only` persists the
  flag into `active_lease.deploy_prerequisites_only` only when True
  (minimal-state pin). YAML executor appends `--tags prerequisite`
  on the resolved value; MD executor prepends `PREREQ_ONLY_NOTICE`
  banner so SK's LLM sees the mode switch before the recipe.
  Lease-level override wins over spec value.

### Fixed

- **0285 SK deploy-bypass closed — `is_deploy_safe` classifier.** SK
  was refusing every deploy that touched the main fleet tree, even
  when the lease pointed at a per-task `.worktrees/<seq>/` worktree
  OR named a remote `STAND_HOST`; state short-circuited to ready
  without `ansible-playbook` executing. New
  `is_deploy_safe(worktree, host, project_dir)` classifier resolves
  three branches: isolated worktree always safe, main-tree +
  local-host unsafe (self-modify), main-tree + remote-host safe.
  `LOCAL_HOSTS` set + host normalization so PROJECT.env strings
  classify cleanly. Lease mutator also clears stale `down_reason` on
  free→preparing so prior incidents don't poison subsequent leases.

## 1.3.2 — 2026-05-27

Followup cut after the 1.3.0 BREAKING stand-stream redesign + 1.3.1
emergency wake fix. Five verified tasks merged on local main; all
empirically validated via real avatar SSH stand probes (no shape-only
evidence).

### Fixed

- **0258 — complete BREAKING removal of stand-stream runtime.**
  CLI now rejects `--stream stand` and `--kind stand_request` with
  rc=2 + `stream=stand removed in 1.3.0` message; `greatminds setup`
  no longer scaffolds `stand_{requests,wip,done}/` directories;
  `coordd._build_inotify_dirs` drops the legacy queue watches; new
  `greatminds migrate-stand-history` CLI moves pre-1.3.0
  `stand_done/*` artifacts under `coordination/archive/stand-history/`
  (idempotent, supports `--dry-run`).

- **0268 — `_evaluate_gate_check` reads lease evidence first.**
  Pre-fix, a lease-evidence-carrying task could see `gate-check`
  return `pass` from the lease while `_check_gate_for_stand_required`
  returned `missing` from the same task data (the latter only knew
  about removed `stand_done/<id>.yaml` files). Now
  `extract_lease_evidence_from_tests` is probed first; the legacy
  `find_stand_evidence` path remains as a fall-through for any
  in-flight pre-migration tasks.

- **0267 — `greatminds setup` bakes `autoMode.allow` + ops perms from
  schema canon.** `data/schema.yaml` now declares the full
  `claude_settings.autoMode.allow` (`Bash(git push origin main:*)` +
  `--follow-tags` variants) and `claude_settings.permissions.allow`
  (ssh / scp / rsync / git revert) under one source of truth.
  `_build_settings_local_json` populates `autoMode.allow` from schema
  instead of a hardcoded list; `_ensure_claude_settings_local`
  additively merges new canonical entries on existing fleets — the
  operator's own additions and Stop/UserPromptSubmit hooks are
  preserved; repeat runs report `unchanged`.

- **0269 — coordd inotify `.stand` events route to STAND-KEEPER.**
  `.stand` was an `INOTIFY_QUEUE_DIRS` entry but not listed in
  `schema.queues`, so `_owning_role_for_queue('.stand')` returned
  `None` and `_route_queue_event` silently dropped state.yaml writes.
  Schema now declares `.stand` with `owner: STAND-KEEPER`,
  `writers: [STAND-KEEPER, TESTER, ARCHITECT-PLANNER, MAINTAINER]`,
  `kind: state`. Lease delivery latency drops from waiting on SK's
  own ScheduleWakeup tick to coordd-pushed via `press_enter` on the
  input_sock. The `kind: state` marker keeps `watchdog` (active-only)
  and `wake_check` (terminal-only) iterations untouched.

- **0271 — `greatminds stand lease` enforces per-task worktree at
  acquire.** Pre-fix the only worktree enforcement was SK's runtime
  whitelist; the CLI accepted any `--worktree`, flipped state.yaml to
  `preparing`, and only SK rejected later with a self-modify reason,
  leaving an orphaned lease. Now schema declares
  `stand.resource.lease.worktree_constraint` (pattern
  `{project_dir}/.worktrees/{seq}[-slug]`, `enforced_by: cli`) and
  `stand.py:_validate_lease_worktree` rejects at acquire with rc=2 +
  named-rule errors: empty/None, main-tree (with paste-ready
  `git worktree add` recipe), wrong parent, wrong basename. Relative
  paths resolve via `Path(...).resolve(strict=False)`. state.yaml is
  not mutated on reject.

## 1.3.1 — 2026-05-26

Emergency cut. Single fix: 0259 — coordd's chat-mode inotify wake
now reaches the claude TUI input handler.

§9.1 self-blocker carve-out: the fix's deployment IS the
verification mechanism (gate_check on 0259 was irreducibly blocked
because SK couldn't deploy 0259 to verify itself — the broken wake
channel was the verification channel). Same pattern as the 1.2.6
cut. Empirical evidence recorded inline in 0259's tests block:
direct press_enter probe on READER returned `ok=True` with
input_sock channel + leaf SIGINT; end-to-end probe (filed
feature_inbox/9999.yaml → coordd inotify → press_enter →
input_sock → SIGINT to PLANNER's own Bash subprocess exit 130)
confirms the full chain.

### Fixed

- **0259 coordd inotify wake uses `input_sock` via `press_enter`.**
  Pre-0259, coordd's chat-mode wake (`tmux_send_keys_wake`) sent
  bracketed-paste text + Enter via `tmux send-keys`, but the TUI
  input handler on claude panes intermittently never received the
  submit (visible prompt, no turn-fire). Replaced with
  `press_enter` writing the wake payload to the role's
  pty-tracked `input_sock` and SIGINTing the leaf=node descendant
  — same channel that DEV/OPERATOR keystrokes flow through.
- **`_send_enter.py` alignment with `coordd.WAKE_*` constants
  (PLANNER follow-up).** `_WAKE_GAP_S` 0.2 → 0.35 (mirrors
  `coordd.WAKE_GAP_SECONDS`, production-proven via
  `push_to_role`). `_KEY_TO_BYTES['Enter']` `b'\r'` → `b'\r\n'`
  (mirrors `coordd.WAKE_ENTER` CRLF; bare CR fails claude TUI
  submit detection intermittently).

### Removed

- **`coordd.tmux_send_keys_wake` function** and its
  `_LAST_TMUX_NUDGE` rate-limit table + `_read_event_wake_schema`
  helper. All chat-mode wakes now flow through `press_enter`.

### Upgrade

```bash
pip install --upgrade greatminds==1.3.1
greatminds restart --bootstrap
```

## 1.3.0 — 2026-05-26

**BREAKING.** Stand-stream redesign: the legacy three-queue model
(`stand_requests/` → `stand_wip/` → `stand_done/`) is replaced by a
lease-based singleton stand resource backed by
`coordination/.stand/state.yaml`. Operators upgrading from 1.2.x must
drain any in-flight `stand_requests/*` and `stand_wip/*` before
`pip install --upgrade greatminds==1.3.0`; existing `stand_done/*`
files may stay as historical evidence.

### Breaking

- **Old stand queues removed.** `stand_requests/`, `stand_wip/`,
  `stand_done/` queue directories are no longer scaffolded by
  `greatminds setup`. All transitions touching them are gone from
  `schema.yaml`, along with the `stand` task stream and four
  stand-stream validators in `cli/task.py`.
- **`greatminds stand request` / `stand result` CLI removed.**
  Replaced by the lease API below. `greatminds task new --stream stand`
  raises with a pointer at the new CLI.
- **`gate_check` reads lease evidence exclusively.** The backwards-
  compatibility fallback to `find_stand_evidence` (stand_done scan)
  is gone. Pre-1.3.0 tasks without a `lease_id` on their tests block
  return `missing`; refile via the lease API to verify.

### Added

- **Lease-based stand CLI:**
  - `greatminds stand lease --task <id> --worktree <path> --profile
    <enum>` — request a lease; returns a UUID `lease_id`. FIFO queue
    when the stand is busy.
  - `greatminds stand release --lease-id <id> --result
    pass|fail|partial` — return the stand to free; result is a
    closed enum (no prose channel).
  - `greatminds stand ready --lease-id <id>` — SK signals deploy
    complete; state preparing→ready; inbox-info to holder.
  - `greatminds stand down --reason …` / `stand up --reason …` —
    halt/recovery; queue paused under `down`.
  - `greatminds stand status` — read-only state + queue + history-
    tail.
- **`coordination/.stand/state.yaml`** singleton state file with
  fcntl-protected I/O. Four states (`free` / `preparing` / `ready`
  / `down`) and transitions encoded in `schema.yaml stand.resource`.
- **`coordd` inotify on `.stand/`** so SK reacts sub-second to state
  changes instead of polling.
- **Role canon (`STAND-KEEPER.md`, `TESTER.md`, `EXPLORER.md`)**
  rewritten for the lease lifecycle. SK input is structured-only
  (`--task`, `--worktree`, `--profile`); SK never sees `what to test`
  by design (information asymmetry forces TESTER ownership of
  probes).
- **Public docs:** new `docs/concepts/stand-operations.md` page,
  `docs/concepts/stand-gate.md` updated for lease evidence,
  `mkdocs.yml` nav entry, `docs/architecture/filesystem-layout.md`
  reflects `coordination/.stand/state.yaml`.

### Companion changes (1.2.x → 1.3.0 batch)

- **Commit-drift closure** (0228 / 0229 / 0233): TESTER own
  functional_probes + tester_observations are now required on a
  scope-driven schema gate; `gate_check` records a worktree
  fingerprint to distinguish committed vs in-flight overlays;
  `greatminds stand request` (now removed in 1.3.0) resolved
  target_commit from `evidence_for[0]`'s impl block — superseded by
  lease `--worktree` semantics.
- **0236 + 0237** UserPromptSubmit hook + tmux send-keys split fix
  for chat-mode panes (PLANNER / MAINTAINER) no longer miss messages
  during USER topic-switches.
- **0238** new `docs/concepts/codex-profiles.md` page covering the
  0158 per-role `CODEX_HOME` model.
- **0241** PLANNER role canon — propose-then-file default chat
  posture, codified.
- **Bugfixes** 0198 / 0202 / 0204 / 0235 + 13 doc tasks (0208 →
  0220) cleared along the way.

### Upgrade

```bash
pip install --upgrade greatminds==1.3.0
greatminds update            # restarts daemon + agents
```

`greatminds update` auto-runs `daemon install` when the per-project
template unit is missing (0202), so legacy pre-0008 fleets upgrade
in a single step.

## 1.1.2 — 2026-05-21

Bug-fix release. Three regressions found in 1.1.0 real-world use, plus a
critical wake-up channel regression introduced silently by the 1.0.0
umbrella-console-script migration. No new features.

### Fixed

- **`task append-block --body-file` option missing.** Dropped during the
  argparse→click conversion. The plan orchestrator and several agent
  prompts still passed it, breaking the orchestrator. Restored.
- **`coerce_value` blanket-split every `--field` value on commas**, so
  any prose value (e.g. `stand_reason="POST /node, then GET /health"`)
  was silently turned into a YAML list, and downstream validators
  choked. Now only fields explicitly in `LIST_FIELDS` are split on
  commas; all other fields stay strings even if they contain commas or
  colons.
- **click `multiple=True` did not accept argparse-style space-separated
  values** (`--hosts X Y` failed with "Got unexpected extra argument
  (Y)"). New `_split_multivalue` callback supports both forms:
  `--hosts X --hosts Y` (repeated flag, idiomatic click) AND
  `--hosts X,Y` (one flag, comma-separated). `stand request` uses it
  consistently.
- **`greatminds-pty-launch` console-script never existed in 1.0.0
  umbrella migration** — `pyproject.toml` only declares `greatminds`,
  so `shutil.which("greatminds-pty-launch")` always returned `None`,
  silently disabling pty wrapping. Result: every agent since 1.0.0
  ran without the pty wrapper → no `input_sock` in `.agent_registry/
  <role>.json` → coordd fell back to writing /dev/pts (slave side =
  display output, NOT input) → wake keystrokes never reached agents,
  who only ticked on their own ScheduleWakeup timer. start_agent now
  invokes pty_launch via `python -m greatminds.cli.pty_launch` (same
  pattern as render-role).
- **`pty_launch.write_registry` was overwriting `session_id`** put
  there by start_agent's pre-pty registry write. Now it MERGES on top
  of any existing record so session_id (and any other downstream key)
  survives the pid/sock enrichment.
- **click strips `--` from variadic args in `pty_launch`'s click
  signature**, breaking claude's `--mcp-config <file> -- PROMPT`
  contract (claude's variadic `--mcp-config` consumes the prompt as a
  config file). The `python -m greatminds.cli.pty_launch` direct-exec
  path now bypasses click for argv parsing so `--` survives into the
  child's argv. The umbrella `greatminds pty-launch` subcommand path
  goes through click and remains affected — but that path is only
  used for diagnostics, not by start_agent.

### Changed (internal — no behavioural change for users)

- Full click-native CLI rewrite per the `guardora_vfl` reference
  pattern. Dropped:
  * 9 `SimpleNamespace` shims that wrapped legacy `cmd_X(args:
    argparse.Namespace)` handlers from the argparse era.
  * 5 `import argparse` statements (`task.py`, `inbox.py`, `stand.py`,
    `coordd.py`, `start_agent.py`).
  * `die(code, msg)` global helper — replaced with direct
    `raise GreatMindsError(msg, exit_code=N)` at every callsite. One
    exception class total (`GreatMindsError(click.ClickException)`),
    no subclass hierarchy.
- Inter-module subprocess calls between `stand.py` / `plan.py` and
  `task.py` replaced with direct Python function imports:
  `create_task()`, `move_task()`, `append_block()`. These are the
  library API; the click handlers are thin wrappers calling them,
  matching vfl's `create_node()` / `create_project()` pattern.

### Added

- `tests/test_task_field_coercion.py` — pytest regression suite
  covering all three 1.1.0 bugs (20 tests, all passing). Tests:
  `stand_reason` with commas/colons stays string; LIST_FIELDS still
  split; `--body-file` works; `--body @PATH` works;
  hosts comma-separated AND repeated-flag forms.
- `core/errors.py` — single `GreatMindsError(click.ClickException)`
  type. Callers pass exit code at the raise site:
  `raise GreatMindsError("bad value", exit_code=2)`.
- CI: pytest runs against the installed wheel after the smoke loop.

## 1.1.0 — 2026-05-21

**Breaking** — canon docs translated to English; env-var namespace
renamed `COORD_*` → `GREATMINDS_*`; new `--lang` option for `greatminds
setup` controls the user-facing language each agent uses while keeping
internal artefacts (task files, journal, code) English.

### Breaking

- All `COORD_*` env vars renamed to `GREATMINDS_*`:
  - `COORD_PROJECT_DIR` → `GREATMINDS_PROJECT_DIR`
  - `COORD_CANON_DIR` → `GREATMINDS_CANON_DIR`
  - `COORD_ROLE` → `GREATMINDS_ROLE`
  - `COORD_FORCE` → `GREATMINDS_FORCE`
  - `COORD_FRESH` → `GREATMINDS_FRESH`
  - `COORD_REGISTRY_TOOL` → `GREATMINDS_REGISTRY_TOOL`
  - `COORD_START_AGENT_SAFE|NOTITLE|NOPTY` → `GREATMINDS_START_AGENT_*`
  - `COORD_CURSOR_MEM_MAX|MEM_HIGH|CPU|MODEL` → `GREATMINDS_CURSOR_*`
  - `COORD_POSTGRES_DSN` → `GREATMINDS_POSTGRES_DSN`
- Canon docs (`COORDINATE.md`, `command_START.yaml`) translated from
  Russian to English. Pre-1.1.0 versions on PyPI (0.1.0, 0.1.1, 1.0.0)
  shipped Russian-only canon by mistake — yank them in favour of 1.1.0+.

### Added

- `greatminds setup --lang <code>` flag — records the agent
  user-facing language in `PROJECT.md` as `<GREATMINDS_LANG>`. Any
  language a chat model speaks works (`en`, `ru`, `zh`, `es`, `fr`,
  `ja`, etc.). Default: `en`.
- Common preamble in `command_START.yaml` instructs every agent to
  communicate with the USER in `<GREATMINDS_LANG>` while keeping
  internal artefacts (task YAML fields, journal entries, commit
  messages, file paths, inbox messages between roles, code) English
  regardless.

## 1.0.0 — 2026-05-21

**Breaking release** — the 19 separate ``greatminds-*`` entry-points
are consolidated into a single ``greatminds`` umbrella with subcommands
(click groups + flat commands). All canon docs and prompts now use
``greatminds X`` syntax instead of ``bin/X``.

### Breaking

- Single entry-point ``greatminds`` replaces the 0.1.x set of 19
  ``greatminds-*`` binaries. Migration:
  - ``greatminds-task list verified`` → ``greatminds task list verified``
  - ``greatminds-inbox send DEVELOPER --kind wake`` → ``greatminds inbox send DEVELOPER --kind wake``
  - ``greatminds-stand request --request-type deploy …`` → ``greatminds stand request --request-type deploy …``
  - ``greatminds-coordd --verbose`` → ``greatminds coordd --verbose``
  - ``greatminds-coord-launch --target tmux`` → ``greatminds launch --target tmux``
  - ``greatminds-coord-init`` → ``greatminds setup``
  - (etc. — all 19 commands now subcommands of ``greatminds``)
- ``coord-init``, ``coord-launch``, ``coord-tmux`` modules removed; their
  functionality is in ``greatminds setup`` and ``greatminds launch``.

### Added

- ``greatminds.core.env`` — adaptive Python-env detector covering 8
  scenarios: pixi, uv, poetry, conda, plain venv, external-venv
  (``$VIRTUAL_ENV``), external-conda (``$CONDA_DEFAULT_ENV``), and
  system fallback.
- ``greatminds launch`` uses the detector to activate the project's
  env in each tmux window or VS Code task automatically — agent
  prompts can call bare ``greatminds X`` without env-setup boilerplate.
- ``--venv /path`` override on ``greatminds launch`` for explicit
  control when auto-detection isn't desired.
- ``greatminds setup`` extends ``coord-init`` with a clearer next-steps
  guide.
- Click-native coloured output: cyan info / green success / red error /
  yellow warning across all subcommands.

### Verified end-to-end

5 env-manager sandboxes (pixi, uv, poetry, conda, plain venv): each
runs ``greatminds setup`` + ``greatminds launch --target tmux``,
attaches a window, executes ``greatminds --version`` from the
activated env — all five pass. Task lifecycle smoke (new → triage →
plan → dev) confirmed against a fresh project: 7+ journal
transitions, schema validation rejects malformed blocks, role
permissions enforced.

## 0.1.1 — 2026-05-20

### Fixed

- `greatminds-coord-launch` and `greatminds-coord-tmux` now find a
  sibling `greatminds-start-agent` in the same venv via
  ``Path(sys.executable).parent`` BEFORE falling back to ``shutil.which``.
  Previously they relied on PATH only, which broke when the user invoked
  the binary by full path (e.g. ``./.venv/bin/greatminds-coord-launch``)
  without sourcing the venv. Also: ``.resolve()`` is intentionally NOT
  called on ``sys.executable`` because uv-managed venvs symlink it to the
  underlying Python install — resolving would skip past the venv's bin
  dir entirely.
- "each agent window has 'bin/start_agent <ROLE> <tool>' pre-typed"
  message updated to print the actual resolved launcher name.

## 0.1.0 — 2026-05-20

First public release.

### Added

- Python package `greatminds` with 19 console entry-points covering the
  full R8 pipeline:
  - **Task management**: `greatminds-task`, `greatminds-inbox`,
    `greatminds-stand`, `greatminds-plan`, `greatminds-migrate-task`.
  - **Pipeline introspection**: `greatminds-gate-check`,
    `greatminds-wake-check`, `greatminds-watchdog`,
    `greatminds-intent-clean`, `greatminds-lint-tokens`.
  - **Hooks (Claude Code)**: `greatminds-stop-decide`,
    `greatminds-notify-journal`.
  - **Project bootstrap**: `greatminds-coord-init`.
  - **Agent launcher / fleet**: `greatminds-start-agent`,
    `greatminds-pty-launch`, `greatminds-render-role`,
    `greatminds-coordd`, `greatminds-coord-tmux`,
    `greatminds-coord-launch` (tmux/vscode/cursor-ide targets).
- Package data shipped under `greatminds.data`: `schema.yaml`,
  `command_START.yaml`, `COORDINATE.md`, `PROJECT_VARIABLES.md`, 13 role
  specs under `roles/`, 8 layered Claude Code plugins under `plugins/`,
  `mcp/canon.json` (context7 + playwright), 3 codex profile-v2 configs
  under `codex/profiles/`, queue templates under `templates/`.
- Path resolution shared across all CLI modules via `greatminds.core`:
  - `find_coord_dir(start=None, *, strict=True)` — walks up from cwd or
    honours `$COORD_PROJECT_DIR`.
  - `find_canon_dir()` — `$COORD_CANON_DIR` override or
    `importlib.resources.files("greatminds.data")`.
  - `caller_role()`, `die`, `now_iso`, `prog_name`.
- GitHub Actions CI building wheel on py 3.11/3.12/3.13, smoke-testing
  every entry-point's `--help`, validating packaged-data resolution, and
  running `greatminds-coord-init` on a fresh tmpdir.

### Notes

- Apache-2.0 licensed.
- `coordd-install` (Bash, systemd unit installer) and the stress-test
  scripts are not packaged in 0.1.0 — they will return as separate
  entry-points once their dependencies on systemd / heavy load
  generation are revisited.
