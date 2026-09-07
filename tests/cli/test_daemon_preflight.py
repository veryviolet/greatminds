import subprocess

import pytest
from click.testing import CliRunner

from greatminds.cli import daemon


@pytest.mark.parametrize('command', ['install', 'start', 'restart', 'repair'])
@pytest.mark.parametrize('problem', ['missing', 'malformed', 'native_transport', 'environment'])
def test_bad_configuration_fails_before_service_or_file_mutations(tmp_path, monkeypatch, command, problem):
    project = tmp_path/'project'
    (project/'coordination').mkdir(parents=True)
    path = project/'coordination/execution.yaml'
    if problem == 'malformed':
        path.write_text('{invalid')
    elif problem == 'native_transport':
        path.write_text('version: 1\nagents: {bad: {transport: native}}\nbindings: {}\n')
    elif problem == 'environment':
        path.write_text('version: 1\nagents: {}\nbindings: {}\n')
        (project/'.greatminds').mkdir()
        (project/'.greatminds/PROJECT.env').write_text('KEY="invalid secret')
    if command != 'install':
        daemon.register_project('fixture', project)
    before = {str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    calls = []
    monkeypatch.setattr(daemon, '_systemctl', lambda *args: calls.append(args))
    name_flag = '--name' if command in ('install', 'repair') else '--project'
    result = CliRunner().invoke(daemon.daemon, [command, name_flag, 'fixture', '--project-dir', str(project)] + (['--systemd'] if command in {'start', 'restart'} else []))
    assert result.exit_code != 0, result.output
    assert 'invalid secret' not in result.output
    assert calls == []
    assert before == {str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('command', ['start', 'restart', 'repair'])
def test_service_activation_requires_registration(tmp_path, monkeypatch, command):
    (tmp_path/'coordination').mkdir()
    (tmp_path/'coordination/execution.yaml').write_text('version: 1\nagents: {}\nbindings: {}\n')
    calls = []
    monkeypatch.setattr(daemon, '_systemctl', lambda *args: calls.append(args))
    flag = '--name' if command == 'repair' else '--project'
    result = CliRunner().invoke(daemon.daemon, [command, flag, 'fixture', '--project-dir', str(tmp_path)] + (['--systemd'] if command in {'start', 'restart'} else []))
    assert result.exit_code != 0
    assert 'not registered' in result.output
    assert not calls and not daemon.SYSTEMD_USER_DIR.exists()


def test_broken_project_can_still_be_stopped(tmp_path, monkeypatch):
    daemon.register_project('fixture', tmp_path)
    calls = []
    monkeypatch.setattr(daemon, '_systemctl', lambda *args:
        calls.append(args) or subprocess.CompletedProcess(args, 0, '', ''))
    result = CliRunner().invoke(daemon.daemon, ['stop', '--systemd', '--project', 'fixture'])
    assert result.exit_code == 0, result.output
    assert calls == [('stop', 'greatminds-daemon@fixture.service')]
