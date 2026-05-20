---
name: stand-protocol
description: Use whenever the task touches the stand — any plan with stand_required=true, any stand_request to STAND-KEEPER, any verification step that probes a deployed instance, or any rebuild/restart of stand services. Covers gate_check_commit source (from stand_done not impl), role ownership of stand operations (TESTER read-only / SK or EXPLORER mutating), live-mutating verify owner declaration, EXPLORER no-host-probe rule, tooling-blocked smoke escalation, and the bind-mount-rebuild-restart pattern. Trigger on "stand_required", "stand_request", "gate_check", "stand_done", "docker restart", "deploy", "redeploy", "lattice-a", "lattice-b", "live verification".
---

# Stand protocol

Stand work involves several roles with sharply different responsibilities.
Confusing them is a frequent and high-cost mistake (7+ incidents in our
history). This skill codifies the boundaries.

## gate_check_commit source

For any task with `stand_required: true`, the implementer's
`implementation` block has a `gate_check_commit` field. The value
MUST come from the `commit:` field of the matching
`stand_done/<id>.yaml` SK published — NOT from
`implementation.base_commit`.

Why (feedback 0366): the implementer may have written code that
ends up being deployed to a slightly different SHA than they branched
from, especially if SK rebuilt against `origin/main` or if there were
trivial commits in between. The deployed-and-verified SHA is what
matters; SK records it; impl quotes it.

```bash
# In implementer's tick, after SK writes stand_done:
GATE_SHA=$(grep '^commit:' coordination/stand_done/<id>.yaml | awk '{print $2}')
bin/task append-block implementation --id <id> \
  --field gate_check_commit="$GATE_SHA" \
  --field base_commit="$(git rev-parse HEAD)" \
  ...
```

## Role ownership — who does what on the stand

| Operation | Owner |
|---|---|
| `docker compose up/down/build/restart` of stand containers | **STAND-KEEPER** (mutating infra) |
| Volume wipes / fresh-DB / reset state | **STAND-KEEPER** (authorised via stand_request body) |
| GET requests against deployed REST | TESTER, EXPLORER, READER (read-only probes) |
| POST/PUT/PATCH/DELETE against deployed REST during product probes | **STAND-KEEPER** or **EXPLORER** (per the plan's live-mutating verify owner declaration) |
| DB read queries via psql | TESTER, SK, EXPLORER (read-only) |
| DB write queries / migrations | **STAND-KEEPER** only |
| ssh into stand hosts + docker/systemctl | **STAND-KEEPER** only (EXPLORER explicitly forbidden) |
| Smoke healthcheck after rebuild | **STAND-KEEPER** records in stand_done |
| Product P-chain functional verification | **TESTER** (read-only) cites SK's smoke evidence |

## TESTER never mutates the stand

TESTER does **read-only** probes — GET requests, psql SELECT queries,
status checks. For tasks whose verification inherently involves
mutating state (CRUD lifecycle, multi-step workflows that
create/modify/delete entities through the product API), TESTER
DOES NOT execute the mutation chain. Instead the plan declares an
explicit `live-mutating verify owner: STAND-KEEPER | EXPLORER` line in
its body; that named role does the mutating run and writes evidence;
TESTER cites that evidence in the `tests` block.

This is precedent across 7+ incidents (0379, 0381, 0386, 0389, 0396,
0409, 0410). Pretending TESTER can "just curl POST once" silently
diverges from the precedent and burns AR's review budget.

## EXPLORER — no host-probe rule

EXPLORER (scenario B) uses the product as a **user**. Allowed:
- REST API calls (any verb if plan permits) — through the deployed
  endpoint, no shortcuts
- UI clicks through Playwright or browser
- DB read queries through the product's authorised DB access
- Reading deployed docs

Forbidden:
- `ssh` into stand hosts
- `docker` / `docker compose` commands on stand hosts
- `ls` / `cat` of host filesystem
- ANY shell access to the stand infrastructure

If EXPLORER needs infra-readiness changes (rebuild, redeploy,
volume-wipe), file a `stand_request` to SK — do NOT do it directly.

## Tooling-blocked smoke escalation

If a smoke or product probe needs a heavyweight tool that isn't
installed (e.g., a browser harness, a specific protocol client),
do NOT unilaterally provision it on shared stand infrastructure. Do
NOT curl-proxy through to fake the missing capability. Instead:

```bash
bin/inbox send ARCHITECT-PLANNER --kind ask \
  --task <id> --about "tooling-blocked smoke" \
  --body "Need <tool> on <host> for the <chain>; cannot self-provision on shared infra. Please decide: provision via SK, defer the chain, or use alternate verification path."
```

Precedent 0414 affirms: escalate, never workaround.

## bind-mount-rebuild-restart pattern

**Applies when** the project deploys backend services from a
bind-mounted code volume (Docker Compose with `volumes: ["./app:/app"]`
or similar — code lives on the host filesystem, bind-mounted into the
running container).

Pattern: a backend `docker compose build` rebuilds the image but does
**NOT** restart the running process. The bind-mounted code is already
visible inside the live container, but the running process (e.g.,
uvicorn) loaded modules at startup — module-level reads are stale.

So **after a backend rebuild**, SK MUST `docker restart` the backend
service on every host:

```bash
ssh "${STAND_HOST_A}" "cd ${PROJECT_ROOT} && docker compose build && docker restart <backend-service>"
ssh "${STAND_HOST_B}" "cd ${PROJECT_ROOT} && docker compose build && docker restart <backend-service>"
```

UI deploys typically do NOT need this (vite picks up changes via HMR
or a build artifact restart). Only backend bind-mounted services.

Precedent 0380: backend stand_requests that omit the restart leave a
stale uvicorn and tests probe outdated code.

**Tokens used:** STAND_HOST_A, STAND_HOST_B (PROJECT.env), PROJECT_ROOT (exported by start_agent).
