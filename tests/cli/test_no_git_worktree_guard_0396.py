from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError


def test_required_code_task_cannot_route_to_implementer_queue_without_git(
        tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()

    with pytest.raises(GreatMindsError) as exc:
        task_mod._worktree_hook_pre_move(
            coord,
            "ARCHITECT-PLANNER",
            "0396-no-git-backend",
            {"kind": "bugfix"},
            "feature_plan",
            "feature_dev",
        )

    msg = str(exc.value)
    assert "not a git repository" in msg
    assert "before routing" in msg
    assert "validation-only" in msg


def test_non_worktree_task_kind_still_routes_without_git(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()

    task_mod._worktree_hook_pre_move(
        coord,
        "ARCHITECT-PLANNER",
        "0396-docs",
        {"kind": "docs"},
        "feature_plan",
        "feature_docs",
    )
