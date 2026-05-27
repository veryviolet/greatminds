"""Tests for task 0299: ``greatminds update`` branches by env
manager + skips tmux restart when the session was absent.

Pre-0299 the update flow had two failure modes:
  1. Under uv, ``<py> -m pip install --upgrade greatminds`` wrote
     the new version into the venv but left ``uv.lock`` pointing
     at the old one. Next ``uv run`` snapped back. Infinite loop.
  2. ``update`` always called ``greatminds restart`` which started
     a tmux session even when the operator had deliberately killed
     it.

0299 closes both: ``_upgrade_command_for_env`` picks the lockfile-
aware command per detected env_type; ``_step_restart_agents`` first
runs ``tmux has-session`` and skips the restart when the session
isn't running.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from greatminds.cli import update as update_mod


# ---------- _upgrade_command_for_env ----------


def test_uv_env_uses_lock_command(tmp_path: Path) -> None:
    """0299: uv → ``uv lock --upgrade-package greatminds`` (then
    ``uv sync`` runs as a follow-up step)."""
    cmd = update_mod._upgrade_command_for_env("uv", tmp_path)
    assert cmd == ["uv", "lock", "--upgrade-package", "greatminds"]


def test_poetry_env_uses_poetry_update(tmp_path: Path) -> None:
    cmd = update_mod._upgrade_command_for_env("poetry", tmp_path)
    assert cmd[:2] == ["poetry", "update"]
    assert "greatminds" in cmd
    assert "--directory" in cmd


def test_pixi_env_uses_pixi_update(tmp_path: Path) -> None:
    cmd = update_mod._upgrade_command_for_env("pixi", tmp_path)
    assert cmd[:2] == ["pixi", "update"]
    assert "greatminds" in cmd


def test_conda_env_uses_conda_update(tmp_path: Path) -> None:
    cmd = update_mod._upgrade_command_for_env("conda", tmp_path)
    assert cmd[:3] == ["conda", "update", "-y"]
    assert "greatminds" in cmd


def test_venv_env_falls_back_to_pip(tmp_path: Path) -> None:
    """Plain venv has no lockfile to maintain → pip path."""
    cmd = update_mod._upgrade_command_for_env("venv", tmp_path)
    assert cmd == [sys.executable, "-m", "pip", "install",
                    "--upgrade", "greatminds"]


def test_external_venv_falls_back_to_pip(tmp_path: Path) -> None:
    cmd = update_mod._upgrade_command_for_env(
        "external-venv", tmp_path)
    assert cmd[0] == sys.executable
    assert "pip" in cmd


def test_no_env_type_falls_back_to_pip(tmp_path: Path) -> None:
    """No env manager detected → pip (the pre-0299 path)."""
    cmd = update_mod._upgrade_command_for_env(None, tmp_path)
    assert cmd[0] == sys.executable
    assert "pip" in cmd


# ---------- _tmux_session_present ----------


def test_tmux_session_present_returns_false_when_session_none(
    monkeypatch,
) -> None:
    """None session → False; tmux is not even consulted."""
    monkeypatch.setattr(
        update_mod.shutil, "which",
        lambda _name: pytest.fail("0299: must not call tmux when "
                                    "session is None"),
    )
    assert update_mod._tmux_session_present(None) is False


def test_tmux_session_present_returns_false_when_tmux_missing(
    monkeypatch,
) -> None:
    """tmux not on PATH → False (no session can exist)."""
    monkeypatch.setattr(update_mod.shutil, "which", lambda _n: None)
    assert update_mod._tmux_session_present("greatminds") is False


def test_tmux_session_present_true_when_has_session_succeeds(
    monkeypatch,
) -> None:
    """``tmux has-session -t <session>`` returns 0 → True."""
    monkeypatch.setattr(update_mod.shutil, "which",
                         lambda _n: "/fake/bin/tmux")
    monkeypatch.setattr(
        update_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""),
    )
    assert update_mod._tmux_session_present("greatminds") is True


def test_tmux_session_present_false_when_has_session_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(update_mod.shutil, "which",
                         lambda _n: "/fake/bin/tmux")
    monkeypatch.setattr(
        update_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=""),
    )
    assert update_mod._tmux_session_present("greatminds") is False


def test_tmux_session_present_false_on_subprocess_timeout(
    monkeypatch,
) -> None:
    """Defensive: hung ``tmux has-session`` → False (no presumed
    session). Don't propagate TimeoutExpired."""
    monkeypatch.setattr(update_mod.shutil, "which",
                         lambda _n: "/fake/bin/tmux")

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=10)
    monkeypatch.setattr(update_mod.subprocess, "run", fake_run)
    assert update_mod._tmux_session_present("greatminds") is False


# ---------- _step_restart_agents tmux gate ----------


def test_restart_agents_skips_when_session_absent(
    monkeypatch, capsys,
) -> None:
    """Tmux session not running → ``_step_restart_agents`` skips
    the ``greatminds restart`` subprocess entirely and logs the
    skip-info line."""
    monkeypatch.setattr(
        update_mod, "_resolve_session_from_coord_yaml",
        lambda: "greatminds",
    )
    monkeypatch.setattr(
        update_mod, "_tmux_session_present", lambda _s: False,
    )
    monkeypatch.setattr(
        update_mod.subprocess, "run",
        lambda *a, **k: pytest.fail(
            "0299: restart must NOT subprocess when tmux absent"),
    )
    # Should not raise — graceful skip.
    update_mod._step_restart_agents()
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "absent" in out
    assert "skipping" in out.lower()


def test_restart_agents_runs_when_session_present(
    monkeypatch,
) -> None:
    """Tmux session running → falls through to the legacy restart
    subprocess (the pre-0299 behavior)."""
    monkeypatch.setattr(
        update_mod, "_resolve_session_from_coord_yaml",
        lambda: "greatminds",
    )
    monkeypatch.setattr(
        update_mod, "_tmux_session_present", lambda _s: True,
    )
    monkeypatch.setattr(
        update_mod, "_greatminds_bin",
        lambda: "/fake/bin/greatminds",
    )
    called: list = []
    monkeypatch.setattr(
        update_mod.subprocess, "run",
        lambda cmd, **kw: called.append(cmd) or
        subprocess.CompletedProcess(args=cmd, returncode=0,
                                      stdout="", stderr=""),
    )
    update_mod._step_restart_agents()
    assert called, "0299: restart must subprocess when tmux present"
    assert "restart" in called[0]


# ---------- _resolve_session_from_coord_yaml ----------


def test_resolve_session_returns_session_when_present(
    tmp_path: Path, monkeypatch,
) -> None:
    """coord.yaml with ``session: my-fleet`` → returns 'my-fleet'."""
    import yaml as _yaml
    (tmp_path / "coord.yaml").write_text(
        _yaml.safe_dump({"session": "my-fleet"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert update_mod._resolve_session_from_coord_yaml() == "my-fleet"


def test_resolve_session_returns_none_when_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert update_mod._resolve_session_from_coord_yaml() is None


def test_resolve_session_returns_none_on_malformed_yaml(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "coord.yaml").write_text(
        "{:not yaml:", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert update_mod._resolve_session_from_coord_yaml() is None
