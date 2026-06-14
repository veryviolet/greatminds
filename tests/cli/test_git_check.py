"""Regression tests for the optional git permissions checker.

`greatminds check-git-permission commit` must reject commits when
$GREATMINDS_ROLE is not in schema.yaml's git_permissions.commit list.
`greatminds setup` intentionally does not install a git hook that invokes
this command; normal operator-mode commits must remain ordinary git commits.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _gm_role(role: str, *argv: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_ROLE"] = role
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *argv],
        capture_output=True, text=True, env=env,
    )


def _gm_no_role(*argv: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("GREATMINDS_ROLE", None)
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *argv],
        capture_output=True, text=True, env=env,
    )


def test_check_git_permission_allows_reviewer_commit():
    cp = _gm_role("ARCHITECT-REVIEWER", "check-git-permission", "commit")
    assert cp.returncode == 0, (
        f"REVIEWER must be allowed to commit per schema.git_permissions; "
        f"got rc={cp.returncode} stderr={cp.stderr}"
    )


def test_check_git_permission_rejects_developer_commit():
    cp = _gm_role("DEVELOPER", "check-git-permission", "commit")
    assert cp.returncode != 0, (
        f"DEVELOPER must NOT be allowed to commit; got rc={cp.returncode}"
    )
    combined = cp.stderr + cp.stdout
    assert "DEVELOPER" in combined
    assert "git_permissions" in combined


def test_check_git_permission_rejects_unset_role():
    cp = _gm_no_role("check-git-permission", "commit")
    assert cp.returncode != 0, (
        "missing GREATMINDS_ROLE must result in commit refusal"
    )
    combined = cp.stderr + cp.stdout
    assert "$GREATMINDS_ROLE" in combined or "GREATMINDS_ROLE" in combined


def test_check_git_permission_push_also_gated():
    cp = _gm_role("DEVELOPER", "check-git-permission", "push")
    assert cp.returncode != 0, "DEVELOPER must NOT be allowed to push"


def test_setup_does_not_install_git_hook_when_git_dir_exists(tmp_path: Path):
    """setup must not install a role-gating pre-commit hook.

    The checker remains available as an explicit command, but automatic hooks
    break solo/operator mode and make ordinary commits depend on
    GREATMINDS_ROLE.
    """
    (tmp_path / ".git").mkdir()
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert not hook.exists(), "setup must not create .git/hooks/pre-commit"


def test_setup_skips_hook_when_no_git_dir(tmp_path: Path):
    """No .git/ → don't install (avoid creating .git/hooks/ for non-git projects)."""
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert not hook.exists(), (
        "setup must NOT create .git/hooks/ in non-git project"
    )
