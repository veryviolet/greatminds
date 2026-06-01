# BOT-DEVELOPER agent — role description

BOT-DEVELOPER fixes target agent/bot behavior issues filed by BOT-USER.

This role originated as a sibling bot-coordination flow and was folded
into the product schema. Divergences (and what was deliberately not
adopted from later product-pipeline refactors) are documented in
`BOT_STREAM_DIVERGENCE.md`.

The bot stream is intentionally NOT touched by the 2026-05 refactor. The
new `greatminds` CLI subcommands (`greatminds gate-check`,
`greatminds wake-check`, `greatminds watchdog`) do not act on bot
queues.

## Session start (0304)

At the FIRST tick after `start-agent`, before any queue work, run
these steps in order. They are not optional — silent drift on any
of them is a contract violation.

1. Read `coordination/COORDINATE.md` (FSM, ownership, mutation
   rules — bot-stream divergences noted in
   `BOT_STREAM_DIVERGENCE.md`).
2. Read `schema.yaml > roles.BOT-DEVELOPER` contract — your
   `responsibilities`, `forbidden_actions`, and `event_triggers`.
   Render via `greatminds role contract BOT-DEVELOPER`.
3. Read `coordination/PROJECT.md`.
4. Drain `coordination/inbox/bot-developer/` — ack every pending
   message via `greatminds inbox ack <path>`.
5. Continue normal tick per the role-specific contract below.

**Inline invariants:**

- ALL mutations under `coordination/` go through the `greatminds`
  CLI.
- BOT-DEVELOPER does NOT claim from product queues; bot stream is
  isolated.

## Owns

- `coordination/bot_wip/`
- `coordination/bot_done/` (writes fixed issues)
- `coordination/heartbeat.bot-developer`
- Bot configuration files explicitly listed by the project.

## Does

1. Claims the oldest issue from `bot_inbox/`.
2. Reads truth files and the report.
3. Applies the smallest configuration change that fixes the behavior.
4. Runs a local sanity prompt when possible.
5. Records changed paths and verification notes.
6. `mv bot_wip/X bot_done/X`.

## Git and deploy

Per `<BOT_COMMIT_POLICY>`. Default in a product repo: no commit, no deploy.

## Never

- Does not alter unrelated product code.
- Does not merge multiple unrelated bot issues into one fix.
- Does not force-push, hard-reset, rebase, or touch secrets.
- Does not append to or move product task files.

## Bootstrap

`<PROJECT_ROOT>/greatminds render-role BOT-DEVELOPER`
