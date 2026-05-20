# BOT-DEVELOPER agent — role description

BOT-DEVELOPER fixes target agent/bot behavior issues filed by BOT-USER.

This role originated as a sibling bot-coordination flow and was folded
into the product schema. Divergences (and what was deliberately not
adopted from later product-pipeline refactors) are documented in
`BOT_STREAM_DIVERGENCE.md`.

The bot stream is intentionally NOT touched by the 2026-05 refactor. The
new bin/ scripts (gate_check, wake_check, watchdog) do not act on bot
queues.

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

`<PROJECT_ROOT>/bin/render-role BOT-DEVELOPER`
