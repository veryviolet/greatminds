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
