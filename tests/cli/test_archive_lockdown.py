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


def _write_feature_dev_task(coord: Path, task_id: str = "0001-cancel-me") -> None:
    p = coord / "feature_dev" / f"{task_id}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({
        "id": task_id,
        "stream": "product",
        "scope": "backend",
        "kind": "feature",
        "reporter": "USER",
        "opened_at": "2026-06-13T00:00:00Z",
        "priority": "normal",
        "title": "Cancel me",
        "blocks": [
            {"kind": "triage", "by": "ARCHITECT-PLANNER",
             "at": "2026-06-13T00:00:00Z", "notes": "ok"},
            {"kind": "plan", "by": "ARCHITECT-PLANNER",
             "at": "2026-06-13T00:00:00Z", "base_commit": "deadbeef",
             "assignee_role": "DEVELOPER", "stand_required": False,
             "stand_reason": "", "plan_kind": "bugfix", "mode": "A",
             "ready_for_implementation": True},
        ],
    }), encoding="utf-8")


def test_planner_can_withdraw_in_lifecycle_task_via_feature_blocked(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression for the owner-deadlock in the documented withdrawal path:
    PLANNER must be able to park in-lifecycle work when USER cancels it, while
    REVIEWER remains the terminal archive role."""
    from greatminds.cli import task as task_mod

    coord = tmp_path / "coordination"
    (coord / "feature_blocked").mkdir(parents=True)
    _write_feature_dev_task(coord)
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    monkeypatch.setattr(task_mod, "caller_role", lambda: "ARCHITECT-PLANNER")

    task_mod.append_block(
        task_id="0001-cancel-me",
        kind="blocked",
        fields={
            "blocked_by": "ARCHITECT-PLANNER",
            "blocked_at": "2026-06-13T00:00:01Z",
            "reason": "withdrawn by USER; cancelled before implementation",
            "dependencies": ["archive/9999-never-created.yaml"],
            "resume_to": "feature_dev",
        },
    )
    from_q = task_mod.move_task(
        task_id="0001-cancel-me",
        to_queue="feature_blocked",
        reason="USER withdrew task",
    )

    assert from_q == "feature_dev"
    assert (coord / "feature_blocked" / "0001-cancel-me.yaml").is_file()
    assert not (coord / "feature_dev" / "0001-cancel-me.yaml").exists()


def test_planner_cannot_file_normal_dependency_blocked_block(
    tmp_path: Path, monkeypatch,
) -> None:
    """The PLANNER exception is cancellation-only, not a second owner path for
    ordinary dependency parking."""
    from greatminds.cli import task as task_mod
    from greatminds.core.errors import GreatMindsError

    coord = tmp_path / "coordination"
    _write_feature_dev_task(coord)
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    monkeypatch.setattr(task_mod, "caller_role", lambda: "ARCHITECT-PLANNER")

    with pytest.raises(GreatMindsError) as excinfo:
        task_mod.append_block(
            task_id="0001-cancel-me",
            kind="blocked",
            fields={
                "blocked_by": "ARCHITECT-PLANNER",
                "blocked_at": "2026-06-13T00:00:01Z",
                "reason": "waiting on upstream dependency",
                "dependencies": ["feature_dev/0002-dep.yaml"],
                "resume_to": "feature_dev",
            },
        )

    assert "withdrawn-class" in str(excinfo.value)


def test_task_withdraw_helper_builds_canonical_block(
    tmp_path: Path, monkeypatch,
) -> None:
    from greatminds.cli import task as task_mod

    coord = tmp_path / "coordination"
    (coord / "feature_blocked").mkdir(parents=True)
    _write_feature_dev_task(coord)
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    monkeypatch.setattr(task_mod, "caller_role", lambda: "ARCHITECT-PLANNER")

    from_q = task_mod.withdraw_task(
        task_id="0001-cancel-me",
        reason="USER cancelled this duplicate",
    )

    assert from_q == "feature_dev"
    data = task_mod.load_task(
        coord / "feature_blocked" / "0001-cancel-me.yaml")
    blocked = [b for b in data["blocks"] if b["kind"] == "blocked"][-1]
    assert blocked["reason"].startswith("withdrawn:")
    assert blocked["dependencies"] == ["archive/0000-withdrawn-sentinel.yaml"]
    assert blocked["resume_to"] == "feature_dev"
