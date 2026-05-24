---
name: fresh-db-volume-wipes
description: Use when STAND-KEEPER processes a stand_request that authorises destructive state operations — fresh-DB, drop volumes, wipe persistent state. Covers authorisation chain (don't ask the user again), safety bounds (which paths are sacred), how to record what was wiped. Trigger on "fresh DB", "drop volumes", "wipe state", "destructive", "TRUNCATE", "stand_request body authorises", "volume rm".
---

# Fresh-DB / volume wipes

When a stand_request body explicitly authorises destructive ops (fresh
DB, drop docker volumes, wipe persistent state), SK executes WITHOUT
re-prompting the user. Asking again is wrong (the authorisation is
in the request body + the role's settings.local.json autoMode +
the protocol itself). Just do it, and record what was wiped.

## Authorisation chain

A destructive op is authorised when ALL of:
1. The `stand_request` task body **explicitly** says one of: "fresh
   DB", "drop volumes", "wipe state", "reset stand", or equivalent
   precise language.
2. SK's `<project>/.claude/settings.local.json` has the relevant Bash
   permissions in `permissions.allow` and / or in `autoMode.allow`.
3. `GREATMINDS_ROLE=STAND-KEEPER` is set (you're acting as SK).

If all three are true: **do not** open an AskUserQuestion modal. Execute.
This is per-precedent (the 0185-related feedback): SK halting for
"are you sure?" on already-authorised destructive ops creates
deadlocks where TESTER waits for fresh state that never arrives.

## What's "in bounds" to wipe

Per the project's stand definition (PROJECT.md `<STAND_*>` tokens),
wipeable surfaces typically include:
- The product DB schema's data (TRUNCATE / DROP + re-init from
  migrations).
- Docker named volumes for the stand's data services.
- Object-storage buckets if part of the stand topology.
- Any cache state (redis, in-mem).

**Out of bounds** even if authorised by an emphatic request:
- Anything under the canon tree `src/greatminds/data/` — that's MAINTAINER's surface,
  not stand's.
- Anything in the project's git working tree (`<PROJECT_ROOT>` host
  paths) — destructive state lives in volumes / DBs, not the code
  tree.
- The host machine's root filesystem outside docker volumes — never.
- Other projects' stands. Only this project's.

If a request asks for something out-of-bounds, escalate to MAINTAINER
via `greatminds inbox send MAINTAINER --kind ask` — do NOT execute.

## Standard fresh-DB sequence

```bash
# Stop the stack so connections are gone (we want a clean truncate)
for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
  ssh "${host}" "cd ${PROJECT_ROOT} && docker compose down"
done

# Wipe DB persistent volume on each host
for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
  ssh "${host}" "docker volume rm <project>_postgres_data" || \
    ssh "${host}" "echo 'volume already absent'"
done

# Bring back up — postgres init scripts re-create schema from scratch
for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
  ssh "${host}" "cd ${PROJECT_ROOT} && docker compose up -d --wait postgres"
done

# Re-run migrations (if not part of compose up)
for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
  ssh "${host}" "cd ${PROJECT_ROOT} && docker compose run --rm app alembic upgrade head"
done

# Bring up the rest
for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
  ssh "${host}" "cd ${PROJECT_ROOT} && docker compose up -d --wait"
done

# Smoke
curl -fsSL "${STAND_URL_A}/health"
curl -fsSL "${STAND_URL_B}/health"
```

## TRUNCATE vs DROP/CREATE

If the stand_request just wants "fresh data" but is OK keeping the
schema:

```bash
psql "${GREATMINDS_POSTGRES_DSN}" -c "
  TRUNCATE TABLE items, users, sessions RESTART IDENTITY CASCADE;
"
```

`RESTART IDENTITY` resets serial sequences. `CASCADE` follows FKs so
you don't need to enumerate tables in dependency order.

DROP + CREATE the entire DB is only needed if:
- Schema itself is supposedly corrupt
- A migration that "undo" doesn't undo cleanly added permanent state
- The request explicitly asks "drop the database"

## Recording the wipe in stand_done

```yaml
- kind: stand_done
  by_role: STAND-KEEPER
  at: <ISO>
  evidence_for: <requesting-task-id>
  commit: <SHA running after wipe>
  hosts:
    - "${STAND_HOST_A}"
    - "${STAND_HOST_B}"
  actions:
    - "docker compose down on both hosts"
    - "docker volume rm <project>_postgres_data on both hosts"
    - "docker compose up -d --wait postgres on both hosts"
    - "alembic upgrade head"
    - "docker compose up -d --wait (full stack)"
  wiped:
    - "postgres_data volume (full DB state, schema + data; re-created via migrations)"
  smoke:
    "${STAND_URL_A}/health": "200 OK"
    "${STAND_URL_B}/health": "200 OK"
  outcome: ready
  body: |
    Fresh-DB wipe per request body authorisation. Schema re-created
    from migrations on both hosts; smoke green.
```

The `wiped:` field is OUR addition for destructive bring-ups — makes it
explicit in the audit trail what was destroyed.

## Partial-wipe variants

Sometimes the request wants narrower scope: "wipe items table only,
keep users". Then TRUNCATE the specific tables:

```bash
psql "${GREATMINDS_POSTGRES_DSN}" -c "TRUNCATE TABLE items RESTART IDENTITY CASCADE;"
```

Record narrowly in `wiped:`: "items table (rows + sequences); users
and sessions intact".

## When NOT to wipe

If the stand_request body does NOT explicitly authorise destructive
ops, and you discover wiping would resolve the issue: file an
`inbox send ARCHITECT-PLANNER --kind ask` describing the discovered
need and asking whether to add it to the request scope. Do NOT
freelance.

**Tokens used:** STAND_HOST_A, STAND_HOST_B, STAND_URL_A, STAND_URL_B,
GREATMINDS_POSTGRES_DSN (PROJECT.env), PROJECT_ROOT (start_agent export).
