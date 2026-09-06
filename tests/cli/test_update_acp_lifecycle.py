import subprocess

import pytest
from click.testing import CliRunner

from greatminds.cli import daemon, update
from greatminds.runtime.bootstrap import bootstrap


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path/'project'
    root.mkdir()
    bootstrap(root)
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize('registered', [False, True])
def test_update_does_not_install_services(project, monkeypatch, registered):
    if registered:
        daemon.register_project('fixture', project)
    def forbidden(*args, **kwargs):
        raise AssertionError('service installation or subprocess is not allowed')
    monkeypatch.setattr(update.subprocess, 'run', forbidden)
    monkeypatch.setattr(update, '_installed_version_fresh', lambda: 'fixture')
    result = CliRunner().invoke(update.update, ['--post-pip'])
    assert result.exit_code == 0, result.output
    assert not daemon.SYSTEMD_USER_DIR.exists()
    assert not daemon.AGENT_ENV_DIR.exists()


def test_installed_service_refresh_uses_try_restart(project, monkeypatch):
    daemon.register_project('fixture', project)
    daemon.install_template_unit()
    daemon.install_project_dropin('fixture', project)
    calls = []
    monkeypatch.setattr(daemon, '_systemctl', lambda *args:
        calls.append(args) or subprocess.CompletedProcess(args, 0, '', ''))
    monkeypatch.setattr(update, '_installed_version_fresh', lambda: 'fixture')
    result = CliRunner().invoke(update.update, ['--post-pip'])
    assert result.exit_code == 0, result.output
    assert calls == [('daemon-reload',), ('try-restart', 'greatminds-daemon@fixture.service')]
    assert 'inactive state preserved' in result.output


def test_explicit_project_refreshes_registered_root_not_cwd(project, tmp_path, monkeypatch):
    daemon.register_project('fixture', project)
    other = tmp_path/'other'
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setattr(update, '_installed_version_fresh', lambda: 'fixture')
    result = CliRunner().invoke(update.update, ['--post-pip', '--project', 'fixture'])
    assert result.exit_code == 0, result.output
    assert list(other.iterdir()) == []


def test_reload_failure_prevents_try_restart(project, monkeypatch):
    daemon.register_project('fixture', project)
    daemon.install_template_unit()
    daemon.install_project_dropin('fixture', project)
    calls = []
    monkeypatch.setattr(daemon, '_systemctl', lambda *args:
        calls.append(args) or subprocess.CompletedProcess(args, 1, '', 'synthetic failure'))
    result = CliRunner().invoke(update.update, ['--post-pip'])
    assert result.exit_code != 0
    assert calls == [('daemon-reload',)]


def test_self_replacement_preserves_project_and_interpreter_path(monkeypatch):
    monkeypatch.setattr(update.sys, 'executable', '/venv with spaces/bin/python')
    calls = []
    monkeypatch.setattr(update.os, 'execv', lambda path, argv: calls.append((path, argv)))
    update._self_replace_to_post_pip('fixture')
    assert calls == [('/venv with spaces/bin/python', ['/venv with spaces/bin/python', '-m',
        'greatminds.cli.main', 'update', '--post-pip', '--project', 'fixture'])]


def test_current_package_still_refreshes_project(project, monkeypatch):
    calls = []
    monkeypatch.setattr(update, '_step_pip_upgrade', lambda major: False)
    monkeypatch.setattr(update, '_refresh_project', lambda name: calls.append(name))
    monkeypatch.setattr(update, '_installed_version_fresh', lambda: 'fixture')
    result = CliRunner().invoke(update.update, [])
    assert result.exit_code == 0, result.output
    assert calls == [None]
