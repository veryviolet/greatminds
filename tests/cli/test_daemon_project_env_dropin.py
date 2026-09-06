"""PROJECT.env → daemon environment via a systemd EnvironmentFile drop-in.

The clean injection point: one per-instance drop-in gives coordd — and
every driven agent it spawns (they inherit its process env) — the fleet's
PROJECT.env as real environment variables.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from greatminds.cli import daemon as dm


def test_install_project_dropin_writes_environmentfile(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    project = tmp_path / "proj"
    (project / ".greatminds").mkdir(parents=True)

    wrote = dm.install_project_dropin("toy", project)
    assert wrote is True

    conf = (tmp_path / "systemd"
            / "greatminds-daemon@toy.service.d" / "10-project-env.conf")
    body = conf.read_text(encoding="utf-8")
    # Optional (leading `-`) EnvironmentFile pointing at the fleet PROJECT.env.
    expected = str(project / ".greatminds" / "PROJECT.env")
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


def test_daemon_candidate_env_layers_project_then_agent_env(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "from-shell")
    project = tmp_path / "proj"
    (project / ".greatminds").mkdir(parents=True)
    (project / ".greatminds" / "PROJECT.env").write_text(
        "ANTHROPIC_BASE_URL=from-project\n"
        "PROJECT_ONLY='two words'\n",
        encoding="utf-8",
    )
    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    target.parent.mkdir(parents=True)
    target.write_text(
        "ANTHROPIC_BASE_URL=from-agent\n"
        "CLAUDE_CODE_OAUTH_TOKEN='secret token'\n",
        encoding="utf-8",
    )

    env = dm._daemon_candidate_env("toy", project)

    assert env["ANTHROPIC_BASE_URL"] == "from-agent"
    assert env["PROJECT_ONLY"] == "two words"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "secret token"


def test_capture_follows_manifest_references_and_prunes_removed_names(tmp_path, monkeypatch):
    import yaml
    project = tmp_path/'project'
    (project/'coordination').mkdir(parents=True)
    path = project/'coordination/execution.yaml'
    document = {'version': 1, 'agents': {'fixture': {'transport': 'acp', 'argv': ['/fake/acp'],
        'adapter_version': 'fixture', 'harness_version': 'fixture',
        'environment': {'API_KEY': 'FIXTURE_SOURCE'}, 'required_env': ['FIXTURE_REQUIRED']}}, 'bindings': {}}
    path.write_text(yaml.safe_dump(document))
    monkeypatch.setenv('FIXTURE_SOURCE', 'synthetic secret')
    monkeypatch.setenv('FIXTURE_REQUIRED', 'synthetic required')
    monkeypatch.setenv('UNRELATED_SECRET', 'must not be captured')
    assert dm.capture_agent_env('fixture', project)
    target = dm._agent_env_file('fixture')
    assert dm._parse_env_file(target) == {'FIXTURE_SOURCE': 'synthetic secret', 'FIXTURE_REQUIRED': 'synthetic required'}
    assert target.stat().st_mode & 0o777 == 0o600
    monkeypatch.delenv('FIXTURE_SOURCE')
    assert not dm.capture_agent_env('fixture', project)
    document['agents']['fixture']['environment'] = {}
    path.write_text(yaml.safe_dump(document))
    assert dm.capture_agent_env('fixture', project)
    assert dm._parse_env_file(target) == {'FIXTURE_REQUIRED': 'synthetic required'}
    document['agents'] = {}
    path.write_text(yaml.safe_dump(document))
    assert dm.capture_agent_env('fixture', project)
    assert target.read_text() == ''


def test_environment_parse_error_does_not_expose_secret(tmp_path):
    import pytest
    import click
    path = tmp_path/'invalid.env'
    path.write_text('KEY="never-print-this')
    with pytest.raises(click.ClickException) as caught:
        dm._parse_env_file(path)
    assert 'never-print-this' not in str(caught.value)
    assert 'invalid environment file syntax' in str(caught.value)


def test_dropin_escapes_spaces_and_systemd_specifiers(tmp_path, monkeypatch):
    project = tmp_path/'project %h with spaces'
    monkeypatch.setattr(dm, 'AGENT_ENV_DIR', tmp_path/'auth %i with spaces')
    dm.install_project_dropin('fixture', project)
    body = (dm._project_dropin_dir('fixture')/'10-project-env.conf').read_text()
    assert f'EnvironmentFile="-{project.parent}/project %%h with spaces/.greatminds/PROJECT.env"' in body
    assert f'EnvironmentFile="-{tmp_path}/auth %%i with spaces/fixture.env"' in body
    assert body.count('EnvironmentFile=') == 2
