"""Tests for task 0303 (upstream issue #3): ``task append-block``
refuses implementation/tests blocks when cwd isn't the per-task
worktree.

Pre-0303 the schema's ``worktrees.required_for_task_kinds`` was
declarative only — implementers could silently edit main while
filing the block. TESTER then rsync'd from ``.worktrees/<id>/``
which lacked the fix → silent contract break. 0303 adds a CLI
gate at append time.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError


def _enforce(kind: str, data: dict, coord: Path, task_id: str) -> None:
    """Direct invocation of the gate helper for unit tests."""
    return task_mod._enforce_worktree_isolation_for_block(
        kind, data, coord, task_id,
    )


def _project(tmp_path: Path) -> Path:
    """Build coord + worktrees layout."""
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)
    return project


# ---------- non-applicable cases (gate skips) ----------


def test_non_code_block_skips_gate(tmp_path: Path, monkeypatch) -> None:
    """Plan/triage/review/etc. blocks are not code-mutating →
    gate must skip regardless of cwd. Otherwise PLANNER's normal
    flow would break (PLANNER edits the task file from main)."""
    project = _project(tmp_path)
    monkeypatch.chdir(project)  # main tree (not .worktrees)
    for kind in ("plan", "triage", "review", "blocked",
                  "session_iteration", "reader_review"):
        _enforce(kind, {"kind": "feature"},
                 project / "coordination", "0303-x")  # no raise


def test_docs_kind_skips_gate(tmp_path: Path, monkeypatch) -> None:
    """task.kind=docs/research isn't in required_for_task_kinds →
    implementation/tests blocks land regardless of cwd."""
    project = _project(tmp_path)
    monkeypatch.chdir(project)  # main tree
    _enforce("implementation", {"kind": "docs"},
             project / "coordination", "0303-x")  # no raise


def test_env_var_overrides_check(tmp_path: Path, monkeypatch) -> None:
    """GREATMINDS_SKIP_WORKTREE_CHECK=1 bypasses the gate so CI
    containers / power users aren't blocked."""
    project = _project(tmp_path)
    monkeypatch.chdir(project)  # main tree
    monkeypatch.setenv("GREATMINDS_SKIP_WORKTREE_CHECK", "1")
    _enforce("implementation", {"kind": "feature"},
             project / "coordination", "0303-x")  # no raise


# ---------- applicable cases ----------


def test_main_tree_implementation_refused(
    tmp_path: Path, monkeypatch,
) -> None:
    """The motivating case: implementer files an impl block while
    cwd is the main tree, not .worktrees/<id>. Refused with the
    correct path hint."""
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    with pytest.raises(GreatMindsError) as exc:
        _enforce("implementation", {"kind": "feature"},
                 project / "coordination", "0303-test-task")
    msg = str(exc.value)
    assert "0303-test-task" in msg or "0303" in msg
    assert ".worktrees" in msg
    assert "GREATMINDS_SKIP_WORKTREE_CHECK" in msg


def test_main_tree_tests_block_refused(
    tmp_path: Path, monkeypatch,
) -> None:
    """tests blocks also gated — TESTER edits worktree code per
    the canon flow."""
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    with pytest.raises(GreatMindsError):
        _enforce("tests", {"kind": "bugfix"},
                 project / "coordination", "0303-x")


def test_worktree_seq_only_accepted(tmp_path: Path, monkeypatch) -> None:
    """cwd = project/.worktrees/0303 → accepted (seq-only form)."""
    project = _project(tmp_path)
    wt = project / ".worktrees" / "0303"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    _enforce("implementation", {"kind": "feature"},
             project / "coordination", "0303-x")


def test_worktree_full_slug_accepted(
    tmp_path: Path, monkeypatch,
) -> None:
    """cwd = project/.worktrees/0303-fix-X → accepted (slug form
    matches the seq prefix)."""
    project = _project(tmp_path)
    wt = project / ".worktrees" / "0303-fix-something"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    _enforce("implementation", {"kind": "feature"},
             project / "coordination", "0303-fix-something")


def test_worktree_wrong_seq_refused(
    tmp_path: Path, monkeypatch,
) -> None:
    """cwd = project/.worktrees/9999 but task is 0303 → refused
    (mirror of the 0271 stand-lease validator's seq check)."""
    project = _project(tmp_path)
    wt = project / ".worktrees" / "9999"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    with pytest.raises(GreatMindsError):
        _enforce("implementation", {"kind": "feature"},
                 project / "coordination", "0303-x")


def test_random_path_refused(tmp_path: Path, monkeypatch) -> None:
    """cwd in some unrelated dir (not project/.worktrees/<seq>) →
    refused."""
    project = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(GreatMindsError):
        _enforce("implementation", {"kind": "feature"},
                 project / "coordination", "0303-x")


def test_nested_subdir_inside_worktree_still_accepted(
    tmp_path: Path, monkeypatch,
) -> None:
    """cwd = project/.worktrees/0303/src/x → accepted (the gate
    walks up the worktrees-root prefix). Editors / scripts often
    cd into subdirs."""
    project = _project(tmp_path)
    wt = project / ".worktrees" / "0303" / "src" / "x"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    _enforce("implementation", {"kind": "feature"},
             project / "coordination", "0303-x")
