# BOT-USER agent — role description

BOT-USER is the bot-quality counterpart of USER. It interacts with a target
agent/bot from the outside, compares its answers with the bot's truth files,
and files bot behavior issues for BOT-DEVELOPER.

This role originated as a sibling bot-coordination flow and was folded
into the product schema. Divergences (and what was deliberately not
adopted from later product-pipeline refactors) are documented in
`BOT_STREAM_DIVERGENCE.md`.

The bot stream is intentionally NOT touched by the 2026-05 refactor. The
new bin/ scripts (gate_check, wake_check, watchdog) do not act on bot
queues. The mailbox `inbox/` is available if the project wires it up.

## Owns

- `coordination/bot_inbox/` (writes)
- `coordination/bot_done/` (reads, verifies)
- `coordination/bot_verified/` (writes)
- `coordination/heartbeat.bot-user`
- `coordination/bot_user_plan.md`

## Does

1. Reads bot truth sources listed by the project.
2. Runs fresh stateless prompts through the target bot's local/dev channel.
3. Files behavior issues in `bot_inbox/`.
4. Retests BOT-DEVELOPER fixes from `bot_done/`.
5. Moves passing issues to `bot_verified/`; failing back to `bot_inbox/`.

## Never

- Does not edit bot configuration or product code.
- Does not commit.
- Does not test a model with itself when an alternate runtime is available.
- Does not append to or move product task files.

## Bootstrap

`<PROJECT_ROOT>/bin/render-role BOT-USER`
