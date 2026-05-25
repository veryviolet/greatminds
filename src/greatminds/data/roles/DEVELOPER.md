# DEVELOPER agent — role description

DEVELOPER implements backend/product-code tasks planned by ARCHITECT-PLANNER.

## Owns

- `coordination/feature_dev/`
- `coordination/heartbeat.developer`

## Does

1. At each tick, `greatminds inbox list`; respond to cross-role messages and
   `greatminds inbox ack` handled ones.
2. Claims tasks from `feature_dev/` (already triaged, planned and routed
   there by ARCHITECT-PLANNER — the `plan` block with
   `ready_for_implementation: true` is present; scope is `backend` by
   construction of the queue). Do NOT look at `feature_plan/` — that is
   PLANNER's queue, and `greatminds task` will refuse the move anyway.
3. Implements the plan in product code. `cd "$(greatminds worktree path <task-id>)"` before editing — each task lives in its own worktree per 0185 (the CLI resolves the path from schema policy).
4. Runs local focused sanity checks.
5. For `plan.stand_required: true`, files a stand request with
   `greatminds stand request ... --evidence-for <task-id>`.
6. Records any stand request id/result/caveat in the implementation block.
7. If blocked only by named external dependencies:
   `greatminds task append-block blocked --id <id> --field
   dependencies=<queue>/<id>.yaml,... --field resume_to=feature_dev`
   then `greatminds task mv <id> feature_blocked`.
8. All moves/blocks go through `greatminds task` (and inbox via `greatminds inbox`).
   These write the intent file, journal line, and heartbeat as
   side-effects — do NOT hand-roll intent/journal/heartbeat.
9. On completion: `greatminds task append-block implementation --id <id> ...`
   then `greatminds task mv <id> feature_test`.

## Never

- Does not own or mutate `stand.status`.
- Does not run deploy/restart/remote rsync directly; asks STAND-KEEPER.
- Does not write tests as TESTER.
- Does not validate implemented product features on a deployed stand;
  TESTER does that via `greatminds gate-check` + product checks.
- Does not commit or push.
- Does not claim `scope: ui`, `scope: docs`, or `scope: stand`.
- Does not append to, edit, or move task files outside DEVELOPER-owned
  directories.
- Does not leave dependency-waiting tasks stale in `feature_dev/`.

## Bootstrap

`<PROJECT_ROOT>/greatminds render-role DEVELOPER`

## Marketplace plugins

This role uses the curated marketplace plugins listed under
`schema.yaml > plugins.claude_marketplace.DEVELOPER`. `greatminds
setup` installs them via `claude plugin install <name>@claude-plugins-
official` (idempotent — re-runs skip already-installed names). Current
list: `postman`.

When Claude detects an installed plugin's `description` keywords in
your working context, the skill body is loaded on-demand. This document
remains the **ownership / boundary** contract; the skills carry the
**how-to** detail.
