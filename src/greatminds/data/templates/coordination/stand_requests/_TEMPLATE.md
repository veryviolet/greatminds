# stand request template

The `evidence_for` field (new) declares which product task(s) this stand
operation provides evidence for. STAND-KEEPER mirrors it into the
`stand_done/` block on completion so greatminds gate-check can match. Empty list is
fine for infrastructure-only operations.

---
id: <seq>-<short-slug>
stream: stand
reporter: <role>
opened_at: <ISO-8601 UTC>
priority: critical | high | medium | low
request_type: deploy | restart | rebuild | smoke | remote_sync | gpu_check | docs_preview | teardown | vite_up | vite_down | other:<name>
evidence_for:
  - <product task id like 0142-foo>     # one or more; empty list for infra-only ops
target:
  hosts:
    - <local | remote-host-a | remote-host-b | ...>
  profile: <full-deploy | vite-dev | compose profile>
  commit: <sha or current-working-tree>
  related_product_task: <task id or null>
  services:
    - <service>
---

## Goal
What stand operation is needed.

## Context
Why this is needed and which task depends on it.

## Requested Checks
- <health/bootstrap/key exchange/GPU/API/UI/train/inference/docs preview>

## Constraints
- <rsync only, no remote git, preserve volumes, GPU required, etc.>

## Expected Result
What STAND-KEEPER should leave ready and how the requester will use it.
For product-task verification, name the evidence TESTER/ARCHITECT-REVIEWER need:
checked URLs, browser screenshots, logs, commands, host/profile, commit or
working-tree source, and expected pass/fail markers.
