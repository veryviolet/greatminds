---
name: infra-surface-separation
description: Use when diagnosing an issue to figure out which of the three infrastructure surfaces (stand / fleet / canon) owns the problem and therefore who fixes it. Covers the three-surface model, surface-to-owner mapping, and how to spot a misrouted concern. Trigger on "stand vs fleet", "canon vs fleet", "where does this fix go", "infra triage", "surface ownership".
---

# Infra surface separation

There are three distinct infrastructure surfaces in this system, each
with a clear owner. Misrouting a problem between surfaces is a common
source of confusion and wasted cycles.

## The three surfaces

### 1. Stand
The deployed product instance(s) that EXPLORER/TESTER/READER probe
and SK operates. For lattice: `lattice-a.guardora.ru`,
`lattice-b.guardora.ru` (production-like dev stand, docker compose,
postgres, the FastAPI/grpc backend + the UI build).

- **Owner of operations:** STAND-KEEPER (deploys, restarts, wipes, healthchecks)
- **Owner of product behaviour:** PLANNER (decides what should happen)
+ implementers (DEVELOPER/UI-DEVELOPER) (make it happen)
- **Owner of the underlying hosts/network:** MAINTAINER (escalation
  target if hosts are unreachable, ssh broken, disk full, etc.)

### 2. Fleet
The set of agent processes running locally on the coordination machine:
the tmux session, the per-role claude/codex/cursor instances, the
coordd daemon, the .agent_registry, the inbox/* directories with live
messages.

- **Owner:** MAINTAINER. Exclusively.
- Everything under `~/.claude/`, `~/.codex/`, `coord-tmux`,
  `bin/start_agent`, `bin/coordd`, `.agent_registry/`, `inbox/*/` —
  MAINTAINER.

### 3. Canon
The protocol itself — `/opt/coordination/` contents: schema.yaml,
bin/* scripts, COORDINATE.md, role docs, plugins/, mcp/, command_START.yaml,
templates/, marketplace.json. The single source of truth that gets
synced into every project that uses the coordination system.

- **Owner:** MAINTAINER (mutation rights).
- Changes here are protocol changes — they need cutover discipline
  if they're breaking (see `canon-sync-and-cutover`).

## Routing a reported issue

When something goes wrong, the diagnostic question is "which surface?".

| Symptom | Surface | Action |
|---|---|---|
| API returns 500 | Stand (product behaviour) | Reproduce as bug → PLANNER user_feedback |
| `lattice-a` ssh refuses | Stand (host network) | escalate MAINTAINER → SK rebuild/replace |
| Agent X process is dead | Fleet | MAINTAINER restart |
| Coordd not delivering wake messages | Fleet | MAINTAINER diagnose coordd |
| `bin/task` rejects valid input | Canon (script defect) | MAINTAINER fix bin/* |
| Schema doesn't allow needed transition | Canon (schema gap) | MAINTAINER amend schema with cutover |
| Skill not auto-invoking when relevant | Canon (skill description quality) | MAINTAINER tune SKILL.md description |
| Test failure on stand because of stale code | Stand (deploy issue) | SK rebuild+restart |
| Test failure because backend rebuilt but didn't restart | Stand (bind-mount caveat) | SK applies bind-mount-rebuild-restart pattern |

## Misrouting examples (anti-patterns)

- "Test is flaky" filed to MAINTAINER → actually a product issue,
  goes to PLANNER triage; or a stand-state issue, goes to SK rebuild.
- "Coordd silent" filed to PLANNER → fleet, MAINTAINER.
- "Plan-block was wrong" filed to MAINTAINER → planner-craft issue,
  PLANNER reviews.
- "Skill not firing" filed to PLANNER → canon, MAINTAINER tunes
  description.

## Quick triage command

```bash
# What surface am I looking at right now?
echo "stand: $(curl -fsSL "${STAND_URL_A}/health" 2>&1 | head -1)"
echo "fleet: $(bin/watchdog --project-dir "${PROJECT_ROOT}" 2>&1 | grep -E 'STALE|stale|orphan' | head -3)"
echo "canon: $(git -C /opt/coordination log -1 --oneline)"
```

A green answer on all three: not an infra issue, look at product/plan.

**Tokens used:** STAND_URL_A (PROJECT.env), PROJECT_ROOT (start_agent export).
