import json

from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.runtime.diagnostics import diagnose
from greatminds.runtime.store import RunStore


def project(root):
    result = CliRunner().invoke(cli, ['setup', '--project-dir', str(root)])
    assert result.exit_code == 0, result.output
    return RunStore(root/'.greatminds')


def files(root):
    return {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_healthy_empty_project_is_read_only_and_static(tmp_path, monkeypatch):
    project(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('diagnostics launched a process')
    monkeypatch.setattr('subprocess.run', forbidden)
    before = files(tmp_path)
    result = diagnose(tmp_path, environment={})
    assert result['status'] == 'no_findings', result
    assert result['summary'] == {'error': 0, 'warning': 0, 'info': 0}
    assert result['verification'] == 'local_inspection'
    assert result['checks']['deployments'] == 'inspected'
    assert files(tmp_path) == before


def test_recovery_findings_are_stable_without_raw_operation_content(tmp_path):
    store = project(tmp_path)
    store.set_paused(True)
    with store._transaction() as state:
        state['commands'] = {'command-one': {'status': 'needs_recovery', 'argv': ['DO_NOT_LEAK_ARGUMENT'],
                                            'stderr': 'DO_NOT_LEAK_OUTPUT'}}
        state['results']['result-one'] = {'status': 'needs_recovery', 'envelope': {'secret': 'DO_NOT_LEAK_RESULT'}}
    before = files(tmp_path)
    first = diagnose(tmp_path, environment={'SECRET_VALUE': 'DO_NOT_LEAK_ENV'})
    assert first == diagnose(tmp_path, environment={})
    assert first['summary']['error'] == 2
    assert first['summary']['info'] == 1
    codes = {(f['component'], f['code']) for f in first['findings']}
    assert ('commands', 'operation_needs_recovery') in codes
    command_finding = next(row for row in first['findings'] if row['component'] == 'commands')
    resolve = next(action for action in command_finding['recovery_actions'] if action['id'] == 'resolve_command')
    assert resolve['argv'] == ['greatminds', 'run', 'command-resolve', 'command-one']
    assert resolve['environment'] == {'GREATMINDS_PROJECT_DIR': str(tmp_path)}
    assert resolve['required_options'] == ['--reason']
    assert ('results', 'operation_needs_recovery') in codes
    assert 'DO_NOT_LEAK' not in json.dumps(first)
    assert files(tmp_path) == before


def test_broken_configuration_does_not_hide_broken_deployments(tmp_path):
    project(tmp_path)
    (tmp_path/'coordination/execution.yaml').write_text('password: DO_NOT_LEAK_CONFIG\n')
    stand = tmp_path/'.greatminds/.stand'
    stand.mkdir()
    (stand/'deployments.json').write_text('DO_NOT_LEAK_LEDGER')
    before = files(tmp_path)
    result = diagnose(tmp_path, environment={})
    assert result['checks']['configuration'] == 'failed'
    assert result['checks']['runtime'] == 'inspected'
    assert result['checks']['deployments'] == 'failed'
    assert result['checks']['agent_prerequisites'] == 'unavailable'
    assert result['summary']['error'] == 2
    assert 'DO_NOT_LEAK' not in json.dumps(result)
    assert files(tmp_path) == before


def test_malformed_operation_collection_is_a_finding_not_a_crash(tmp_path):
    store = project(tmp_path)
    with store._transaction() as state:
        state['commands'] = ['DO_NOT_LEAK_MALFORMED']
    result = diagnose(tmp_path, environment={})
    assert result['checks']['commands'] == 'failed'
    assert result['checks']['deployments'] == 'inspected'
    assert 'DO_NOT_LEAK' not in json.dumps(result)


def test_cli_json_keeps_inspection_failures_machine_readable(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.setattr('greatminds.cli.daemon.execution_environment', lambda _: {})
    (tmp_path/'coordination/execution.yaml').write_text('unreadable: [')
    result = CliRunner().invoke(cli, ['run', 'doctor', '--project-dir', str(tmp_path), '--json'])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report['version'] == 1
    assert report['checks']['configuration'] == 'failed'
    assert report['checks']['deployments'] == 'inspected'


def test_authentication_and_dependency_holds_remain_distinct(tmp_path):
    from test_operator_observation import project as configured_project
    from test_maintenance import write
    store, schema, config, task = configured_project(tmp_path)
    claim = store.claim(task=task, binding=config.bindings[0], config=config,
                        schema=schema, project=tmp_path, owner_id='test')
    store.transition(claim.run['id'], owner_id='test', event_id='start', target='starting')
    store.transition(claim.run['id'], owner_id='test', event_id='auth', target='waiting_auth',
                     reason='authentication_required', details={'private': 'DO_NOT_LEAK_AUTH'})
    write(store.runtime, 'feature_blocked', '0002-blocked', dependencies=['verified/0003-missing.yaml'])
    before = files(tmp_path)
    report = diagnose(tmp_path, environment={})
    auth = [row for row in report['findings'] if row['code'] == 'waiting_auth']
    dependency = [row for row in report['findings'] if row['code'] == 'dependency_hold']
    assert auth[0]['evidence']['run_id'] == claim.run['id']
    assert dependency[0]['evidence']['task_id'] == '0002-blocked'
    assert dependency[0]['evidence']['reason_codes']
    assert auth[0]['action'] != dependency[0]['action']
    assert 'DO_NOT_LEAK' not in json.dumps(report)
    assert files(tmp_path) == before


def test_filesystem_findings_are_shared_with_watchdog_and_bundle(tmp_path, monkeypatch):
    import os
    from greatminds.runtime.diagnostic_bundle import collect_bundle
    store = project(tmp_path)
    intent = store.runtime/'intent/SECRET_INTENT.json'
    intent.parent.mkdir(exist_ok=True)
    intent.write_text('SECRET_BODY')
    os.utime(intent, (0, 0))
    tree = tmp_path/'.worktrees/SECRET_ORPHAN'
    tree.mkdir(parents=True)
    task = store.runtime/'feature_dev/0001-old.yaml'
    task.write_text('id: 0001-old\n')
    os.utime(task, (0, 0))
    report = diagnose(tmp_path, environment={})
    codes = {row['code'] for row in report['findings']}
    assert {'orphan_intent', 'orphan_worktree', 'stale_task'} <= codes
    monkeypatch.setattr('greatminds.cli.daemon.execution_environment', lambda _: {})
    result = CliRunner().invoke(cli, ['watchdog', '--project-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert 'ORPHANED INTENTS (1,' in result.output
    assert 'ORPHAN WORKTREES (1)' in result.output
    assert 'STALE TASKS (1)' in result.output
    bundle = collect_bundle(tmp_path, report=report)
    assert 'SECRET_' not in json.dumps(bundle)
    assert {'orphan_intent', 'orphan_worktree', 'stale_task'} <= {r['code'] for r in bundle['report']['findings']}


def test_unreadable_task_directory_prevents_all_clear(tmp_path, monkeypatch):
    from pathlib import Path
    store = project(tmp_path)
    blocked = store.runtime/'feature_dev'
    original = Path.iterdir
    def iterdir(path):
        if path == blocked:
            raise PermissionError('SECRET_PATH_ERROR')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', iterdir)
    report = diagnose(tmp_path, environment={})
    assert report['checks']['stale_tasks'] == 'failed'
    assert 'SECRET_PATH_ERROR' not in json.dumps(report)
    result = CliRunner().invoke(cli, ['watchdog', '--project-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert 'Task observation unavailable' in result.output
    assert 'All clear' not in result.output
    assert 'active queues: 0 stale tasks' not in result.output
