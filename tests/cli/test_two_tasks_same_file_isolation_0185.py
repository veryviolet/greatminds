"""0185 contract proof: two tasks editing the same file in separate
worktrees don't contaminate each other.

Pre-0185 the 0115/0166 file-lock system serialized this — task B
blocked on task A's lock until A's verified-and-released. The locks
were a band-aid; with per-task worktrees task B simply edits in its
own checkout. Both reach feature_review independently; REVIEWER then
merges them sequentially into main (one may conflict — handback per
schema.yaml > worktrees.conflict_handback_to).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from greatminds.cli import worktree as wt_mod


def _git(args: list[str], cwd: Path,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(["init", "-b", "main"], project)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "user.name", "Test"], project)
    (project / "foo.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    _git(["add", "foo.py"], project)
    _git(["commit", "-m", "initial foo"], project)
    return project


def test_two_tasks_edit_same_file_in_parallel(repo: Path) -> None:
    """0185 contract: tasks A and B both declare foo.py. Pre-0185
    they would have serialized via the 0166 lock. Post-0185 they
    edit independently in their own worktrees. No contamination."""
    wt_a = wt_mod.worktree_create(repo, "0185-task-a", base="main")
    wt_b = wt_mod.worktree_create(repo, "0185-task-b", base="main")
    # Both worktrees see the same initial foo.py — they were branched
    # from main.
    assert (wt_a / "foo.py").read_text(encoding="utf-8") == \
           (wt_b / "foo.py").read_text(encoding="utf-8")

    # A edits foo.py one way.
    (wt_a / "foo.py").write_text(
        "def foo():\n    return 2  # task A change\n", encoding="utf-8")
    _git(["add", "foo.py"], wt_a)
    _git(["commit", "-m", "a edits"], wt_a)

    # B edits foo.py a different way — concurrently, in its own
    # worktree. Pre-0185 this would have been blocked by the lock.
    (wt_b / "foo.py").write_text(
        "def foo():\n    return 3  # task B change\n", encoding="utf-8")
    _git(["add", "foo.py"], wt_b)
    _git(["commit", "-m", "b edits"], wt_b)

    # Both worktrees still see their OWN edits.
    assert "task A" in (wt_a / "foo.py").read_text(encoding="utf-8")
    assert "task B" in (wt_b / "foo.py").read_text(encoding="utf-8")
    # Main is UNCHANGED — neither edit has merged yet.
    assert (repo / "foo.py").read_text(encoding="utf-8") == \
           "def foo():\n    return 1\n"


def test_reviewer_merges_first_task_cleanly_second_conflicts(repo: Path) -> None:
    """0185 contract: REVIEWER merges task A first (clean), then task
    B (conflicts because both touched the same line of foo.py). The
    second merge surfaces conflict markers so REVIEWER can hand back
    via schema's conflict_handback_to.
    """
    wt_a = wt_mod.worktree_create(repo, "0185-task-a", base="main")
    wt_b = wt_mod.worktree_create(repo, "0185-task-b", base="main")
    (wt_a / "foo.py").write_text(
        "def foo():\n    return 2  # task A change\n", encoding="utf-8")
    _git(["add", "foo.py"], wt_a)
    _git(["commit", "-m", "a edits"], wt_a)
    (wt_b / "foo.py").write_text(
        "def foo():\n    return 3  # task B change\n", encoding="utf-8")
    _git(["add", "foo.py"], wt_b)
    _git(["commit", "-m", "b edits"], wt_b)

    a_result = wt_mod.worktree_merge(repo, "0185-task-a")
    assert a_result.ok is True

    # Now B conflicts on the same line.
    b_result = wt_mod.worktree_merge(repo, "0185-task-b")
    assert b_result.ok is False
    assert "foo.py" in b_result.conflicts
    # And main has been restored to A's merged state (no half-merged
    # MERGE_HEAD leftover).
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert "task A" in (repo / "foo.py").read_text(encoding="utf-8")
