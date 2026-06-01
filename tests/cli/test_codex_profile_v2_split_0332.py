"""Tests for task 0332: codex 0.135 CONFIG_PROFILE_V2 — `greatminds
setup` must emit a SPLIT per-role codex home (base ``config.toml``
WITHOUT a ``[profiles.<role>]`` table + sibling ``<role>.config.toml``
profile layer), so ``--profile <role>`` does not trip the 0.135
legacy-profile error.

codex 0.135 layers ``$CODEX_HOME/<role>.config.toml`` on top of
config.toml and REJECTS a ``[profiles.<role>]`` table / top-level
``profile=`` selector in config.toml when ``--profile`` is passed:
  "--profile `<role>` cannot be used while .../config.toml contains
   legacy `profile`/`[profiles.<role>]` config; move those settings
   into .../<role>.config.toml".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import setup as setup_mod
from greatminds.cli import daemon as daemon_mod


@pytest.fixture(autouse=True)
def _isolate_daemon(tmp_path, monkeypatch):
    reg_dir = tmp_path / ".config" / "greatminds"
    monkeypatch.setattr(daemon_mod, "REGISTRY_DIR", reg_dir)
    monkeypatch.setattr(daemon_mod, "REGISTRY_PATH",
                        reg_dir / "projects.json")
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR",
                        tmp_path / ".config" / "systemd" / "user")


def _setup(tmp_path: Path) -> Path:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    result = CliRunner().invoke(
        setup_mod.setup, ["--project-dir", str(project_dir)],
        catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return project_dir


def _codex_homes(project_dir: Path) -> list[Path]:
    root = project_dir / "coordination" / ".codex-home"
    return sorted(p for p in root.iterdir() if p.is_dir())


def test_setup_emits_codex_homes(tmp_path: Path) -> None:
    homes = _codex_homes(_setup(tmp_path))
    assert homes, "0332: setup must create per-role codex homes"
    # the shipped codex roles
    names = {h.name for h in homes}
    assert {"explorer", "technical-writer", "developer"} <= names


def test_base_config_has_no_profile_table(tmp_path: Path) -> None:
    """0332: every base config.toml must NOT contain a [profiles.X]
    table nor a top-level `profile=` selector (the 0.135 trip wires)."""
    for home in _codex_homes(_setup(tmp_path)):
        base = (home / "config.toml").read_text(encoding="utf-8")
        assert "[profiles." not in base, (
            f"0332: {home.name}/config.toml still has a [profiles.X] "
            f"table — trips codex 0.135 --profile")
        assert not re.search(r'^profile\s*=', base, re.M), (
            f"0332: {home.name}/config.toml has a top-level profile= "
            f"selector — trips codex 0.135 --profile")


def test_sibling_layer_carries_profile_keys(tmp_path: Path) -> None:
    """0332: each <role>.config.toml layer must carry the profile keys
    (model / approval_policy / sandbox_mode) as TOP-LEVEL keys."""
    for home in _codex_homes(_setup(tmp_path)):
        layer = home / f"{home.name}.config.toml"
        assert layer.is_file(), (
            f"0332: missing profile layer {home.name}/{home.name}.config.toml")
        text = layer.read_text(encoding="utf-8")
        assert re.search(r'^model\s*=', text, re.M), (
            f"0332: {layer.name} missing top-level model=")
        assert re.search(r'^approval_policy\s*=', text, re.M)
        assert re.search(r'^sandbox_mode\s*=', text, re.M)
        # The layer is top-level — no [profiles.X] wrapper.
        assert "[profiles." not in text


def test_base_retains_developer_instructions_and_trust(tmp_path: Path):
    """The base keeps developer_instructions and gains per-project trust
    (so a CODEX_HOME-scoped launch is non-interactive)."""
    project_dir = _setup(tmp_path)
    proj = str(project_dir.resolve())
    for home in _codex_homes(project_dir):
        base = (home / "config.toml").read_text(encoding="utf-8")
        assert "developer_instructions" in base
        assert f'[projects."{proj}"]' in base
        assert 'trust_level = "trusted"' in base


def test_idempotent_existing_home_not_overwritten(tmp_path: Path) -> None:
    """Re-running setup must NOT clobber an operator-customized base
    config.toml (idempotency preserved)."""
    project_dir = _setup(tmp_path)
    explorer_base = (project_dir / "coordination" / ".codex-home"
                     / "explorer" / "config.toml")
    explorer_base.write_text("# operator edit\n", encoding="utf-8")
    result = CliRunner().invoke(
        setup_mod.setup, ["--project-dir", str(project_dir)],
        catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert explorer_base.read_text(encoding="utf-8") == "# operator edit\n"
