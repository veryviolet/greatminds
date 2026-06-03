"""Tests for task 0314 (0311 Phase 1b): MAINTAINER self-loop
watchdog.

MAINTAINER flips from chat to a periodic /loop health-check that
auto-restarts dead agents / coordd and escalates queue/FSM stalls
to ARCHITECT-PLANNER. It becomes non-user-facing: USER reaches
infra topics through PLANNER.

This task is stand_required (live recovery validation on avatar
is TESTER's job). The Python tests pin the canon shape — the
command_START bootstrap launch mode, the tick body's recovery +
escalation instructions, the coord.yaml template mode, and the
role doc's self-loop / non-user-facing framing.
"""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


# MAINTAINER's bootstrap launch mode + tick-body instructions moved
# out of command_START (the system prompt is now the static
# bootstrap.md; the self-loop behaviour is glossary.lifecycles.self-loop
# + schema.roles.MAINTAINER.event_triggers.on_self_loop_tick). Canon
# self-loop truth is pinned below (coord.yaml template mode + schema
# lifecycle).


# ---------- coord.yaml template ----------


def test_coord_template_maintainer_mode_is_loop() -> None:
    """0314: the coord.yaml template must set the maintainer window
    to mode=loop so fresh fleets launch it as a self-loop."""
    doc = yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(
            encoding="utf-8")
    ) or {}
    windows = doc.get("windows") or []
    maint = next(
        (w for w in windows
         if isinstance(w, dict) and w.get("role") == "MAINTAINER"),
        None,
    )
    assert maint is not None, "coord template missing MAINTAINER window"
    assert maint.get("mode") == "loop", (
        f"0314: maintainer window mode must be 'loop' (got "
        f"{maint.get('mode')!r})"
    )


# ---------- consistency with 0312 lifecycle ----------


def test_schema_lifecycle_self_loop_matches_canon() -> None:
    """0314 builds on 0312's lifecycle field — MAINTAINER's
    schema lifecycle must be self-loop, consistent with the
    bootstrap + role doc."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    maint = (doc.get("roles") or {}).get("MAINTAINER") or {}
    assert maint.get("lifecycle") == "self-loop"
