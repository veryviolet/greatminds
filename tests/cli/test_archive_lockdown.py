"""Regression tests for task 0100: PLANNER cannot archive in-lifecycle tasks.

Schema removes the five transitions
  feature_dev / feature_ui_dev / feature_docs / feature_test / feature_review → archive
that were previously allowed for ARCHITECT-PLANNER with empty `requires:`.
The canonical withdraw path is now PLANNER → feature_blocked (with a
sentinel `blocked` block) → ARCHITECT-REVIEWER archives from there.

These tests pin: PLANNER's archive shortcut is gone, the legitimate
PLANNER archives (user_feedback, feature_inbox, feature_plan) still work.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _schema_text() -> str:
    from greatminds.core.paths import find_canon_dir
    return (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")


def _schema_transitions() -> list[dict]:
    return (yaml.safe_load(_schema_text()) or {}).get("transitions") or []


def _planner_archive_pairs() -> set[str]:
    return {
        t.get("from")
        for t in _schema_transitions()
        if t.get("to") == "archive" and t.get("by") == "ARCHITECT-PLANNER"
    }


def test_planner_cannot_archive_from_implementer_queues() -> None:
    """The five in-lifecycle implementer queues are NOT in PLANNER's
    archive set. PLANNER trying to mv from them to archive will hit
    'no transition X → archive in schema' in can_role_move()."""
    forbidden = {
        "feature_dev",
        "feature_ui_dev",
        "feature_docs",
        "feature_test",
        "feature_review",
    }
    pairs = _planner_archive_pairs()
    overlap = forbidden & pairs
    assert overlap == set(), (
        f"PLANNER still has schema transitions to archive from "
        f"in-lifecycle queues: {sorted(overlap)} — violates 0100 lockdown. "
        f"Withdrawal must go through PLANNER → feature_blocked → REVIEWER archive."
    )


def test_planner_can_still_archive_from_intake() -> None:
    """Legitimate PLANNER archives (pre-implementation work) remain."""
    pairs = _planner_archive_pairs()
    for src in ("user_feedback", "feature_inbox", "feature_plan"):
        assert src in pairs, (
            f"PLANNER must retain '{src} → archive' transition; "
            f"that's a legitimate pre-impl archive. Set: {sorted(pairs)}"
        )


def test_reviewer_can_still_archive_from_feature_blocked() -> None:
    """REVIEWER's feature_blocked → archive is the terminal step of the
    new withdraw path. Must remain present."""
    transitions = _schema_transitions()
    has = any(
        t.get("from") == "feature_blocked"
        and t.get("to") == "archive"
        and t.get("by") == "ARCHITECT-REVIEWER"
        for t in transitions
    )
    assert has, (
        "REVIEWER's feature_blocked → archive transition is missing — "
        "without it the canonical 0100 withdraw path has no terminal step."
    )


def test_can_role_move_rejects_planner_archive_from_feature_dev() -> None:
    """End-to-end: invoke can_role_move() directly with the forbidden
    combination and assert it returns an error message naming the
    schema gap."""
    from greatminds.cli.task import can_role_move
    err = can_role_move("ARCHITECT-PLANNER", "feature_dev", "archive",
                        task_data={})
    assert err is not None, (
        "can_role_move() must refuse ARCHITECT-PLANNER feature_dev → "
        "archive after 0100 lockdown"
    )
    assert "no transition" in err and "feature_dev" in err and "archive" in err, (
        f"error should explain the missing schema transition; got: {err!r}"
    )


def test_can_role_move_still_allows_reviewer_archive_from_blocked() -> None:
    """The REVIEWER archive path is intact."""
    from greatminds.cli.task import can_role_move
    err = can_role_move("ARCHITECT-REVIEWER", "feature_blocked", "archive",
                        task_data={})
    assert err is None, (
        f"REVIEWER feature_blocked → archive must remain allowed after 0100; "
        f"got error: {err!r}"
    )
