# Changelog

All notable changes to **greatminds** are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; versions
follow [SemVer](https://semver.org/) once 1.0.0 ships.

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
