"""Tests for the configurable worktrees.default_branch.

Pre-fix, worktree_merge hardcoded ``main`` (checkout/pull/merge) and the
base-commit fallback did ``rev-parse main`` — so a project could not run
its coordination on any branch other than main (every completed task's
merge forced work back to main). default_branch (default "main") makes
the base + merge target configurable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from greatminds.cli import worktree as wt_mod
from greatminds.cli.worktree import WorktreePolicy


def _capture(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None, check=True):
        calls.append(list(args))
        # rev-parse returns a sha so base resolution succeeds.
        out = "deadbeef\n" if args and args[0] == "rev-parse" else ""
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=out, stderr="")
    monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)
    return calls


# ---------- policy plumbing ----------


def test_default_branch_defaults_to_main():
    assert WorktreePolicy().default_branch == "main"


def test_canon_schema_carries_default_branch():
    p = wt_mod.load_worktree_policy()
    # canon ships default_branch: main — backward compatible.
    assert p.default_branch == "main"


# ---------- merge retargets to default_branch ----------


def test_merge_targets_configured_branch(tmp_path: Path, monkeypatch):
    calls = _capture(monkeypatch)
    policy = WorktreePolicy(default_branch="unify")
    res = wt_mod.worktree_merge(tmp_path, "t1", policy=policy)
    assert res.ok
    assert calls[0] == ["checkout", "unify"], calls[0]
    pull = next(c for c in calls if c and c[0] == "pull")
    assert pull[:4] == ["pull", "--ff-only", "origin", "unify"]
    # the merged branch is still the task branch, into unify.
    merge = next(c for c in calls if c and c[0] == "merge"
                 and "--abort" not in c)
    assert "task/t1" in merge


def test_merge_still_main_by_default(tmp_path: Path, monkeypatch):
    """No policy → canon default_branch (main): unchanged behaviour."""
    calls = _capture(monkeypatch)
    wt_mod.worktree_merge(tmp_path, "t1")
    assert calls[0] == ["checkout", "main"]


# ---------- base fallback uses default_branch ----------


# ---------- per-project override via coord.yaml ----------


def test_coord_yaml_overrides_default_branch(tmp_path: Path):
    """A project pins its working branch in coord.yaml (durable across
    upgrades), NOT in the package schema."""
    (tmp_path / "coord.yaml").write_text(
        "session: proj\nworktrees:\n  default_branch: unify\n",
        encoding="utf-8")
    p = wt_mod.load_worktree_policy(tmp_path)
    assert p.default_branch == "unify"


def test_coord_yaml_override_under_coordination(tmp_path: Path):
    (tmp_path / "coordination").mkdir()
    (tmp_path / "coordination" / "coord.yaml").write_text(
        "session: proj\nworktrees:\n  default_branch: dev\n", encoding="utf-8")
    assert wt_mod.load_worktree_policy(tmp_path).default_branch == "dev"


def test_no_coord_override_falls_back_to_canon_main(tmp_path: Path):
    # no coord.yaml in project → canon default (main).
    assert wt_mod.load_worktree_policy(tmp_path).default_branch == "main"


def test_no_project_dir_uses_canon_only(tmp_path: Path):
    assert wt_mod.load_worktree_policy().default_branch == "main"


def test_base_fallback_uses_default_branch(tmp_path: Path, monkeypatch):
    calls = _capture(monkeypatch)
    # no coordination/task → find_task yields nothing → fallback to
    # rev-parse <default_branch>.
    sha = wt_mod._resolve_base_commit(tmp_path, "t1", None,
                                      default_branch="unify")
    assert sha == "deadbeef"
    rev = next(c for c in calls if c and c[0] == "rev-parse")
    assert rev == ["rev-parse", "unify"]
