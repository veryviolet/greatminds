# stand readiness/result block snippet

Append before moving `stand_wip/X.md -> stand_done/X.md`.
This block records stand operation/readiness only. It is not product acceptance
or regression testing; TESTER performs product checks after readiness exists.

The `evidence_for` field (new) lists the product task ids this stand run
provides evidence for. bin/gate_check matches by this field. For pure
infrastructure operations not tied to a product task, set `evidence_for: []`.

---
stand_result:
  closed_by: stand-keeper-agent
  closed_at: <ISO-8601 UTC>
  result: pass | fail | partial | blocked
  stand_status: READY | DEGRADED | DOWN | BLOCKED
  hosts:
    - <host>
  commit: <sha>
  profile: <full-deploy | vite-dev | other; matches schema.yaml stand_profiles>
  evidence_for:
    - <product task id like 0142-foo>      # one or more; empty list for infra-only ops
  related_product_task: <task id or null>   # legacy single-task field; prefer evidence_for
  commands:
    - <command summary>
  evidence:
    - <health URL/log path/output summary>
  follow_up: null | <user_feedback/task id>
---

## Stand Result
- <summary>

## Evidence
- <readiness output, GPU check, service status, URLs>

## Notes
- <caveats or follow-up needed>
