import asyncio
import json

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.usage_observations import normalize
from greatminds.runtime.store import RunStore
from test_supervisor import setup, supervisor


@pytest.mark.parametrize('value', [-1, True, '12', 1.5, 2**64])
def test_invalid_counts_never_become_zero(value):
    observed = normalize('tokens', {'totalTokens': value, 'inputTokens': 0, 'outputTokens': 0})
    assert observed == {'status': 'invalid', 'scope': 'unknown'}


@pytest.mark.parametrize('cost', [False, {'amount': float('nan'), 'currency': 'USD'},
    {'amount': float('inf'), 'currency': 'USD'}, {'amount': True, 'currency': 'USD'},
    {'amount': -1, 'currency': 'USD'}, {'amount': 1, 'currency': 'SECRET_CURRENCY'}])
def test_invalid_cost_is_allowlisted(cost):
    observed = normalize('session', {'used': 0, 'size': 100, 'cost': cost})
    assert observed['cost'] == {'status': 'invalid', 'scope': 'session'}


def test_missing_and_reported_zero_are_distinct():
    assert normalize('tokens', None)['status'] == 'missing'
    assert normalize('tokens', {'totalTokens': 0, 'inputTokens': 0, 'outputTokens': 0})['status'] == 'reported'
    assert normalize('session', {'used': 0, 'size': 0})['cost']['status'] == 'missing'


def test_real_acp_observations_keep_context_separate_and_never_sum_tokens(tmp_path):
    store, schema, config, task = setup(tmp_path, 'usage-observations')
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            run = await service.execute(claim, binding=config.bindings[0], prompt='hello')
        assert run['state'] == 'completed', run
        usage = run['usage']
        assert usage['session']['data']['context']['values'] == {'used': 20, 'size': 100}
        assert usage['session']['data']['cost']['amount'] == .3
        assert usage['tokens']['data']['scope'] == 'unknown'
        assert usage['tokens']['data']['values']['totalTokens'] == 150
        assert usage['tokens']['data']['values']['cachedReadTokens'] == 70
        assert 'SECRET_' not in json.dumps(store.snapshot())
        assert RunStore(store.runtime).snapshot()['runs'][run['id']]['usage'] == usage
    asyncio.run(check())


def test_usage_owner_missing_sample_and_atomic_failure(tmp_path, monkeypatch):
    import greatminds.runtime.store as module
    store, schema, config, task = setup(tmp_path)
    run = store.claim(task=task, binding=config.bindings[0], config=config, schema=schema,
                      project=tmp_path, owner_id='owner').run
    args = dict(kind='tokens', data={'totalTokens': 5, 'inputTokens': 4, 'outputTokens': 1})
    with pytest.raises(GreatMindsError, match='supervisor'):
        store.record_usage(run['id'], owner_id='stranger', **args)
    store.record_usage(run['id'], owner_id='owner', **args)
    store.record_usage(run['id'], owner_id='owner', kind='tokens', data=None)
    assert store.snapshot()['runs'][run['id']]['usage']['tokens']['data']['status'] == 'missing'
    before = store.path.read_bytes()
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError, match='write failed'):
        store.record_usage(run['id'], owner_id='owner', **args)
    assert store.path.read_bytes() == before
