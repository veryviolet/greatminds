"""Public setup and managed execution have one ACP contract and transport."""
import importlib.util
import json

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.main import cli


def test_plain_setup_creates_an_empty_explicit_acp_contract_without_external_actions(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('setup invoked an external process')
    monkeypatch.setattr('subprocess.run', forbidden)
    result = CliRunner().invoke(cli, ['setup', '--project-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['bindings'] == 0
    assert yaml.safe_load((tmp_path/'coordination/execution.yaml').read_text()) == {
        'version': 1, 'agents': {}, 'bindings': {}}
    assert (tmp_path/'.greatminds/feature_dev').is_dir()
    assert not (tmp_path/'coordination/coord.yaml').exists()
    assert not (tmp_path/'.greatminds/bootstrap.md').exists()
    assert not (tmp_path/'.greatminds/.codex-home').exists()
    assert not (tmp_path/'.greatminds/.agent_registry').exists()
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert CliRunner().invoke(cli, ['setup', '--project-dir', str(tmp_path)]).exit_code == 0
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_default_daemon_runs_acp_from_nested_directory(tmp_path, monkeypatch):
    assert CliRunner().invoke(cli, ['setup', '--project-dir', str(tmp_path)]).exit_code == 0
    nested = tmp_path/'src';nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.delenv('GREATMINDS_PROJECT_DIR', raising=False)
    result = CliRunner().invoke(cli, ['coordd', '--once'])
    assert result.exit_code == 0, result.output
    assert (tmp_path/'.greatminds/.runtime').is_dir()
    assert not (tmp_path/'.greatminds/.agent_registry').exists()


@pytest.mark.parametrize('command', [['coordd', '--once'], ['launch', '--target', 'vscode']])
def test_missing_contract_never_selects_native_execution(tmp_path, command):
    config = tmp_path/'coordination';config.mkdir()
    (config/'coord.yaml').write_text('windows: [{role: DEVELOPER, tool: claude, mode: driven}]')
    before = list(config.iterdir())
    result = CliRunner().invoke(cli, [*command, '--project-dir', str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert 'cannot load execution config' in result.output
    assert list(config.iterdir()) == before
    assert not (tmp_path/'.greatminds').exists()


@pytest.mark.parametrize('name,module', [('start-agent', 'start_agent'), ('pty-launch', 'pty_launch')])
def test_native_launch_commands_and_modules_are_removed(name, module):
    result = CliRunner().invoke(cli, [name, '--help'])
    assert result.exit_code == 2
    assert 'No such command' in result.output
    assert importlib.util.find_spec('greatminds.cli.'+module) is None


@pytest.mark.parametrize('command', ['restart', 'migrate'])
def test_native_fleet_control_commands_are_not_public(command):
    result = CliRunner().invoke(cli, [command, '--help'])
    assert result.exit_code == 2
    assert 'No such command' in result.output


def test_setup_preserves_existing_runtime_tasks_and_custom_configuration(tmp_path):
    config=tmp_path/'coordination';config.mkdir()
    (config/'PROJECT.md').write_text('user project')
    queue=tmp_path/'.greatminds/feature_inbox';queue.mkdir(parents=True)
    (queue/'0001.md').write_text('user task')
    result = CliRunner().invoke(cli, ['setup', '--project-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (config/'PROJECT.md').read_text() == 'user project'
    assert (queue/'0001.md').read_text() == 'user task'
