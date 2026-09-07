import json
import stat

import pytest
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.runtime.diagnostic_bundle import collect_bundle, write_bundle
from test_operator_observation import project


def fixture(root):
    store, schema, config, task = project(root)
    claim = store.claim(task=task, binding=config.bindings[0], config=config,
                        schema=schema, project=root, owner_id='test')
    store.transition(claim.run['id'], owner_id='test', target='starting', event_id='start')
    store.transition(claim.run['id'], owner_id='test', target='waiting_auth', event_id='auth',
                     reason='authentication_required', details={'private': 'SECRET_OUTCOME'})
    with store._transaction() as state:
        state['runs'][claim.run['id']]['timings'] = {'context_ready': 1.25, 'SECRET_TIMING': 'SECRET_VALUE'}
        state['events'][-1]['data'] = {'credential': 'SECRET_EVENT'}
    return store, claim


def test_export_is_allowlisted_and_references_stay_linked(tmp_path):
    store, claim = fixture(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    bundle = collect_bundle(tmp_path, environment={'KEY': 'SECRET_ENV'})
    text = json.dumps(bundle)
    for private in ('SECRET_', 'secret-value', 'private-executable', str(tmp_path), claim.token,
                    claim.run['id'], claim.run['task_id']):
        assert private not in text
    run = bundle['runs'][0]
    assert run['timings'] == {'context_ready': 1.25}
    assert run['metrics']['elapsed_seconds'] is None
    assert run['reference'] == bundle['events'][-1]['run_reference']
    auth = next(row for row in bundle['report']['findings'] if row['code'] == 'waiting_auth')
    assert run['reference'] == auth['references']['run_id']
    assert run['binding_reference'] == bundle['configuration']['bindings'][0]['reference']
    assert bundle['versions']['acp_sdk']
    assert bundle['truncated'] == {'runs': False, 'events': False, 'findings': False}
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_recent_event_window_is_explicit_and_reference_salt_changes(tmp_path):
    fixture(tmp_path)
    first = collect_bundle(tmp_path, environment={}, event_limit=1)
    second = collect_bundle(tmp_path, environment={}, event_limit=1)
    assert len(first['events']) == 1
    assert first['truncated']['events'] is True
    assert first['runs'][0]['reference'] != second['runs'][0]['reference']


def test_corrupt_runtime_does_not_hide_configuration_and_error_report(tmp_path):
    store, _ = fixture(tmp_path)
    store.path.write_text('SECRET_INVALID_JSON')
    bundle = collect_bundle(tmp_path, environment={})
    assert bundle['collection']['runtime'] == 'unavailable'
    assert bundle['collection']['configuration'] == 'inspected'
    assert bundle['runs'] == []
    assert bundle['truncated']['runs'] is None
    assert 'SECRET_' not in json.dumps(bundle)


def test_writer_is_private_atomic_and_never_replaces_existing_paths(tmp_path):
    path = tmp_path/'bundle.json'
    write_bundle(path, {'version': 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_bundle(path, {'replace': True})
    link = tmp_path/'link.json'
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        write_bundle(link, {'replace': True})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('.greatminds-diagnostics-*'))


def test_cli_collects_even_when_diagnosis_reports_error(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr('greatminds.cli.daemon.execution_environment', lambda _: {})
    output = tmp_path/'report.json'
    result = CliRunner().invoke(cli, ['run', 'doctor', '--project-dir', str(tmp_path),
                                      '--json', '--bundle', str(output)])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)['summary']['error'] >= 1
    bundle = json.loads(output.read_text())
    assert bundle['kind'] == 'greatminds_local_diagnostics'
    assert 'SECRET_' not in output.read_text()
    original = output.read_bytes()
    result = CliRunner().invoke(cli, ['run', 'doctor', '--project-dir', str(tmp_path),
                                      '--bundle', str(output)])
    assert result.exit_code == 1
    assert 'FileExistsError' in result.output
    assert output.read_bytes() == original


def test_finding_window_and_total_export_size_are_bounded(tmp_path):
    fixture(tmp_path)
    bundle = collect_bundle(tmp_path, environment={}, finding_limit=1)
    assert len(bundle['report']['findings']) == 1
    assert bundle['truncated']['findings'] is True
    assert bundle['report']['findings'][0]['severity'] == 'error'
    assert sum(bundle['report']['summary'].values()) > 1
    with pytest.raises(ValueError, match='5 MiB'):
        write_bundle(tmp_path/'too-large.json', {'text': 'x' * (5 * 1024 * 1024)})
    assert not (tmp_path/'too-large.json').exists()
    assert not list(tmp_path.glob('.greatminds-diagnostics-*'))
