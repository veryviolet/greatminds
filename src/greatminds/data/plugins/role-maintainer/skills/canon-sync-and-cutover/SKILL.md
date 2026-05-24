---
name: canon-sync-and-cutover
description: Use when changes in canon (<GREATMINDS_REPO> — schema, bin/*, role docs, plugins, MCP config) need to propagate to running projects, when introducing a breaking protocol change requires fleet-wide cutover (silence → migrate → restart), or when a cutover failed and needs rollback. Trigger on "canon sync", "cutover", "schema change", "bin/* update", "fleet restart", "rollback", "migration", "coord-init sync".
---

# Canon sync and cutover

Canon (`<GREATMINDS_REPO>`) is the single source of truth for the
protocol. Projects (`<project>/coordination/` and friends) inherit
from it. When canon changes, projects need to be brought in sync — and
sometimes that requires a fleet-wide cutover.

## Three classes of canon change

1. **Plugin / SKILL.md edits.** Skills are loaded on-demand by claude
   when relevant. No fleet restart needed — next agent tick picks
   up new skill content automatically (Claude re-reads the
   `--plugin-dir` contents per session, or at most per-resume).

2. **bin/* script edits.** Project trees usually carry symlinks to
   canon bin/* (via `coord-init`). Running agents already have the
   script open; on next invocation they get the new version. No
   restart needed unless the script's CLI contract changed (in which
   case re-running it after the change may fail until restart).

3. **schema.yaml or hard contract change.** Most disruptive. All
   in-flight tasks were authored against the OLD schema. Restarting
   agents on a NEW schema may reject those tasks. Requires the
   silence-migrate-restart cutover pattern.

## Routine sync (no fleet impact)

For non-breaking canon updates:

```bash
# In canon repo
cd <GREATMINDS_REPO>
git add <changed paths> && git commit -m "<message>" && git push origin main

# Per project: pull canon to project's installed bin/ tree
cd <project>
# bin/ usually symlinks back to canon, so no project-side commit needed.
# Verify:
ls -la greatminds start-agent     # should be symlink → <GREATMINDS_REPO>/greatminds start-agent
# If symlinks are stale, re-run coord-init to refresh:
<GREATMINDS_REPO>/greatminds setup --project-dir "$(pwd)"
```

`coord-init` is idempotent: re-running it copies/links missing pieces
and updates schema.yaml-like canon-derived files (uses `force=True`),
but does NOT overwrite project-owned files (coord.yaml, PROJECT.md).

## Cutover for breaking changes (silence → migrate → restart)

When schema.yaml semantically changes (queue rename, role rename, new
required field, removed transition), an in-flight task in the old
shape will break. Pattern:

1. **Silence.** Tell all roles to stop claiming new work — via
   `greatminds inbox send <each-role> --kind ask` with a "drain in flight,
   do not claim" instruction. Wait for in-flight tasks to reach
   `verified/` or `feature_blocked/` — watch `greatminds watchdog`.

2. **Migrate.** Apply the canon schema change. If existing tasks
   need to be rewritten (e.g., a renamed field), use `greatminds migrate-task`
   or a one-off migration script. Run `greatminds watchdog` to confirm 0
   stale tasks, 0 orphaned intents.

3. **Restart fleet.** Stop all role agents (in tmux, `C-c` each
   window), then re-bootstrap:
   ```bash
   cd <project>
   greatminds launch --target tmux --recreate    # kills old session, rebuilds windows
   tmux a -t agents
   # Press Enter in each window to restart on new schema.
   ```
   coordd is independent (systemd-user) — leave it running; it will
   pick up new sessions automatically.

4. **Verify.** `greatminds watchdog` shows: heartbeats fresh, 0 stale,
   0 orphans. One canary tick per role (any small task) confirms the
   new shape works.

## Rollback if cutover failed

If step 3 lands in a broken state (agents won't start, tasks rejected
en-masse):

```bash
cd <GREATMINDS_REPO>
git log --oneline -5
git revert <cutover-commit>     # creates a clean revert commit
git push origin main

cd <project>
<GREATMINDS_REPO>/greatminds setup --project-dir "$(pwd)" --force  # reapply canon, with --force to clobber
# Restart fleet on rolled-back schema (same C-c + greatminds launch --target tmux --recreate dance)
```

`git revert` (not `git reset`) preserves the history of what we tried
— important for postmortem.

## Symlink discipline

Project's `bin/` directory is expected to be a set of symlinks pointing
back to `<GREATMINDS_REPO>/bin/<name>`. If you replace a script in
canon, projects pick up the change at the next invocation
automatically. If you find a project where `greatminds start-agent` is a
**copy** (not symlink), that's a defect — fix with:

```bash
cd <project>/bin
ln -sf <GREATMINDS_REPO>/greatminds start-agent start_agent
# Confirm: readlink start_agent → <GREATMINDS_REPO>/greatminds start-agent
```

**Tokens used:** none.
