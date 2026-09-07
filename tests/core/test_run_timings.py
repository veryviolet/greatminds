import asyncio

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.store import RunStore
from test_supervisor import setup, supervisor


def test_live_acp_timings_have_causal_stage_order(tmp_path):
    store, schema, config, task = setup(tmp_path)
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            run = await service.execute(service.claim(task, config.bindings[0]),
                                        binding=config.bindings[0], prompt='work')
        assert run['state'] == 'completed'
        timings = run['timings']
        order = ['workspace_ready', 'context_ready', 'process_recorded', 'protocol_ready',
                 'session_ready', 'first_prompt_started', 'first_prompt_activity', 'cleanup_complete']
        values = [timings[name] for name in order]
        assert values == sorted(values)
        assert timings['first_protocol_activity'] <= timings['first_prompt_activity']
        assert timings['cleanup_complete'] <= run['outcome']['elapsed_seconds']
        assert RunStore(store.runtime).snapshot()['runs'][run['id']]['timings'] == timings
        events = [event for event in store.snapshot()['events'] if event['kind'] == 'run_stage_observed']
        assert len(events) == len(timings)
    asyncio.run(check())


def test_prelaunch_failure_does_not_fabricate_handshake_or_activity(tmp_path):
    from dataclasses import replace
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_prompt_bytes=1)
    config = replace(config, bindings=(binding,))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            run = await service.execute(service.claim(task, binding), binding=binding, prompt='too large')
        assert run['reason'] == 'input_budget_exceeded'
        assert set(run['timings']) == {'workspace_ready', 'context_ready', 'cleanup_complete'}
    asyncio.run(check())


def test_observations_survive_recovery_without_inventing_completion(tmp_path):
    store, schema, config, task = setup(tmp_path)
    run = store.claim(task=task, binding=config.bindings[0], config=config,
                      schema=schema, project=tmp_path, owner_id='owner').run
    store.transition(run['id'], owner_id='owner', event_id='start', target='starting')
    store.record_timing(run['id'], owner_id='owner', stage='protocol_ready', seconds=2.5)
    store.record_timing(run['id'], owner_id='owner', stage='protocol_ready', seconds=99)
    with pytest.raises(GreatMindsError, match='supervisor'):
        store.record_timing(run['id'], owner_id='stranger', stage='session_ready', seconds=3)
    for value in [True, -1, float('nan'), float('inf')]:
        with pytest.raises(GreatMindsError, match='timing'):
            store.record_timing(run['id'], owner_id='owner', stage='session_ready', seconds=value)
    store.recover_run(run['id'], previous_owner='owner', owner_id='restarted')
    recovered = RunStore(store.runtime).snapshot()['runs'][run['id']]
    assert recovered['timings'] == {'protocol_ready': 2.5}
    assert recovered['state'] == 'interrupted'
    with pytest.raises(GreatMindsError, match='supervisor'):
        store.record_timing(run['id'], owner_id='restarted', stage='cleanup_complete', seconds=10)
