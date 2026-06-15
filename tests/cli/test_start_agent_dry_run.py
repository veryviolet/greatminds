"""Regression tests for ``greatminds start-agent --dry-run`` (task 0031).

EXPLORER avatar dogfood (review_sessions/0019) caught two problems:

  1. ``--dry-run`` did not exist; ``start-agent EXPLORER claude --dry-run``
     fell through to ``EXTRA`` and was passed to ``claude``, which then
     failed at ``execvp`` on hosts without ``claude`` installed and left
     ``.greatminds/.agent_registry/explorer.session-id`` behind as a
     side effect.
  2. There was no read-only way to inspect which plugin dirs, MCP layers,
     and final argv ``start-agent`` would produce — the only way to
     learn was to actually launch.

The fix introduces a ``--dry-run`` flag that:
- prints role, tool, mode, project / canon dirs, session resume status,
  plugin layers, MCP layers, env vars, prompt preview, and final argv
- writes nothing under ``.greatminds/.agent_registry/``
- never exec's the tool

These tests pin both: (a) dry-run is side-effect-free, (b) the report
covers the required keys.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _setup_project(tmp_path: Path) -> Path:
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
    return tmp_path


def _start_agent_dry_run(project_dir: Path, role: str, tool: str,
                          *extra: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "start-agent", role, tool, "--dry-run", *extra],
        capture_output=True, text=True, env=env,
    )


def test_dry_run_exits_zero_and_does_not_exec_tool(tmp_path: Path):
    """--dry-run must not exec the tool — even when the tool isn't on PATH."""
    proj = _setup_project(tmp_path)
    cp = _start_agent_dry_run(proj, "EXPLORER", "claude")
    assert cp.returncode == 0, (
        f"--dry-run must exit 0 even with claude absent from PATH. "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )


def test_dry_run_writes_no_registry_files(tmp_path: Path):
    """--dry-run must not leave session-id or registry json behind."""
    proj = _setup_project(tmp_path)
    cp = _start_agent_dry_run(proj, "EXPLORER", "claude")
    assert cp.returncode == 0, cp.stderr

    registry_dir = proj / ".greatminds" / ".agent_registry"
    # setup creates the directory; that's fine. What we check is that
    # nothing matching explorer.* was written.
    explorer_artifacts = list(registry_dir.glob("explorer.*"))
    assert not explorer_artifacts, (
        f"--dry-run left artifacts under .agent_registry: {explorer_artifacts}"
    )


def test_dry_run_report_covers_required_keys(tmp_path: Path):
    """Report must include role, tool, mode, plugin layers, MCP, argv."""
    proj = _setup_project(tmp_path)
    cp = _start_agent_dry_run(proj, "EXPLORER", "claude")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout

    for needle in (
        "DRY RUN",
        "role:",
        "tool:",
        "mode:",
        "project_dir:",
        "canon_dir:",
        "session_id:",
        "GREATMINDS_ROLE=EXPLORER",
        "plugin layers",
        "mcp config layers",
        "argv (would exec)",
        "coordination-protocol",
        "role-explorer",
        "canon.json",
    ):
        assert needle in out, (
            f"--dry-run report missing {needle!r}.\nfull stdout:\n{out}"
        )


def test_dry_run_does_not_pass_flag_to_tool_argv(tmp_path: Path):
    """The 1.2.0 bug: --dry-run leaked into EXTRA and got passed to claude.

    The fix is a real click option; the flag must NOT appear in the
    final argv that would be exec'd.
    """
    proj = _setup_project(tmp_path)
    cp = _start_agent_dry_run(proj, "EXPLORER", "claude")
    assert cp.returncode == 0, cp.stderr

    # Find the argv line in the report.
    lines = cp.stdout.splitlines()
    try:
        idx = lines.index("argv (would exec):")
    except ValueError:
        pytest.fail(f"no argv line in dry-run output:\n{cp.stdout}")
    argv_line = lines[idx + 1]
    assert "--dry-run" not in argv_line, (
        f"--dry-run leaked into tool argv: {argv_line}"
    )


def test_dry_run_resume_reads_existing_session_id(tmp_path: Path):
    """When a session-id file exists, dry-run must show RESUME with that UUID
    and must NOT overwrite the file."""
    proj = _setup_project(tmp_path)
    sid_file = proj / ".greatminds" / ".agent_registry" / "explorer.session-id"
    sid_file.parent.mkdir(parents=True, exist_ok=True)
    pinned = "deadbeef-1111-2222-3333-444444444444"
    sid_file.write_text(pinned + "\n", encoding="utf-8")

    cp = _start_agent_dry_run(proj, "EXPLORER", "claude")
    assert cp.returncode == 0, cp.stderr
    assert pinned in cp.stdout
    assert "RESUME" in cp.stdout
    # File must remain untouched (same content).
    assert sid_file.read_text(encoding="utf-8").strip() == pinned


def test_dry_run_new_session_does_not_write_session_file(tmp_path: Path):
    """No prior session-id → dry-run shows NEW UUID but doesn't write it."""
    proj = _setup_project(tmp_path)
    sid_file = proj / ".greatminds" / ".agent_registry" / "explorer.session-id"
    # Ensure it doesn't already exist.
    assert not sid_file.exists()

    cp = _start_agent_dry_run(proj, "EXPLORER", "claude")
    assert cp.returncode == 0, cp.stderr
    assert "NEW (would write)" in cp.stdout
    assert not sid_file.exists(), (
        "dry-run with no prior session file must NOT write one"
    )


def test_dry_run_chat_mode_strips_loop_prefix(tmp_path: Path):
    """`--mode chat --dry-run` must show the prompt without /loop prefix."""
    proj = _setup_project(tmp_path)
    cp = _start_agent_dry_run(proj, "EXPLORER", "claude", "--mode", "chat")
    assert cp.returncode == 0, cp.stderr
    # The "prompt (first line, len=N):" block — first line must not start
    # with "/loop ".
    lines = cp.stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("prompt (first line"):
            preview = lines[i + 1].lstrip()
            assert not preview.startswith("/loop "), (
                f"chat mode preview leaked /loop prefix: {preview!r}"
            )
            break
    else:
        pytest.fail("no prompt block in dry-run output")
