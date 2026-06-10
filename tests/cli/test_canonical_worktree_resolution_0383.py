"""Task 0383: canonical worktree resolution + per-task fingerprints.

Three durable fixes behind the recurring 0361/0365/0380 phantom / empty
merges:

1. ``canonical_task_id`` maps any id form (short seq ``0382`` or full slug
   ``0382-full-deploy-...``) to the FULL task id, so worktree PATH /
   BRANCH / CREATE / REMOVE / MERGE never split between ``.worktrees/0382``
   / ``task/0382`` and ``.worktrees/0382-<slug>`` / ``task/0382-<slug>``.

2. ``compute_worktree_fingerprint`` is computed over the PER-TASK worktree
   (tracked diff + untracked new files), so distinct task diffs get
   distinct hashes instead of all collapsing to one value (the c474b1e3
   collision caused by diffing the overlay-free main tree).

3. ``worktree_merge`` captures the worktree's UNCOMMITTED overlay as a
   real commit on the task branch before merging — DEVELOPER works
   uncommitted by design, so otherwise ``git merge task/<id>`` brings in
   nothing and the implementation is silently dropped.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from greatminds.cli import worktree as wt_mod
from greatminds.cli.gate_check import compute_worktree_fingerprint


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(["init", "-b", "main"], project)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "user.name", "Test"], project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "README.md"], project)
    _git(["commit", "-m", "initial"], project)
    return project


def _seed_task(project: Path, full_id: str, queue: str = "feature_dev") -> None:
    """Drop a minimal task file so find_task can resolve short→full."""
    qdir = project / "coordination" / queue
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / f"{full_id}.yaml").write_text(
        f"id: {full_id}\nstream: product\nkind: bugfix\n", encoding="utf-8")


# ---------- 1. canonical resolution ----------


def test_canonical_task_id_short_resolves_to_full(repo: Path) -> None:
    _seed_task(repo, "0399-canonical-thing")
    assert wt_mod.canonical_task_id(repo, "0399") == "0399-canonical-thing"
    # full id resolves to itself (idempotent)
    assert (wt_mod.canonical_task_id(repo, "0399-canonical-thing")
            == "0399-canonical-thing")


def test_canonical_task_id_falls_back_when_unknown(repo: Path) -> None:
    # No task file: resolver returns the given id rather than crashing.
    assert wt_mod.canonical_task_id(repo, "0400") == "0400"


def test_worktree_create_uses_full_id_for_short_arg(repo: Path) -> None:
    _seed_task(repo, "0399-canonical-thing")
    path = wt_mod.worktree_create(repo, "0399", base="main")
    # Created at the FULL-id path / branch, not the short-id ones.
    assert path == repo / ".worktrees" / "0399-canonical-thing"
    assert path.is_dir()
    assert not (repo / ".worktrees" / "0399").exists()
    branches = _git(["branch", "--list"], repo).stdout
    assert "task/0399-canonical-thing" in branches
    assert "task/0399\n" not in branches and "task/0399 " not in branches


def test_worktree_path_for_full_after_canonicalization(repo: Path) -> None:
    _seed_task(repo, "0399-canonical-thing")
    policy = wt_mod.load_worktree_policy(repo)
    cid = wt_mod.canonical_task_id(repo, "0399")
    assert (policy.worktree_path_for(repo, cid)
            == repo / ".worktrees" / "0399-canonical-thing")


# ---------- 2. fingerprint uniqueness ----------


def test_fingerprint_distinct_for_distinct_tracked_diffs(repo: Path) -> None:
    wt_a = wt_mod.worktree_create(repo, "0401-a", base="main")
    wt_b = wt_mod.worktree_create(repo, "0402-b", base="main")
    (wt_a / "README.md").write_text("change A\n", encoding="utf-8")
    (wt_b / "README.md").write_text("totally different change B\n",
                                    encoding="utf-8")
    fp_a = compute_worktree_fingerprint(wt_a)
    fp_b = compute_worktree_fingerprint(wt_b)
    assert fp_a and fp_b and fp_a != "clean" and fp_b != "clean"
    assert fp_a != fp_b


def test_fingerprint_clean_tree_is_clean_not_a_collision(repo: Path) -> None:
    """The c474b1e3 collision came from fingerprinting the overlay-free
    main tree. A worktree with no overlay yields the 'clean' sentinel —
    never a content hash that could masquerade as a real overlay."""
    wt = wt_mod.worktree_create(repo, "0403-c", base="main")
    assert compute_worktree_fingerprint(wt) == "clean"


def test_fingerprint_includes_untracked_new_files(repo: Path) -> None:
    """Two worktrees whose overlay is ONLY a distinct new file must still
    fingerprint uniquely (untracked files are invisible to git diff HEAD)."""
    wt_a = wt_mod.worktree_create(repo, "0404-a", base="main")
    wt_b = wt_mod.worktree_create(repo, "0405-b", base="main")
    (wt_a / "new_a.py").write_text("print('a')\n", encoding="utf-8")
    (wt_b / "new_b.py").write_text("print('b')\n", encoding="utf-8")
    fp_a = compute_worktree_fingerprint(wt_a)
    fp_b = compute_worktree_fingerprint(wt_b)
    assert fp_a not in (None, "clean") and fp_b not in (None, "clean")
    assert fp_a != fp_b


# ---------- 3. merge captures the uncommitted overlay ----------


def test_merge_captures_uncommitted_overlay_to_main(repo: Path) -> None:
    _seed_task(repo, "0406-impl")
    wt = wt_mod.worktree_create(repo, "0406", base="main")
    # DEVELOPER overlay: a new file + a tracked modification, UNCOMMITTED.
    (wt / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
    (wt / "README.md").write_text("hi\nplus a line\n", encoding="utf-8")

    result = wt_mod.worktree_merge(repo, "0406")

    assert result.ok, result.message
    # main now carries the implementation — no phantom/empty merge.
    assert (repo / "feature.py").read_text(encoding="utf-8") == "VALUE = 42\n"
    assert "plus a line" in (repo / "README.md").read_text(encoding="utf-8")
    log = _git(["log", "--oneline", "main"], repo).stdout
    assert "0406" in log  # the captured-overlay commit + merge commit


def test_commit_overlay_noop_when_worktree_clean(repo: Path) -> None:
    wt_mod.worktree_create(repo, "0407-clean", base="main")
    policy = wt_mod.load_worktree_policy(repo)
    assert wt_mod._commit_worktree_overlay(repo, "0407-clean", policy) is False


def test_commit_overlay_returns_true_after_capture(repo: Path) -> None:
    wt = wt_mod.worktree_create(repo, "0408-dirty", base="main")
    (wt / "x.py").write_text("x = 1\n", encoding="utf-8")
    policy = wt_mod.load_worktree_policy(repo)
    assert wt_mod._commit_worktree_overlay(repo, "0408-dirty", policy) is True
    # the overlay is now a real commit on the task branch
    log = _git(["log", "--oneline", "task/0408-dirty"], repo).stdout
    assert "0408-dirty" in log
