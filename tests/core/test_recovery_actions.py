import json

import pytest
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.runtime.recovery_actions import recovery_actions


@pytest.mark.parametrize('command', [
    ['run', 'command-resolve', 'operation', '--reason', 'inspected'],
    ['run', 'repair', '--operation', 'operation'],
    ['stand', 'deployment-resolve', 'operation', '--reason', 'inspected'],
    ['stand', 'deployment-recover', 'operation'],
])
def test_recovery_rejects_token_only_agent_context(command, monkeypatch):
    monkeypatch.delenv('GREATMINDS_RUN_ID', raising=False)
    monkeypatch.setenv('GREATMINDS_RUN_TOKEN', 'scoped-credential')
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 3, result.output
    assert 'operator' in result.output


def test_scoped_command_resolution_from_another_working_directory(tmp_path, monkeypatch):
    from test_commands import setup
    project = tmp_path/'project'
    project.mkdir()
    store, claim, service = setup(project)
    request = service.request(claim.run['id'], 'check', token=claim.token, request_id='uncertain')
    service._update(request['id'], status='needs_recovery')
    actions = recovery_actions('commands', 'operation_needs_recovery',
                               {'operation_id': request['id']}, project=project)
    action = next(item for item in actions if item['id'] == 'resolve_command')
    assert action['required_options'] == ['--reason']
    assert not action['automatic']
    other = tmp_path/'other'
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setenv('GREATMINDS_PROJECT_DIR', str(other))
    monkeypatch.delenv('GREATMINDS_RUN_ID', raising=False)
    monkeypatch.delenv('GREATMINDS_RUN_TOKEN', raising=False)
    argv = [*action['argv'][1:], '--reason', 'inspected external effects']
    result = CliRunner().invoke(cli, argv, env=action['environment'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['status'] == 'resolved'
    before = store.snapshot()
    repeat = CliRunner().invoke(cli, argv, env=action['environment'])
    assert repeat.exit_code == 0, repeat.output
    assert store.snapshot() == before
    assert list(other.iterdir()) == []


def test_recovery_descriptors_preserve_decision_boundaries(tmp_path):
    maintenance = recovery_actions('maintenance', 'operation_needs_recovery', {'operation_id': 'm'}, project=tmp_path)
    abandon = next(item for item in maintenance if item['id'] == 'abandon_operation')
    assert 'destination does not exist' in abandon['preconditions']
    assert abandon['effect'] == 'abandon_uncommitted_intent'
    deployment = recovery_actions('deployments', 'deployment_unresolved', {'operation_id': 'd'}, project=tmp_path)
    resolve = next(item for item in deployment if item['id'] == 'resolve_deployment')
    assert 'process cleanup is confirmed' in resolve['preconditions']
    assert resolve['effect'] == 'acknowledge_external_uncertainty'
    assert all(item['automatic'] is False for item in maintenance + deployment)
    assert recovery_actions('results', 'operation_needs_recovery', {'operation_id': 'r'}, project=tmp_path) == []
