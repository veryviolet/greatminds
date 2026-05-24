"""Regression test for task 0104: feature_blocked.writers consistency.

PLANNER can mv tasks INTO feature_blocked (via the
``any_active_queue → feature_blocked by current_owner`` transition,
since PLANNER owns feature_inbox / feature_plan / review_sessions /
stand_requests / archive / user_feedback). So PLANNER must also be
listed in feature_blocked.writers — otherwise PLANNER can park a task
but cannot append blocks to it while parked, which breaks any flow
that needs to update a parked PLANNER task in place.

This test pins option (a) from the task body: add ARCHITECT-PLANNER
to feature_blocked.writers.
"""
from __future__ import annotations

import yaml


def _schema() -> dict:
    from greatminds.core.paths import find_canon_dir
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def test_planner_in_feature_blocked_writers() -> None:
    fb = (_schema().get("queues") or {}).get("feature_blocked") or {}
    writers = fb.get("writers") or []
    assert "ARCHITECT-PLANNER" in writers, (
        "feature_blocked.writers must include ARCHITECT-PLANNER (0104 "
        "consistency fix) — PLANNER can mv into feature_blocked via the "
        f"current_owner transition. Current writers: {writers}"
    )


def test_feature_blocked_writers_still_includes_implementers() -> None:
    """Adding PLANNER must NOT remove anyone else."""
    fb = (_schema().get("queues") or {}).get("feature_blocked") or {}
    writers = set(fb.get("writers") or [])
    for r in ("DEVELOPER", "UI-DEVELOPER", "TECHNICAL-WRITER",
              "TESTER", "READER", "ARCHITECT-REVIEWER"):
        assert r in writers, (
            f"{r} dropped from feature_blocked.writers — broke the parking "
            f"queue for that role. Current writers: {sorted(writers)}"
        )
