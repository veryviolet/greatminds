"""PROJECT.env → daemon environment via a systemd EnvironmentFile drop-in.

The clean injection point: one per-instance drop-in gives coordd — and
every driven agent it spawns (they inherit its process env) — the fleet's
PROJECT.env as real environment variables.
"""
from __future__ import annotations

from pathlib import Path

from greatminds.cli import daemon as dm


def test_install_project_dropin_writes_environmentfile(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)

    wrote = dm.install_project_dropin("toy", project)
    assert wrote is True

    conf = (tmp_path / "systemd"
            / "greatminds-daemon@toy.service.d" / "10-project-env.conf")
    body = conf.read_text(encoding="utf-8")
    # Optional (leading `-`) EnvironmentFile pointing at the fleet PROJECT.env.
    expected = str(project / "coordination" / "PROJECT.env")
    assert f"EnvironmentFile=-{expected}" in body
    assert "[Service]" in body


def test_install_project_dropin_is_idempotent(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)

    assert dm.install_project_dropin("toy", project) is True
    # Same inputs → no rewrite (no gratuitous daemon-reload churn).
    assert dm.install_project_dropin("toy", project) is False


def test_dropin_optional_dash_tolerates_missing_env_file(
    tmp_path: Path, monkeypatch,
) -> None:
    # The `-` prefix means a fleet with no PROJECT.env yet still gets a
    # valid unit (systemd silently skips the missing file).
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)
    dm.install_project_dropin("toy", project)
    conf = (tmp_path / "systemd"
            / "greatminds-daemon@toy.service.d" / "10-project-env.conf")
    assert "EnvironmentFile=-" in conf.read_text(encoding="utf-8")
