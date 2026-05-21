---
name: maintainer-vs-planner-routing
description: Use when triaging USER asks or inter-role escalations to decide whether MAINTAINER or ARCHITECT-PLANNER should handle them. Covers the routing table, forwarding mechanics via greatminds inbox, and the "when in doubt" defaults. Trigger on "USER asks", "triage", "route to planner", "route to maintainer", "forward inbox", "infra vs product".
---

# MAINTAINER vs ARCHITECT-PLANNER routing

The two top-level "control" roles split responsibility along a clean
axis: **infrastructure / system** (MAINTAINER) vs **product / planning**
(PLANNER). When USER asks something, or when an agent escalates, the
question is which one of us picks it up.

## Routing table

| Topic | Owner |
|---|---|
| Agent X is dead / stuck / not producing | **MAINTAINER** |
| Tool switch (claude↔codex) for a role | **MAINTAINER** |
| Schema rule needs adding / amending / removing | **MAINTAINER** |
| bin/* script defect or feature request | **MAINTAINER** |
| coordd / cutover / canon sync | **MAINTAINER** |
| `.agent_registry` cleanup / sessionID rotation | **MAINTAINER** |
| Stand infrastructure itself broken (compose, hosts, network) | **MAINTAINER** (and SK for the actual fix) |
| Plugin / SKILL.md / Skill discovery issues | **MAINTAINER** |
| "What features should we build next?" | ARCHITECT-PLANNER |
| Bug discovered while using the product (live behaviour) | ARCHITECT-PLANNER (via user_feedback) |
| Architectural trade-off question | ARCHITECT-PLANNER |
| Plan-block ambiguity in an in-flight task | ARCHITECT-PLANNER |
| READER finds doc gap → spawn writer task | ARCHITECT-PLANNER |
| EXPLORER files bug-as-mini-task | ARCHITECT-PLANNER (triages it) |
| Decision about test strategy / coverage / priority | ARCHITECT-PLANNER |
| Product roadmap, milestones, dependencies between features | ARCHITECT-PLANNER |

## Heuristic when uncertain

Ask: **does the question change the protocol, the agents, or the
machinery they run on?** If yes → MAINTAINER. If no — if it's about
what to BUILD with the protocol → PLANNER.

Example: "the FastAPI route returns 500 when X" — that's a product
bug, goes to PLANNER (via user_feedback). "the agents can't claim
from `feature_dev/` because the queue dir doesn't exist" — that's
infrastructure, goes to MAINTAINER.

## Forwarding

When an inbox ask arrives that's clearly the other one's territory,
forward — don't drop and don't drag PLANNER's queue concerns into
MAINTAINER scope (or vice versa).

```bash
greatminds inbox ack <original-message>      # acknowledge receipt
greatminds inbox send ARCHITECT-PLANNER --kind ask \
  --task <task-if-any> \
  --about "forwarded: <original subject>" \
  --body "Forwarded from MAINTAINER queue — this is product/plan territory.\n\nOriginal body:\n<paste-or-summarise>"
```

When in doubt, double-forwarding (both MAINTAINER and PLANNER receive)
is acceptable; the role that picks it up acks, the other one acks-with-note
"handled by <other-role>".

## What MAINTAINER never does

Recap to avoid scope creep:

- Does NOT triage `user_feedback/` — that's PLANNER's intake.
- Does NOT write plan blocks — PLANNER does that.
- Does NOT claim from product queues (`feature_dev` etc.).
- Does NOT append product-task blocks (plan/impl/tests/review/blocked).
- Does NOT operate the stand directly — STAND-KEEPER does.
- Does NOT modify task files outside of bin/* mutations.

What MAINTAINER does: maintains the **machinery** that lets the
product pipeline run. The product itself is not MAINTAINER's domain.

**Tokens used:** none.
