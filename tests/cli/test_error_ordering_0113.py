"""Regression tests for task 0113: FSM error ordering — role check
before queue-acceptance check; enriched 'not found' diagnosis.

Pre-0113:
  - append_block raised the queue-acceptance error first; a caller
    using a forbidden role would see 'block kind X not acceptable in
    queue Y' instead of the more direct 'role X may not author kind
    Y' error.
  - _do_move raised plain 'task X not found in any queue' when
    find_task returned None — masking the role-mismatch cause when a
    race or short-id lookup gap was the real culprit (the 2026-05-25
    0097 incident).

0113 fixes both:
  - append_block reorders: role_for_block_kind first, then
    require_block_acceptable_in_queue.
  - _do_move enriches 'not found': if the calling role has no
    authorized transition into to_q from any source queue, that's
    surfaced as the primary error (role-mismatch is the more
    actionable diagnosis).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError


# ---------- append_block: role-first ordering ----------


def _make_dev_task(coord: Path) -> Path:
    a = coord / "feature_dev" / "0001-task-a.yaml"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text(
        "id: 0001-task-a\n"
        "stream: product\n"
        "scope: backend\n"
        "kind: feature\n"
        "reporter: USER\n"
        "opened_at: '2026-05-25T00:00:00Z'\n"
        "priority: normal\n"
        "title: A\n"
        "blocks:\n"
        "- kind: triage\n  by: ARCHITECT-PLANNER\n"
        "  at: '2026-05-25T00:00:00Z'\n  notes: ok\n"
        "- kind: plan\n  by: ARCHITECT-PLANNER\n"
        "  at: '2026-05-25T00:00:00Z'\n"
        "  base_commit: deadbeef\n  assignee_role: DEVELOPER\n"
        "  stand_required: false\n  stand_reason: ''\n"
        "  plan_kind: bugfix\n  mode: A\n"
        "  ready_for_implementation: true\n",
        encoding="utf-8",
    )
    return a


def test_append_block_role_check_fires_before_queue_check(
    tmp_path: Path, monkeypatch,
) -> None:
    """A non-implementation role attempting `implementation` block
    must get the role error first, NOT the queue-acceptance error.
    """
    coord = tmp_path / "coordination"
    coord.mkdir()
    _make_dev_task(coord)
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    # TESTER trying to author an implementation block: role rejection
    # is the canonical error.
    monkeypatch.setattr(task_mod, "caller_role", lambda: "TESTER")
    with pytest.raises(GreatMindsError) as excinfo:
        task_mod.append_block(
            task_id="0001-task-a", kind="implementation",
            fields={"base_commit": "deadbeef",
                    "files": ["src/x.py"],
                    "ready_for_test": True},
            body="x",
        )
    msg = str(excinfo.value)
    # Must be the role error, not the queue-acceptance error.
    assert "role TESTER may not author" in msg or "role TESTER" in msg
    assert "implementation" in msg
    # Should NOT mention queue acceptance first.
    assert "not acceptable in queue" not in msg


# ---------- _do_move: enriched not-found error ----------


def test_do_move_enriches_not_found_when_role_cannot_reach_target(
    tmp_path: Path, monkeypatch,
) -> None:
    """DEVELOPER attempting mv to verified — only ARCHITECT-REVIEWER
    can land there. Even though the task isn't found, the more
    diagnostic error is 'no authorized transition'.
    """
    coord = tmp_path
    found_pairs = task_mod._role_can_reach_target("DEVELOPER", "verified")
    role_ok, permitted = found_pairs
    assert not role_ok, (
        "test premise: DEVELOPER should NOT be able to reach verified; "
        f"permitted={permitted}"
    )
    # Now run _do_move on a non-existent task — should hit the
    # enriched branch.
    with pytest.raises(GreatMindsError) as excinfo:
        task_mod._do_move(
            coord, "DEVELOPER", "9999-nonexistent",
            "verified", "test",
        )
    msg = str(excinfo.value)
    assert "9999-nonexistent" in msg
    assert "not found" in msg
    # Enrichment must name the role + target + at least one allowed role.
    assert "DEVELOPER" in msg
    assert "verified" in msg
    assert "ARCHITECT-REVIEWER" in msg


def test_do_move_plain_not_found_when_role_could_have_reached(
    tmp_path: Path, monkeypatch,
) -> None:
    """DEVELOPER attempting mv to feature_test — DEVELOPER IS allowed
    that path (feature_dev → feature_test). 'Not found' is the right
    error; just add a race-hint instead of the role-mismatch enrichment.
    """
    coord = tmp_path
    role_ok, _ = task_mod._role_can_reach_target("DEVELOPER", "feature_test")
    assert role_ok, "test premise: DEVELOPER may mv to feature_test"
    with pytest.raises(GreatMindsError) as excinfo:
        task_mod._do_move(
            coord, "DEVELOPER", "9999-nonexistent",
            "feature_test", "test",
        )
    msg = str(excinfo.value)
    assert "9999-nonexistent" in msg
    assert "not found" in msg
    # The race-hint variant — should mention re-running task list /
    # wake-check (the incident-style recovery path).
    assert "moved by another agent" in msg or "rerun" in msg
    # Must NOT carry the role-mismatch enrichment.
    assert "no authorized transition" not in msg


def test_do_move_unknown_destination_queue_errors_first(
    tmp_path: Path,
) -> None:
    """Unknown target queue is a CLI/typo error — surface before any
    role / lookup work."""
    with pytest.raises(GreatMindsError) as excinfo:
        task_mod._do_move(
            tmp_path, "DEVELOPER", "0001-anything",
            "definitely-not-a-real-queue", "test",
        )
    assert "unknown destination queue" in str(excinfo.value)


# ---------- _role_can_reach_target helper ----------


def test_role_can_reach_target_includes_current_owner_rows() -> None:
    """The any_active_queue → feature_blocked row uses by:current_owner,
    which means any role that owns a from_q can take that path.
    _role_can_reach_target must surface that — otherwise it'd
    wrongly tell DEVELOPER they can't reach feature_blocked."""
    role_ok, permitted = task_mod._role_can_reach_target(
        "DEVELOPER", "feature_blocked",
    )
    assert role_ok
    assert "current_owner" in permitted


def test_role_can_reach_target_rejects_clearly_disallowed_role() -> None:
    """A role that has no schema row landing in the target → False."""
    role_ok, _ = task_mod._role_can_reach_target("DEVELOPER", "verified")
    assert not role_ok


def test_role_can_reach_target_accepts_concrete_by_role_match() -> None:
    """ARCHITECT-REVIEWER → verified is a direct by:ARCHITECT-REVIEWER
    row in schema."""
    role_ok, permitted = task_mod._role_can_reach_target(
        "ARCHITECT-REVIEWER", "verified",
    )
    assert role_ok
    assert "ARCHITECT-REVIEWER" in permitted
