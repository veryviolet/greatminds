"""Regression tests for inbox kind validation (task 0091 item 1).

`greatminds inbox send --kind X` must reject any kind not listed in
schema.yaml's `inbox.allowed_kinds`. Before this fix, the CLI accepted
any string and downstream readers silently inherited garbage.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _gm(project_dir: Path, *argv: str, role: str = "DEVELOPER") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project_dir)
    env["GREATMINDS_ROLE"] = role
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *argv],
        capture_output=True, text=True, env=env,
    )


def _setup_project(tmp_path: Path) -> Path:
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
    return tmp_path


def test_inbox_send_accepts_canonical_kinds(tmp_path: Path) -> None:
    proj = _setup_project(tmp_path)
    for k in ("wake", "ask", "info"):
        cp = _gm(proj, "inbox", "send", "TESTER",
                 "--kind", k, "--body", f"test {k} body")
        assert cp.returncode == 0, (
            f"kind {k!r} rejected but should be allowed: rc={cp.returncode} "
            f"stderr={cp.stderr}"
        )


def test_inbox_send_rejects_unknown_kind(tmp_path: Path) -> None:
    proj = _setup_project(tmp_path)
    cp = _gm(proj, "inbox", "send", "TESTER",
             "--kind", "oops", "--body", "should fail")
    assert cp.returncode != 0, "unknown kind 'oops' must be rejected"
    combined = cp.stderr + cp.stdout
    assert "unknown inbox kind" in combined, (
        f"error should mention 'unknown inbox kind'; got: {combined!r}"
    )
    assert "allowed_kinds" in combined, (
        f"error should reference schema.inbox.allowed_kinds for discoverability"
    )


def test_inbox_send_rejects_empty_kind(tmp_path: Path) -> None:
    """Empty string is not 'wake'/'ask'/'info' either — must be refused."""
    proj = _setup_project(tmp_path)
    cp = _gm(proj, "inbox", "send", "TESTER",
             "--kind", "", "--body", "empty kind")
    assert cp.returncode != 0, "empty kind must be rejected"
