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
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
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
    agent_env = str(tmp_path / "greatminds" / "agent-env" / "toy.env")
    assert f"EnvironmentFile=-{agent_env}" in body
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


def test_capture_agent_env_writes_private_allowlisted_env(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "REGISTRY_DIR", tmp_path / "greatminds")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    assert dm.capture_agent_env("toy") is True

    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    body = target.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY='secret value'" in body
    assert "UNRELATED_SECRET" not in body
    assert target.stat().st_mode & 0o777 == 0o600


def test_capture_agent_env_empty_shell_preserves_existing_file(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "REGISTRY_DIR", tmp_path / "greatminds")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    target.parent.mkdir(parents=True)
    target.write_text("ANTHROPIC_API_KEY=old\n", encoding="utf-8")
    target.chmod(0o600)
    for name in dm.AGENT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    assert dm.capture_agent_env("toy") is False
    assert target.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=old\n"
