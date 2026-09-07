"""Retention bounds event tails without changing recovery or identity records."""
import json

import pytest
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.diagnostic_bundle import collect_bundle
from greatminds.runtime.store import RunStore
from test_runtime_contract import claim_at, config_document, running, runtime


def flood(store, count):
    with store._transaction() as state:
        for index in range(count):
            store._event(state, 'fixture_observation', None, {'index': index})


def test_pruning_preserves_run_receipts_result_idempotence_and_task_bytes(runtime):
    store, claim, _ = running(runtime)
    store.configure_event_retention(100)
    decision = {'decision': 'no_change', 'payload': {'reason': 'semantic work remains'}}
    receipt = store.submit_decision(decision, run_id=claim.run['id'], token=claim.token)
    before = store.snapshot()
    task = runtime / claim.run['task_path']
    task_bytes = task.read_bytes()
    flood(store, 250)
    after = store.snapshot()
    assert len(after['events']) <= 100
    assert after['runs'] == before['runs'] and after['results'] == before['results']
    assert task.read_bytes() == task_bytes
    assert not any(event['run_id'] == claim.run['id'] for event in after['events'])
    restarted = RunStore(runtime)
    assert restarted.submit_decision(decision, run_id=claim.run['id'], token=claim.token) == receipt
    saved = store.path.read_bytes()
    restarted.configure_event_retention(100)
    assert store.path.read_bytes() == saved
    with pytest.raises(GreatMindsError, match='different contents'):
        restarted.submit_decision({'decision': 'no_change', 'payload': {}},
            run_id=claim.run['id'], token=claim.token)
    assert store.path.read_bytes() == saved


def test_claim_sequence_and_event_deduplication_survive_pruning(runtime):
    store = RunStore(runtime)
    store.configure_event_retention(100)
    first = claim_at(runtime)
    args = dict(owner_id='test-supervisor', event_id='cancel', target='cancelled')
    store.transition(first.run['id'], **args)
    flood(store, 150)
    previous_sequence = store.snapshot()['events'][-1]['sequence']
    before = store.path.read_bytes()
    RunStore(runtime).transition(first.run['id'], **args)
    assert store.path.read_bytes() == before
    second = claim_at(runtime)
    assert second.run['sequence'] == previous_sequence + 1
    assert second.run['sequence'] > first.run['sequence']
    sequence = [event['sequence'] for event in store.snapshot()['events']]
    assert sequence == sorted(set(sequence))


def test_failed_pruning_commit_preserves_original_journal(tmp_path, monkeypatch):
    import greatminds.runtime.store as module
    store = RunStore(tmp_path)
    store.configure_event_retention(100)
    flood(store, 99)
    before = store.path.read_bytes()
    original = module.atomic_json
    def fail(*args):
        raise OSError('injected atomic write failure')
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError, match='injected'):
        store.set_paused(True)
    assert store.path.read_bytes() == before
    monkeypatch.setattr(module, 'atomic_json', original)
    store.set_paused(True)
    state = store.snapshot()
    assert state['paused'] and len(state['events']) <= 100
    assert state['event_retention']['discarded_through'] > 0


def test_expired_cursor_reports_gap_and_keeps_pages_read_only(tmp_path):
    store = RunStore(tmp_path/'.greatminds')
    store.configure_event_retention(100)
    flood(store, 250)
    saved = store.path.read_bytes()
    args = ['run', 'events', '--project-dir', str(tmp_path), '--limit', '2']
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines()]
    assert rows[0]['kind'] == 'events_gap'
    assert rows[0]['data']['first_available_sequence'] == rows[1]['sequence']
    assert len(rows) == 3
    again = CliRunner().invoke(cli, [*args, '--after', str(rows[-1]['sequence'])])
    assert again.exit_code == 0, again.output
    next_rows = [json.loads(line) for line in again.output.splitlines()]
    assert all(row['sequence'] > rows[-1]['sequence'] for row in next_rows)
    assert all(row['kind'] != 'events_gap' for row in next_rows)
    assert store.path.read_bytes() == saved
    bundle = collect_bundle(tmp_path, environment={})
    assert bundle['truncated']['events'] is True
    assert bundle['event_retention']['discarded_through'] == rows[0]['sequence']


@pytest.mark.parametrize('value', [0, 99, 1000001, True, '100'])
def test_invalid_event_limit_rejected(value):
    document = config_document()
    document['max_runtime_events'] = value
    with pytest.raises(GreatMindsError):
        parse_execution_config(document, roles={'DEVELOPER'})


def test_default_and_custom_limits():
    document = config_document()
    assert parse_execution_config(document, roles={'DEVELOPER'}).max_runtime_events == 10000
    document['max_runtime_events'] = 100
    assert parse_execution_config(document, roles={'DEVELOPER'}).max_runtime_events == 100


def test_daemon_applies_retention_without_agent_turns_and_restart_is_idle(tmp_path, monkeypatch):
    import asyncio
    import yaml
    from greatminds.runtime.daemon import serve
    result = CliRunner().invoke(cli, ['setup', '--project-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    path = tmp_path/'coordination/execution.yaml'
    config = yaml.safe_load(path.read_text())
    config['max_runtime_events'] = 100
    path.write_text(yaml.safe_dump(config))
    store = RunStore(tmp_path/'.greatminds')
    flood(store, 250)
    def forbidden(*args, **kwargs):
        raise AssertionError('retention must not launch a harness')
    monkeypatch.setattr('greatminds.runtime.acp_transport.AcpTransport.__aenter__', forbidden)
    first = asyncio.run(serve(tmp_path, once=True, environment={}))
    assert first['runs'] == {} and first['results'] == {}
    assert len(first['events']) <= 100
    assert first['event_retention']['max_events'] == 100
    assert first['event_retention']['discarded_count'] > 0
    assert asyncio.run(serve(tmp_path, once=True, environment={})) == first
