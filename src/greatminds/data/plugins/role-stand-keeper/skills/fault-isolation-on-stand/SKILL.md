---
name: fault-isolation-on-stand
description: Use when a stand bring-up smoke fails or a deployed service is misbehaving — narrowing down which layer (container / network / DB / app code / data) owns the fault, and deciding whether to fix in SK scope or escalate to MAINTAINER (infra) or PLANNER (product bug). Trigger on "smoke failed", "stand broken", "container won't start", "health check failing", "503", "connection refused", "fault isolation", "escalate stand failure".
---

# Fault isolation on stand

When a smoke fails or a stand service is misbehaving, SK has to figure
out **which layer** owns the fault before fixing it. Wrong layer = wasted
cycles. Right layer = either fix-and-rebuild (still SK) or escalate
with a useful summary (MAINTAINER for infra, PLANNER for product bug).

## Diagnostic ladder (top-down)

Walk from outermost (the smoke request) inward until you find the
broken layer.

### Layer 1 — DNS / network reachability

```bash
# Can we reach the host at all?
ssh -o ConnectTimeout=5 "${STAND_HOST_A}" "echo ok"
# Output empty / timed out → host is down or unreachable. Escalate
# MAINTAINER (infra), not SK's scope.
```

If unreachable: file `greatminds inbox send MAINTAINER --kind ask` describing
which host, when last reachable, what attempts you made.

### Layer 2 — Docker daemon health

```bash
ssh "${STAND_HOST_A}" "docker info" | head -20
# Looking for: "Server: " section with running info; no errors
```

If docker daemon is unhappy: MAINTAINER again (infra surface). SK
doesn't restart dockerd.

### Layer 3 — Container state

```bash
ssh "${STAND_HOST_A}" "cd ${PROJECT_ROOT} && docker compose ps"
# Look for: services with Status "Up", "running"
# Bad signs: "Restarting", "Exited", "Created" (never started)
```

For each non-running service:

```bash
ssh "${STAND_HOST_A}" "cd ${PROJECT_ROOT} && docker compose logs --tail=100 <service-name>"
```

Common patterns in the logs:
- "ImportError" / "ModuleNotFoundError" → image / code mismatch.
  Likely fix: rebuild + restart. SK scope.
- "could not connect to server: connection refused" (DB) → ordering
  issue or DB itself down. Check postgres separately.
- "Address already in use" → another process bound the port. Either
  stale container, or something else on the host. SK can handle if
  stale container; if external process — MAINTAINER.
- "permission denied" on a bind-mounted path → host permissions issue.
  MAINTAINER for proper host setup.
- Application-level errors (validation failures, business logic
  errors, traceback in our code) → PRODUCT bug. Capture and escalate
  PLANNER (file `greatminds inbox send ARCHITECT-PLANNER --kind ask` with
  context). SK doesn't fix product bugs.

### Layer 4 — DB connectivity

```bash
ssh "${STAND_HOST_A}" "docker compose exec postgres pg_isready -U postgres"
# Output: "<host>:5432 - accepting connections" → DB up.
# Otherwise: DB itself unhealthy.

# From the app's perspective:
ssh "${STAND_HOST_A}" "docker compose exec app psql ${COORD_POSTGRES_DSN} -c 'SELECT 1'"
```

If DB up but app can't connect: usually wrong DSN (env var mismatch),
network between containers, or auth issue. Check
`docker compose config` to see what env the app actually got.

### Layer 5 — Migration / schema state

```bash
ssh "${STAND_HOST_A}" "docker compose exec app alembic current"
ssh "${STAND_HOST_A}" "docker compose exec app alembic heads"
# 'current' should match 'heads'. If not — pending migration.
ssh "${STAND_HOST_A}" "docker compose exec app alembic upgrade head"
```

A common "smoke fails after deploy" cause is a new migration didn't
run (CI didn't run it, or compose service order missed it).

### Layer 6 — Bind-mount stale-process

Per the bind-mount-rebuild-restart pattern: if you did `docker compose
build` and forgot the `docker restart`, the running process is on
OLD code reading NEW config / migrations. Symptoms:
- Health endpoint passes but specific feature endpoints 500
- Logs show no startup banner from the recent rebuild

Fix: `docker restart <backend-service>` on every host.

## Escalation triage

After isolating the layer:

| Layer | Owner | Action |
|---|---|---|
| 1 (DNS/host reachability) | MAINTAINER | inbox ask, include `ping` / `ssh -v` evidence |
| 2 (docker daemon) | MAINTAINER | inbox ask |
| 3 (container fail, image / code mismatch) | SK | rebuild + restart + re-smoke |
| 3 (container fail, application traceback) | PLANNER | inbox ask, attach traceback, link to suspect task |
| 4 (DB up, app can't connect) | usually SK (env / restart fixes); escalate MAINTAINER if config drift | depends |
| 5 (migration) | SK | run upgrade, smoke |
| 6 (bind-mount stale) | SK | docker restart, smoke |
| psql-level DB corruption | MAINTAINER | inbox ask + consider fresh-DB if authorised separately |

## Capturing evidence for an escalation

```bash
# Bundle the relevant logs into a tmpfile for the escalation body
{
  echo "=== compose ps on lattice-a ==="
  ssh "${STAND_HOST_A}" "cd ${PROJECT_ROOT} && docker compose ps"
  echo
  echo "=== <service> logs (last 100) ==="
  ssh "${STAND_HOST_A}" "cd ${PROJECT_ROOT} && docker compose logs --tail=100 <service>"
  echo
  echo "=== smoke ==="
  curl -fsS -o /dev/null -w "%{http_code}\n" "${STAND_URL_A}/health" 2>&1 || echo "health: down"
} > /tmp/fault-isolation-${TASK_ID}.log

# Reference it in the inbox ask
greatminds inbox send MAINTAINER --kind ask \
  --task "${TASK_ID}" \
  --about "stand smoke failed: <one-line summary>" \
  --body "$(cat /tmp/fault-isolation-${TASK_ID}.log)"
```

## Write stand_done with `outcome: blocked`

When you can't bring it up and have escalated, still write a
stand_done — with `outcome: blocked` and the escalation reference:

```yaml
- kind: stand_done
  by_role: STAND-KEEPER
  at: <ISO>
  evidence_for: <requesting-task-id>
  commit: <SHA attempted>
  hosts: ["${STAND_HOST_A}", "${STAND_HOST_B}"]
  outcome: blocked
  body: |
    Bring-up failed at layer 3 (container <service> in restart-loop on
    both hosts). Isolated cause to ImportError in app/foo.py after
    upgrade to dependency X v2.3.

    Escalated to ARCHITECT-PLANNER via inbox/architect-planner/
    ask-<ts>-...md — product bug, not infra. Will re-run bring-up
    after fix verified by REVIEWER and a new stand_request lands.
  blocked_on:
    role: ARCHITECT-PLANNER
    inbox_ref: inbox/architect-planner/ask-<ts>-stand-blocked.md
```

This keeps the chain auditable — the requesting task sees that SK
tried, where it failed, and what's blocking unblock.

## Don't

- Don't restart-loop a broken container hoping it'll stabilise. Read
  the logs, isolate the layer, fix or escalate.
- Don't escalate without evidence. "It doesn't work" with no logs
  burns MAINTAINER/PLANNER's time.
- Don't claim a fresh-DB wipe will fix things if you haven't isolated
  to a data-corruption cause. Wipes are destructive; do them when
  authorised and when they actually address the diagnosed cause.

**Tokens used:** STAND_HOST_A, STAND_HOST_B, STAND_URL_A, COORD_POSTGRES_DSN
(PROJECT.env), PROJECT_ROOT (start_agent export).
