# bot developer block snippet

Append before moving `bot_wip/X.md -> bot_done/X.md`.

---
developer:
  closed_by: bot-developer-agent
  closed_at: <ISO>
  base_commit: <git short sha before edits>
  files_changed:
    - <path>
  deploy_state: local_only | local_only_bridge | restart_needed
  commit_sha: <sha or null if project policy forbids commit>
  pushed: false
  deployed_at: null
  ready_for_retest: true | local_only_unverifiable
---

## What was changed
- <summary>

## Why
- <how this addresses the issue>

## How BOT-USER should re-test
- <additional retest notes>
