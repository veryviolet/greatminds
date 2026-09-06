"""Regression tests for task 0091 item 2: git permissions pre-commit gate.

`greatminds check-git-permission commit` must reject commits when
$GREATMINDS_ROLE is not in schema.yaml's git_permissions.commit list.
The hook installed by `greatminds setup` invokes this command, so
direct CLI testing of the command is equivalent to hook testing.
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


def test_acp_setup_preserves_user_hook_and_never_installs_one(tmp_path):
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    hooks = tmp_path / ".git/hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\n# user-owned hook\nexit 0\n")
    before = hook.read_bytes()
    result = CliRunner().invoke(cli, ["setup", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert hook.read_bytes() == before
    hook.unlink()
    result = CliRunner().invoke(cli, ["setup", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not hook.exists()
