---
name: stand-bring-up
description: Use when STAND-KEEPER processes a stand_request — full deploy/redeploy/restart sequence, healthcheck smoke, what to record in stand_done evidence. Covers the canonical bring-up sequence, the build vs restart distinction (bind-mount caveat), and the stand_done block writing rules. Trigger on "stand_request", "stand bring-up", "redeploy", "docker compose up", "stand_done evidence", "smoke healthcheck".
---

# Stand bring-up

STAND-KEEPER processes a stand_request by deploying / redeploying /
restarting the stand and recording deterministic evidence in
`stand_done/<id>.yaml`. TESTER (and others) later cite this evidence
in their probes.

## Canonical sequence

1. **Read the stand_request body** — what does the requester actually
   need? Fresh state? Specific commit deployed? A specific service
   restarted? Don't improvise — execute what was asked.

2. **Identify the relevant hosts** — `STAND_HOST_A`, `STAND_HOST_B`
   from PROJECT.env. For peer-pair setups, both must be in sync; for
   single-node, just the one.

3. **Sync code if needed** (commit-based deploys):
   ```bash
   for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
     ssh "${host}" "cd ${PROJECT_ROOT} && git fetch origin && git checkout ${target_commit}"
   done
   ```

4. **Build** (image rebuild):
   ```bash
   for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
     ssh "${host}" "cd ${PROJECT_ROOT} && docker compose build"
   done
   ```

5. **Restart** — CRITICAL for bind-mount setups (see
   `stand-protocol`/bind-mount-rebuild-restart in coordination-protocol):
   ```bash
   for host in "${STAND_HOST_A}" "${STAND_HOST_B}"; do
     ssh "${host}" "cd ${PROJECT_ROOT} && docker compose up -d --wait"
     # For bind-mount backends: rebuilding image alone does NOT restart
     # the running process. After `compose build` you MUST also restart:
     ssh "${host}" "docker restart <backend-service-name>"
   done
   ```
   Skip `docker restart` only if you KNOW the project doesn't bind-mount
   backend code (a pure-image deploy where the new image replaces the
   container).

6. **Smoke healthcheck** — confirm the stand is actually reachable:
   ```bash
   curl -fsSL "${STAND_URL_A}/health" || { echo "FAIL: lattice-a health"; exit 1; }
   curl -fsSL "${STAND_URL_B}/health" || { echo "FAIL: lattice-b health"; exit 1; }
   ```
   If a smoke fails, do NOT write a green stand_done — go to
   `fault-isolation-on-stand`.

7. **Write stand_done** — see "Evidence format" below.

## Evidence format (`stand_done/<id>.yaml` body)

```yaml
- kind: stand_done
  by_role: STAND-KEEPER
  at: <ISO timestamp>
  evidence_for: <requesting-task-id>
  commit: <full 40-char SHA actually deployed; from `git -C ${PROJECT_ROOT} rev-parse HEAD`>
  hosts:
    - "${STAND_HOST_A}"
    - "${STAND_HOST_B}"
  actions:
    - "git checkout <commit> on both hosts"
    - "docker compose build on both hosts"
    - "docker restart <backend-service> on both hosts (bind-mount caveat)"
    - "docker compose up -d --wait on both hosts"
  smoke:
    "${STAND_URL_A}/health": "200 OK"
    "${STAND_URL_B}/health": "200 OK"
  outcome: ready
  body: |
    Stand redeployed at <commit-shortsha> on both hosts. Backend
    restarted to pick up bind-mounted code. Healthcheck green on both
    A and B endpoints.
```

Critical fields:
- **`commit:`** — the EXACT SHA running on the stand right now. NOT
  the requesting task's `base_commit` if SK deployed something
  different. TESTER will use this as `gate_check_commit`.
- **`evidence_for:`** — links to the requesting task (or tasks — can
  be a list if one bring-up satisfies multiple requesters).
- **`outcome:`** — `ready` if smoke is green; `blocked` if you
  couldn't bring it up (see `fault-isolation-on-stand`).

## Live-mutating verification on behalf of TESTER

When a plan declares `live-mutating verify owner: STAND-KEEPER`, SK
must additionally run the mutating product flow as part of bring-up
(or in a separate stand_request specifically for the verification).

Run the actual user-facing CRUD/lifecycle sequence:

```bash
# Example: create→update→delete cycle on the deployed product API
curl -fsS -X POST "${STAND_URL_A}/items" -d '{"name":"smoke-test"}' -H "content-type:application/json" \
  | tee /tmp/created.json
id=$(jq -r '.id' < /tmp/created.json)
curl -fsS -X PATCH "${STAND_URL_A}/items/${id}" -d '{"name":"updated"}' -H "content-type:application/json"
curl -fsS "${STAND_URL_A}/items/${id}" | jq -e '.name == "updated"'
curl -fsS -X DELETE "${STAND_URL_A}/items/${id}"
test "$(curl -fsS -o /dev/null -w '%{http_code}' "${STAND_URL_A}/items/${id}")" = "404"
echo "live-mutating cycle verified end-to-end"
```

Record what you ran in the stand_done body under a `live_mutating:`
section. TESTER cites this evidence in their tests block.

## Idempotent re-run

A `stand_request` may arrive for the same intent twice (retries,
queue weirdness). Bring-up MUST be idempotent — re-running it on an
already-green stand should produce a green stand_done, not duplicate
side-effects. The `docker compose up -d --wait` is naturally idempotent
(it's a no-op if up); just check status before doing destructive ops.

## Don't

- Don't deploy to ONLY one host if both should be in sync. Both or
  none.
- Don't skip the smoke. A green deploy with a failed health is a
  red stand.
- Don't write `outcome: ready` if smoke failed — write `blocked`
  with `body:` describing the failure mode.
- Don't write `commit:` as anything other than the actual deployed
  SHA. If you deployed a different SHA than requested, say so and
  flag in body.

**Tokens used:** STAND_HOST_A, STAND_HOST_B, STAND_URL_A, STAND_URL_B
(PROJECT.env), PROJECT_ROOT (start_agent export).
