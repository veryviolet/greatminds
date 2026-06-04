"""Driven turns must spawn the tool by its REAL absolute path.

coordd runs as a systemd-user daemon with a minimal PATH (no
``~/.local/bin``), so a bare ``claude`` / ``codex`` argv[0] failed to
spawn and the claude-driven roles (TESTER / DEVELOPER / UI / READER)
silently never ran. ``_resolve_tool_bin`` finds the real path (PATH →
login shell → bare), and the driven argv / env use it.
"""
from __future__ import annotations

import os

from greatminds.cli import coordd as cd


def _clear_cache():
    cd._TOOL_BIN_CACHE.clear()


def test_resolve_tool_bin_prefers_which(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(cd.shutil, "which",
                        lambda t: "/opt/tools/claude" if t == "claude" else None)
    assert cd._resolve_tool_bin("claude") == "/opt/tools/claude"


def test_resolve_tool_bin_falls_back_to_login_shell(monkeypatch, tmp_path):
    """When which misses (daemon's minimal PATH), the login shell resolves
    it — even a non-standard location."""
    _clear_cache()
    real = tmp_path / "weird" / "place" / "claude"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(cd.shutil, "which", lambda _t: None)

    import subprocess

    def fake_run(cmd, *_a, **_k):
        # the login-shell `command -v` probe
        assert cmd[0] == "bash" and "-lc" in cmd
        return subprocess.CompletedProcess(cmd, 0, f"{real}\n", "")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd._resolve_tool_bin("claude") == str(real.resolve())


def test_resolve_tool_bin_bare_when_unresolvable(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(cd.shutil, "which", lambda _t: None)
    import subprocess
    monkeypatch.setattr(
        cd.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", ""))
    assert cd._resolve_tool_bin("nope") == "nope"


def test_driven_claude_argv0_is_resolved_absolute(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(cd, "_resolve_tool_bin",
                        lambda t: f"/abs/{t}")
    argv = cd._build_driven_claude_argv("sid-123", None, fresh=False)
    assert argv[0] == "/abs/claude"          # NOT bare "claude"
    assert "--resume" in argv and "sid-123" in argv
    fresh = cd._build_driven_claude_argv("sid", None, fresh=True)
    assert fresh[0] == "/abs/claude"
    assert "--resume" not in fresh


def test_driven_subprocess_env_path_has_tool_dirs(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(cd, "_resolve_tool_bin", lambda t: f"/abs/{t}/bin/{t}")
    monkeypatch.setattr(cd.shutil, "which", lambda _t: None)
    env = cd._driven_subprocess_env("tester")
    assert env["GREATMINDS_ROLE"] == "TESTER"
    # the resolved claude / codex dirs are on PATH for child processes.
    assert "/abs/claude/bin" in env["PATH"].split(os.pathsep)
    assert "/abs/codex/bin" in env["PATH"].split(os.pathsep)
