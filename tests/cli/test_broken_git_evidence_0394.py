"""0394: broken git worktree metadata is not commit evidence."""
from __future__ import annotations

import pytest

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError


AT = "2026-06-12T00:00:00Z"
SENTINEL = "unknown-git-worktree"


def _base_block(kind: str) -> dict:
    return {"kind": kind, "by": "ARCHITECT-PLANNER", "at": AT}


def _assert_rejects(block: dict) -> None:
    with pytest.raises(GreatMindsError) as exc:
        task_mod.validate_block("product", block)
    assert SENTINEL in str(exc.value)
    assert "not valid commit evidence" in str(exc.value)


def test_plan_rejects_unknown_git_worktree_base_commit() -> None:
    block = _base_block("plan") | {
        "base_commit": SENTINEL,
        "assignee_role": "DEVELOPER",
        "stand_required": True,
        "stand_reason": "must inspect avatar git state",
        "plan_kind": "bugfix",
        "mode": "A",
        "ready_for_implementation": True,
    }
    _assert_rejects(block)


def test_implementation_rejects_unknown_git_worktree_base_commit() -> None:
    block = {
        "kind": "implementation",
        "by": "DEVELOPER",
        "at": AT,
        "base_commit": SENTINEL,
        "files": ["src/greatminds/cli/task.py"],
        "ready_for_test": True,
    }
    _assert_rejects(block)


def test_tests_reject_unknown_git_worktree_commit_fields() -> None:
    block = {
        "kind": "tests",
        "by": "TESTER",
        "at": AT,
        "base_commit": "abc123",
        "test_files": ["tests/cli/test_x.py"],
        "test_command": "pytest tests/cli/test_x.py -q",
        "test_result": "pass",
        "stand_evidence": {
            "reproduction_steps": "run git rev-parse on stand",
            "observed_without_fix": "stale gitdir pointer",
            "observed_with_fix": ".git absent by design",
            "lease_id": "lease",
            "result": "pass",
            "commit": SENTINEL,
        },
        "gate_check_result": "pass",
        "gate_check_at": AT,
        "gate_check_commit": "abc123",
        "ready_for_review": True,
    }
    _assert_rejects(block)


def test_review_approval_rejects_unknown_git_worktree_commit() -> None:
    block = {
        "kind": "review",
        "by": "ARCHITECT-REVIEWER",
        "at": AT,
        "outcome": "approved",
        "commit": SENTINEL,
    }
    _assert_rejects(block)


def test_explicit_no_git_mode_may_be_recorded_in_prose_not_commit_field() -> None:
    block = _base_block("plan") | {
        "base_commit": "HEAD",
        "assignee_role": "DEVELOPER",
        "stand_required": True,
        "stand_reason": (
            "full-deploy creates an explicit no-git payload; commit evidence "
            "comes from the lease/worktree, not the deployed tree"
        ),
        "plan_kind": "bugfix",
        "mode": "A",
        "ready_for_implementation": True,
    }
    task_mod.validate_block("product", block)

