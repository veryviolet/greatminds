"""Tests for GitHub issue #10 (task 0362): worktree path normalization
and project-root resolution.

Two related 1.6.x CLI-plumbing bugs:

Bug A — relative ``--worktree`` paths were stored verbatim in
``.stand/state.yaml``. coordd later re-resolved the string against ITS
OWN cwd (often ``/home/<user>`` under systemd-user with no
``WorkingDirectory=``), so ``is_deploy_safe`` rejected it as "unknown
worktree location" (rc 126). Fix: ``stand lease`` resolves the path to
absolute before storing.

Bug B — project-root resolution stopped at the first ``coordination/``
found while walking up from cwd. A stray write from inside a per-task
worktree could leave an orphan ``coordination/`` there, so a later
``greatminds`` verb run from the worktree read an empty coordination/
with no queues ("task not found"). Fix: ``find_coord_dir`` skips any
``coordination/`` nested under a ``.worktrees/`` ancestor and walks up
to the canonical project root.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from greatminds.core.paths import find_coord_dir
from greatminds.cli.stand_executor import is_deploy_safe


TASK_ID = "0362-github-10-worktree-path-normalization-and-project-root-resol"
SEQ = "0362"


# ---------------------------------------------------------------------------
# Bug B — find_coord_dir skips orphan coordination/ inside worktrees
# ---------------------------------------------------------------------------


def _project_with_worktree_orphan(tmp_path: Path) -> tuple[Path, Path]:
    """Build a project tree with the canonical coordination/ AND an
    orphan coordination/ inside a per-task worktree."""
    project = tmp_path / "proj"
    (project / "coordination" / "feature_test").mkdir(parents=True)
    worktree = project / ".worktrees" / SEQ
    # The orphan: an empty coordination/ with no queues, exactly what a
    # stray write from inside the worktree would leave behind.
    (worktree / "coordination").mkdir(parents=True)
    return project, worktree


def test_walk_skips_worktree_orphan_and_finds_project_root(
    tmp_path: Path,
) -> None:
    """Walking up from inside a worktree must skip the orphan and return
    the canonical project coordination/."""
    project, worktree = _project_with_worktree_orphan(tmp_path)
    cwd = os.getcwd()
    try:
        os.chdir(worktree)
        # Ensure no env override interferes.
        old = os.environ.pop("GREATMINDS_PROJECT_DIR", None)
        try:
            coord = find_coord_dir()
        finally:
            if old is not None:
                os.environ["GREATMINDS_PROJECT_DIR"] = old
    finally:
        os.chdir(cwd)
    assert coord == (project / "coordination").resolve(), (
        f"#10: find_coord_dir must skip the worktree orphan and return "
        f"the project coordination/, got {coord}"
    )
    # The skipped path must NOT be the orphan.
    assert ".worktrees" not in coord.parts


def test_env_project_dir_pointing_at_worktree_is_skipped(
    tmp_path: Path,
) -> None:
    """Even when GREATMINDS_PROJECT_DIR points AT the worktree, the
    orphan coordination/ under .worktrees/ must be skipped — the walk
    falls back to the real project root."""
    project, worktree = _project_with_worktree_orphan(tmp_path)
    cwd = os.getcwd()
    env_before = os.environ.get("GREATMINDS_PROJECT_DIR")
    try:
        os.chdir(worktree)
        os.environ["GREATMINDS_PROJECT_DIR"] = str(worktree)
        coord = find_coord_dir()
    finally:
        os.chdir(cwd)
        if env_before is None:
            os.environ.pop("GREATMINDS_PROJECT_DIR", None)
        else:
            os.environ["GREATMINDS_PROJECT_DIR"] = env_before
    assert coord == (project / "coordination").resolve(), (
        f"#10: GREATMINDS_PROJECT_DIR=<worktree> must not pin the orphan; "
        f"got {coord}"
    )


def test_nonstrict_fallback_under_worktree_roots_at_project(
    tmp_path: Path,
) -> None:
    """strict=False (pty-launch path) must never point at an orphan
    coordination/ inside the worktree: when cwd is under .worktrees/,
    the fallback roots at the directory holding .worktrees/."""
    project = tmp_path / "proj"
    worktree = project / ".worktrees" / SEQ
    worktree.mkdir(parents=True)
    # No coordination/ anywhere — exercise the fallback branch.
    coord = find_coord_dir(start=worktree, strict=False)
    assert coord == project / ".greatminds", (
        f"#10: non-strict fallback must root at project, not the "
        f"worktree orphan; got {coord}"
    )
    assert ".worktrees" not in coord.parts


# ---------------------------------------------------------------------------
# Bug A — stand lease stores an absolute worktree path
# ---------------------------------------------------------------------------


def _project_with_state(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump({"state": "free"}), encoding="utf-8",
    )
    sp = project / "coordination" / "stand-profiles"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "full-deploy.yaml").write_text(
        yaml.safe_dump([{"name": "p", "hosts": "localhost", "tasks": []}]),
        encoding="utf-8",
    )
    (project / "coordination" / "stand-profiles.yaml").write_text(
        yaml.safe_dump({
            "profiles": {
                "full-deploy": {
                    "file": "full-deploy.yaml",
                    "purpose": "full validation",
                    "used_for": ["tester_validation"],
                    "default_for": ["feature_test"],
                }
            }
        }),
        encoding="utf-8",
    )
    wt = project / ".worktrees" / SEQ
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    return project


def test_relative_worktree_stored_absolute(tmp_path: Path) -> None:
    """A relative ``--worktree`` must be persisted as an absolute path so
    coordd's deploy is independent of coordd's cwd."""
    project = _project_with_state(tmp_path)
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project)
    env["GREATMINDS_ROLE"] = "TESTER"
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "stand", "lease",
         "--task", TASK_ID,
         "--worktree", f".worktrees/{SEQ}",
         "--profile", "full-deploy"],
        capture_output=True, text=True, env=env, cwd=str(project),
    )
    assert cp.returncode == 0, (
        f"#10: relative worktree must be accepted. "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    state = yaml.safe_load(
        (project / "coordination" / ".stand" / "state.yaml")
        .read_text(encoding="utf-8")
    )
    stored = state["active_lease"]["worktree"]
    assert os.path.isabs(stored), (
        f"#10 Bug A: stored worktree must be absolute, got {stored!r}"
    )
    assert Path(stored) == (project / ".worktrees" / SEQ).resolve(), (
        f"#10 Bug A: stored worktree must resolve to the project "
        f"worktree, got {stored!r}"
    )


def test_stored_absolute_path_is_deploy_safe_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    """The downstream effect of Bug A: a deploy-safety check that resolves
    the STORED path must still pass even when run from an unrelated cwd
    (simulating coordd under systemd-user). A relative string would
    resolve against that foreign cwd and be rejected; the absolute one
    survives."""
    project = _project_with_state(tmp_path)
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project)
    env["GREATMINDS_ROLE"] = "TESTER"
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "stand", "lease",
         "--task", TASK_ID,
         "--worktree", f".worktrees/{SEQ}",
         "--profile", "full-deploy"],
        capture_output=True, text=True, env=env, cwd=str(project),
    )
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    state = yaml.safe_load(
        (project / "coordination" / ".stand" / "state.yaml")
        .read_text(encoding="utf-8")
    )
    stored = state["active_lease"]["worktree"]
    # Resolve from a foreign cwd — the absolute stored path is immune.
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    cwd = os.getcwd()
    try:
        os.chdir(foreign)
        safe, reason = is_deploy_safe(stored, "remote-host", project)
    finally:
        os.chdir(cwd)
    assert safe, (
        f"#10 Bug A: stored absolute worktree must be deploy-safe from "
        f"any cwd; got refused: {reason}"
    )
