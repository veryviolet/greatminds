"""Run the exact public recovery action emitted by aggregate diagnosis."""
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

from greatminds.runtime.commands import CommandService
from greatminds.runtime.observation import configuration
from greatminds.runtime.store import RunStore, TaskRevision
from test_acp_daemon import project


def test_doctor_command_repair_and_repeat_preserve_task_without_any_executor(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    project(root)
    config_path = root / 'coordination/execution.yaml'
    document = yaml.safe_load(config_path.read_text())
    document['commands'] = {'check': {'argv': [sys.executable, '-c',
        "from pathlib import Path; Path('COMMAND_EXECUTED').write_text('unexpected')"],
        'roles': ['DEVELOPER']}}
    config_path.write_text(yaml.safe_dump(document))
    schema, config = configuration(root)
    store = RunStore(root / '.greatminds')
    task_path = store.runtime / 'feature_dev/0001-example.yaml'
    task_before = task_path.read_bytes()
    claim = store.claim(task=TaskRevision.capture(store.runtime, task_path), binding=config.bindings[0],
                        config=config, schema=schema, project=root, owner_id='fixture')
    for status in ('starting', 'running'):
        store.transition(claim.run['id'], owner_id='fixture', target=status, event_id=status)
    commands = CommandService(store, environment={})
    request = commands.request(claim.run['id'], 'check', token=claim.token)
    commands._update(request['id'], status='needs_recovery', reason='injected_missing_completion')
    store.recover_run(claim.run['id'], previous_owner='fixture', owner_id='recovery')
    environment = {'PATH': os.pathsep.join((str(Path(sys.executable).parent), '/usr/bin', '/bin'))}
    def doctor():
        result = subprocess.run([sys.executable, '-I', '-m', 'greatminds.cli.main', 'run', 'doctor',
            '--project-dir', str(root), '--json'], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=15)
        assert result.returncode in (0, 1), result.stderr
        return json.loads(result.stdout)
    before = store.path.read_bytes()
    diagnosis = doctor()
    assert store.path.read_bytes() == before
    finding = next(item for item in diagnosis['findings'] if item['component'] == 'commands')
    action = next(item for item in finding['recovery_actions'] if item['id'] == 'resolve_command')
    assert action['required_options'] == ['--reason'] and action['automatic'] is False
    argv = action['argv'] + ['--reason', 'injected uncertainty inspected; no command was executed']
    for attempt in range(2):
        result = subprocess.run(argv, cwd=action['cwd'], env={**environment, **action['environment']},
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['status'] == 'resolved'
        if attempt == 0:
            resolved_bytes = store.path.read_bytes()
        else:
            assert store.path.read_bytes() == resolved_bytes
        assert not [f for f in doctor()['findings'] if f['component'] == 'commands']
        assert store.path.read_bytes() == resolved_bytes
    assert task_path.read_bytes() == task_before
    state = store.snapshot()
    assert len(state['runs']) == 1 and state['results'] == {}
    assert not (root / 'agent-starts.log').exists()
    assert not (root / 'COMMAND_EXECUTED').exists()
    assert state['runs'][claim.run['id']]['state'] == 'interrupted'
    assert not state['runs'][claim.run['id']].get('retry_authorized')
