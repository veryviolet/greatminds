"""Tests for task 0380: ``greatminds stand lease`` must give a
review_session task (EXPLORER) a legal per-task worktree path WITHOUT the
role running raw git.

Before 0380, ``--worktree`` was required AND had to already exist under
``.worktrees/<seq>[-slug]``. But review_session tasks get no per-task
worktree on route to review_sessions (worktrees.required_for_task_kinds is
product-only), and EXPLORER is forbidden from ``git worktree add``. So
``acquire_lease_via_stand_lease`` — the first step of EXPLORER's role
trigger — had no legal path, stranding the campaign.

0380 makes ``stand lease`` default ``--worktree`` to the canonical per-task
path and auto-create it (via the sanctioned ``worktree_create`` surface)
when absent. The main-fleet-tree foot-gun stays rejected, and the
auto-created worktree is cleaned up on review_session archive.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand as stand_mod
from greatminds.cli import task as task_mod
from greatminds.cli import worktree as wt_mod
from greatminds.core.errors import GreatMindsError


REVIEW_TASK = "0379-post-drain-avatar-toy-project-greatminds-exploratory-campaig"
SEQ = "0379"


# ---------- git project fixture ----------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _git_out(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_project(tmp_path: Path) -> Path:
    """A real git repo (one commit on ``main``) with a coordination dir and
    a review_session task — enough for worktree_create to resolve a base."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(["-c", "init.defaultBranch=main", "init"], project)
    _git(["config", "user.email", "t@t"], project)
    _git(["config", "user.name", "t"], project)
    (project / "README.md").write_text("x\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "init"], project)

    rs = project / "coordination" / "review_sessions"
    rs.mkdir(parents=True)
    (rs / f"{REVIEW_TASK}.yaml").write_text(
        yaml.safe_dump({
            "id": REVIEW_TASK,
            "stream": "review_session",
            "opened_by": "ARCHITECT-PLANNER",
            "opened_at": "2026-06-10T00:00:00Z",
            "target_functionality": "campaign",
            "scenarios": ["explore"],
            "stand_target": "avatar/smoke-only/main",
            "mode": "B",
        }),
        encoding="utf-8",
    )
    return project


# ---------- stale branch refresh for review_session leases ----------


def test_create_refreshes_stale_review_session_branch_to_current_main(
        tmp_path: Path) -> None:
    """0393: remove/create remediation must not reuse a stale
    review_session branch after dependency fixes land on main."""
    project = _git_project(tmp_path)
    old_main = _git_out(["rev-parse", "main"], project)
    (project / "fix.txt").write_text("verified dependency\n",
                                     encoding="utf-8")
    _git(["add", "fix.txt"], project)
    _git(["commit", "-m", "verified dependency fix"], project)
    new_main = _git_out(["rev-parse", "main"], project)
    assert old_main != new_main

    branch = f"task/{REVIEW_TASK}"
    _git(["branch", branch, old_main], project)

    created = wt_mod.worktree_create(project, REVIEW_TASK)
    assert created.is_dir()
    assert _git_out(["rev-parse", branch], project) == new_main
    assert _git_out(["rev-parse", "HEAD"], created) == new_main


def test_create_reuses_existing_product_branch_without_refreshing(
        tmp_path: Path) -> None:
    """0393 safety: implementation branches are not silently rewritten."""
    project = _git_project(tmp_path)
    task_id = "0393-product-branch"
    product_q = project / "coordination" / "feature_dev"
    product_q.mkdir(parents=True, exist_ok=True)
    (product_q / f"{task_id}.yaml").write_text(
        yaml.safe_dump({"id": task_id, "stream": "product",
                        "kind": "bugfix", "scope": "backend"}),
        encoding="utf-8",
    )
    old_main = _git_out(["rev-parse", "main"], project)
    (project / "later.txt").write_text("later main\n", encoding="utf-8")
    _git(["add", "later.txt"], project)
    _git(["commit", "-m", "later main"], project)
    new_main = _git_out(["rev-parse", "main"], project)
    assert old_main != new_main

    branch = f"task/{task_id}"
    _git(["branch", branch, old_main], project)

    created = wt_mod.worktree_create(project, task_id)
    assert created.is_dir()
    assert _git_out(["rev-parse", branch], project) == old_main
    assert _git_out(["rev-parse", "HEAD"], created) == old_main
    assert _git_out(["rev-parse", "main"], project) == new_main


# ---------- _resolve_or_create_lease_worktree ----------


def test_defaults_to_canonical_path_and_auto_creates(tmp_path: Path) -> None:
    """worktree=None → canonical ``.worktrees/<task_id>``, materialized."""
    project = _git_project(tmp_path)
    result = stand_mod._resolve_or_create_lease_worktree(
        REVIEW_TASK, None, project)
    expected = project / ".worktrees" / REVIEW_TASK
    assert Path(result) == expected.resolve()
    assert expected.is_dir() and (expected / ".git").exists()
    # the branch was created off main
    branches = subprocess.run(
        ["git", "branch", "--list", f"task/{REVIEW_TASK}"],
        cwd=str(project), capture_output=True, text=True).stdout
    assert f"task/{REVIEW_TASK}" in branches


def test_seq_only_path_creates_canonical_worktree(tmp_path: Path) -> None:
    """A shape-valid but non-existent ``.worktrees/0379`` hint still yields
    the canonical full-id worktree, created and returned."""
    project = _git_project(tmp_path)
    seq_hint = str(project / ".worktrees" / SEQ)
    result = stand_mod._resolve_or_create_lease_worktree(
        REVIEW_TASK, seq_hint, project)
    assert Path(result) == (project / ".worktrees" / REVIEW_TASK).resolve()
    assert Path(result).is_dir()


def test_existing_worktree_is_reused_not_recreated(tmp_path: Path) -> None:
    """When the worktree already exists, it is returned as-is (idempotent,
    no second ``git worktree add``)."""
    project = _git_project(tmp_path)
    created = wt_mod.worktree_create(project, REVIEW_TASK)
    marker = created / "MARKER"
    marker.write_text("keep\n", encoding="utf-8")
    result = stand_mod._resolve_or_create_lease_worktree(
        REVIEW_TASK, str(created), project)
    assert Path(result) == created.resolve()
    assert marker.exists(), "existing worktree must not be recreated"


def test_main_tree_foot_gun_still_rejected(tmp_path: Path) -> None:
    """The 0271 safety rule survives: the project dir itself is rejected
    BEFORE any auto-create."""
    project = _git_project(tmp_path)
    with pytest.raises(GreatMindsError) as exc:
        stand_mod._resolve_or_create_lease_worktree(
            REVIEW_TASK, str(project), project)
    assert ".worktrees" in str(exc.value)
    # nothing materialized
    assert not (project / ".worktrees" / REVIEW_TASK).exists()


# ---------- archive cleanup of a review_session worktree ----------


def test_archive_hook_removes_review_session_worktree(tmp_path: Path) -> None:
    """A review_session task has no ``kind`` but CAN own a lease worktree;
    the post-move archive hook must remove it (else it orphans)."""
    project = _git_project(tmp_path)
    coord = project / "coordination"
    created = wt_mod.worktree_create(project, REVIEW_TASK)
    assert created.is_dir()
    task_mod._worktree_hook_post_move(
        coord, REVIEW_TASK,
        {"stream": "review_session"}, "archive")
    assert not created.exists(), "review_session worktree must be cleaned"


# ---------- end-to-end through the Click command ----------


def _run_lease(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project)
    env["GREATMINDS_ROLE"] = "EXPLORER"
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "stand", "lease", *args],
        capture_output=True, text=True, env=env, cwd=str(project),
    )


def test_cli_lease_without_worktree_auto_creates(tmp_path: Path) -> None:
    """EXPLORER's real path: ``stand lease --task <rs> --profile smoke-only``
    with NO --worktree must succeed (auto-create) and mint a lease."""
    project = _git_project(tmp_path)
    (coord := project / "coordination" / ".stand").mkdir(parents=True)
    (coord / "state.yaml").write_text(
        yaml.safe_dump({"state": "free"}), encoding="utf-8")
    sp = project / "coordination" / "stand-profiles"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "smoke-only.yaml").write_text(
        yaml.safe_dump([{"name": "p", "hosts": "localhost", "tasks": []}]),
        encoding="utf-8")
    (project / "coordination" / "stand-profiles.yaml").write_text(
        yaml.safe_dump({
            "profiles": {
                "smoke-only": {
                    "file": "smoke-only.yaml",
                    "purpose": "review-session smoke validation",
                    "used_for": ["explorer_review", "quick_readiness"],
                    "default_for": ["explorer"],
                }
            }
        }),
        encoding="utf-8",
    )

    cp = _run_lease(project, "--task", REVIEW_TASK, "--profile", "smoke-only")
    assert cp.returncode == 0, (
        f"0380: lease without --worktree must auto-create + succeed. "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}")
    assert "lease_id" in cp.stdout
    assert (project / ".worktrees" / REVIEW_TASK).is_dir()
    # the lease recorded the absolute canonical worktree
    state = yaml.safe_load(
        (project / "coordination" / ".stand" / "state.yaml")
        .read_text(encoding="utf-8"))
    lease = (state.get("active_lease")
             or (state.get("lease_queue") or [{}])[0])
    assert str(project / ".worktrees" / REVIEW_TASK) in str(
        lease.get("worktree"))
