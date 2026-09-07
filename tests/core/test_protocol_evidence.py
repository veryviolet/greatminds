import asyncio
from dataclasses import replace
import json

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.diagnostic_bundle import collect_bundle
from greatminds.runtime.protocol_evidence import TOOL_EVENT_LIMIT
from greatminds.runtime.store import RunStore
from test_supervisor import setup, supervisor


@pytest.mark.parametrize('scenario', ['protocol-evidence', 'protocol-evidence-error'])
def test_real_supervisor_keeps_bounded_protocol_facts_without_payloads(tmp_path, scenario):
    store, schema, config, task = setup(tmp_path, scenario)
    config = replace(config, bindings=(replace(config.bindings[0], timeout_seconds=10),))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            run = await service.execute(claim, binding=config.bindings[0], prompt='SECRET_PROMPT')
        protocol = run['protocol']
        assert run['outcome']['updates'] == 81
        assert protocol['negotiated']['data']['protocol_version'] == 1
        assert protocol['negotiated']['data']['capabilities']['promptCapabilities.image'] is True
        assert len(protocol['tool_events']) == TOOL_EVENT_LIMIT
        assert protocol['tool_events_truncated'] is True
        first, second = [entry['data'] for entry in protocol['tool_events'][:2]]
        assert first['phase'] == 'prompt'
        assert first['tool_reference'] == second['tool_reference']
        assert first['status'] == 'in_progress' and second['status'] == 'completed'
        assert first['kind'] == 'edit' and first['content_count'] == first['location_count'] == 1
        assert protocol['tool_events'][2]['data']['tool_reference'] != first['tool_reference']
        if scenario.endswith('-error'):
            assert run['reason'] == 'protocol_error'
            assert protocol['error']['data'] == {'phase': 'prompt', 'rpc_code': -32603, 'exception': 'RequestError'}
        else:
            assert run['state'] == 'completed'
            assert protocol['stop']['data']['reason'] == 'end_turn'
        state = store.snapshot()
        assert state['results'] == {}
        assert sum(e['kind'] == 'protocol_trace_truncated' for e in state['events']) == 1
        assert len(json.dumps(protocol)) < 32768
        assert 'SECRET_' not in json.dumps(state)
        assert claim.token not in json.dumps(state)
        exported = collect_bundle(tmp_path, environment={})
        assert 'SECRET_' not in json.dumps(exported)
        assert exported['runs'][0]['protocol']['tool_events_retained'] == TOOL_EVENT_LIMIT
        assert exported['runs'][0]['protocol']['tool_events_truncated'] is True
    asyncio.run(check())


def test_evidence_requires_active_owner_and_survives_restart_and_event_pruning(tmp_path):
    store, schema, config, task = setup(tmp_path)
    claim = store.claim(task=task, binding=config.bindings[0], config=config,
                        schema=schema, project=tmp_path, owner_id='owner')
    args = {'kind': 'error', 'data': {'phase': 'session_new', 'rpc_code': -32000,
                                     'exception': 'RequestError', 'message': 'SECRET_ERROR'}}
    with pytest.raises(GreatMindsError, match='supervisor'):
        store.record_protocol(claim.run['id'], owner_id='stranger', **args)
    store.record_protocol(claim.run['id'], owner_id='owner', **args)
    before = store.path.read_bytes()
    store.record_protocol(claim.run['id'], owner_id='owner', **args)
    assert store.path.read_bytes() == before
    evidence = store.snapshot()['runs'][claim.run['id']]['protocol']
    store.configure_event_retention(100)
    with store._transaction() as state:
        for _ in range(200):
            store._event(state, 'fixture', None, {})
    store.recover_run(claim.run['id'], previous_owner='owner', owner_id='restarted')
    assert RunStore(store.runtime).snapshot()['runs'][claim.run['id']]['protocol'] == evidence
    with pytest.raises(GreatMindsError, match='supervisor'):
        store.record_protocol(claim.run['id'], owner_id='restarted', **args)


def test_failed_evidence_publication_does_not_publish_partial_metadata(tmp_path, monkeypatch):
    import greatminds.runtime.store as module
    store, schema, config, task = setup(tmp_path)
    claim = store.claim(task=task, binding=config.bindings[0], config=config,
                        schema=schema, project=tmp_path, owner_id='owner')
    before = store.path.read_bytes()
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError, match='write failed'):
        store.record_protocol(claim.run['id'], owner_id='owner', kind='error',
                              data={'phase': 'initialize', 'rpc_code': -32603})
    assert store.path.read_bytes() == before
