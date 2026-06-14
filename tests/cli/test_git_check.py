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


def test_setup_installs_hook_when_git_dir_exists(tmp_path: Path):
    """greatminds setup must install .git/hooks/pre-commit when .git/ is
    present in the project root."""
    # Mock a .git/ dir so setup will install.
    (tmp_path / ".git").mkdir()
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.is_file(), (
        "setup must install .git/hooks/pre-commit when .git/ is present"
    )
    body = hook.read_text(encoding="utf-8")
    assert "check-git-permission commit" in body
    # And it must be executable.
    assert hook.stat().st_mode & 0o111, "pre-commit hook must be executable"


def test_pre_commit_hook_uses_absolute_python_not_bare_greatminds(
    tmp_path: Path,
) -> None:
    """REVIEWER 0091 iter blocker: bare `greatminds` in the hook fails
    when git's sanitized PATH lacks .venv/bin. The hook must invoke
    the project's Python via `sys.executable -m greatminds.cli.main`
    so it works regardless of PATH.
    """
    (tmp_path / ".git").mkdir()
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0
    hook_text = (tmp_path / ".git" / "hooks" / "pre-commit").read_text(
        encoding="utf-8"
    )
    # Must NOT use bare `greatminds` (PATH-dependent).
    for line in hook_text.splitlines():
        line_no_comment = line.split("#", 1)[0].strip()
        if line_no_comment.startswith("exec "):
            assert "exec greatminds " not in line, (
                f"hook must not invoke bare `greatminds` (PATH-dependent): "
                f"{line!r}"
            )
            # Must reference the python binary that setup ran under.
            assert sys.executable in line, (
                f"hook must pin to sys.executable={sys.executable}: {line!r}"
            )
            assert "-m greatminds.cli.main" in line


def test_pre_commit_hook_actually_runnable_blocks_developer(
    tmp_path: Path,
) -> None:
    """Run the installed hook as a real shell script with
    GREATMINDS_ROLE=DEVELOPER; must exit non-zero (refuse) regardless
    of the caller's PATH. Proves the absolute-python fix works
    end-to-end."""
    (tmp_path / ".git").mkdir()
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    # Run the hook with a minimal PATH (no .venv/bin) — DEVELOPER must
    # still get refused (not "command not found").
    env = {"PATH": "/usr/bin:/bin",
           "GREATMINDS_ROLE": "DEVELOPER"}
    rc = subprocess.run([str(hook)], env=env, capture_output=True, text=True)
    assert rc.returncode != 0, (
        f"hook must refuse DEVELOPER commit; got rc={rc.returncode}, "
        f"stdout={rc.stdout!r}, stderr={rc.stderr!r}"
    )
    combined = rc.stdout + rc.stderr
    assert "DEVELOPER" in combined or "git_permissions" in combined, (
        f"hook output must explain why refused (not 'command not found'): "
        f"{combined!r}"
    )


def test_pre_commit_hook_actually_runnable_allows_reviewer(
    tmp_path: Path,
) -> None:
    """Counterpart: ARCHITECT-REVIEWER passes the same hook."""
    (tmp_path / ".git").mkdir()
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    env = {"PATH": "/usr/bin:/bin",
           "GREATMINDS_ROLE": "ARCHITECT-REVIEWER"}
    rc = subprocess.run([str(hook)], env=env, capture_output=True, text=True)
    assert rc.returncode == 0, (
        f"ARCHITECT-REVIEWER must be allowed by hook; got rc={rc.returncode}, "
        f"stdout={rc.stdout!r}, stderr={rc.stderr!r}"
    )


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
