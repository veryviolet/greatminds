import asyncio
from dataclasses import replace

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.store import RunStore
from test_runtime_contract import config_document
from test_supervisor import setup, supervisor


@pytest.mark.parametrize('limit,currency', [(0, 'USD'), (-1, 'USD'), (True, 'USD'),
    ('1', 'USD'), (float('inf'), 'USD'), (float('nan'), 'USD'), (1, None),
    (None, 'USD'), (1, 'usd'), (1, 'SECRET_CURRENCY')])
def test_cost_budget_configuration_is_explicit_and_finite(limit, currency):
    document = config_document()
    document['bindings']['developer'].update(max_reported_session_cost=limit, reported_cost_currency=currency)
    with pytest.raises(GreatMindsError, match='cost'):
        parse_execution_config(document, roles={'DEVELOPER'})


@pytest.mark.parametrize('scenario,limit,reason', [
    ('usage-observations', 1, None), ('usage-observations', .3, 'cost_limit_reached'),
    ('usage-budget-hang', .25, 'cost_limit_reached'), ('normal', 1, 'cost_unavailable'),
    ('usage-regress', 1, 'cost_regressed'), ('usage-currency', 1, 'currency_changed'),
    ('usage-invalid', 1, 'invalid_cost')])
def test_real_prompt_cost_guard_and_cancellation(tmp_path, scenario, limit, reason):
    store, schema, config, task = setup(tmp_path, scenario)
    binding = replace(config.bindings[0], max_reported_session_cost=limit,
                      reported_cost_currency='USD', timeout_seconds=10)
    config = replace(config, bindings=(binding,))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            run = await service.execute(service.claim(task, binding), binding=binding, prompt='hello')
        if reason:
            assert run['reason'] == 'reported_usage_budget', run
            assert run['outcome']['usage_budget']['reason'] == reason
        else:
            assert run['state'] == 'completed', run
        if scenario == 'usage-budget-hang':
            assert (tmp_path / 'usage-cancelled').read_text() == 'cancelled'
        assert not store.snapshot()['results']
    asyncio.run(check())


def active(store, schema, config, task, project, owner):
    binding = config.bindings[0]
    run = store.claim(task=task, binding=binding, config=config, schema=schema,
                      project=project, owner_id=owner).run
    store.transition(run['id'], owner_id=owner, event_id='start', target='starting')
    store.transition(run['id'], owner_id=owner, event_id='ready', target='running', session_id='session')
    return run['id']


@pytest.mark.parametrize('change,reason', [({'amount': .1, 'currency': 'USD'}, 'cost_regressed'),
    ({'amount': .3, 'currency': 'EUR'}, 'currency_changed'),
    ({'amount': -1, 'currency': 'USD'}, 'invalid_cost')])
def test_cost_discontinuity_is_sticky_across_recovery(tmp_path, change, reason):
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_reported_session_cost=1, reported_cost_currency='USD')
    config = replace(config, bindings=(binding,))
    run_id = active(store, schema, config, task, tmp_path, 'one')
    store.prepare_usage_session(run_id, owner_id='one', loaded=False)
    assert store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='begin') is None
    for cost in [{'amount': .2, 'currency': 'USD'}, change, {'amount': .4, 'currency': 'USD'}]:
        store.record_usage(run_id, owner_id='one', kind='session', data={'used': 10, 'size': 100, 'cost': cost})
    assert store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='finish')['reason'] == reason
    store.recover_run(run_id, previous_owner='one', owner_id='two')
    store = RunStore(store.runtime)
    resumed = active(store, schema, config, task, tmp_path, 'two')
    store.prepare_usage_session(resumed, owner_id='two', loaded=True)
    assert store.usage_prompt_boundary(resumed, owner_id='two', binding=binding, phase='begin')['reason'] == reason


@pytest.mark.parametrize('finish', [True, False])
def test_resumed_session_never_treats_uncertain_or_missing_cost_as_zero(tmp_path, finish):
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_reported_session_cost=1, reported_cost_currency='USD')
    config = replace(config, bindings=(binding,))
    run_id = active(store, schema, config, task, tmp_path, 'one')
    store.prepare_usage_session(run_id, owner_id='one', loaded=False)
    store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='begin')
    if finish:
        store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='finish')
    store.recover_run(run_id, previous_owner='one', owner_id='two')
    resumed = active(store, schema, config, task, tmp_path, 'two')
    store.prepare_usage_session(resumed, owner_id='two', loaded=True)
    verdict = store.usage_prompt_boundary(resumed, owner_id='two', binding=binding, phase='begin')
    assert verdict['amount'] is None
    assert verdict['reason'] == ('cost_unavailable' if finish else 'session_history_unknown')


def test_real_conversation_load_preserves_cumulative_cost_and_stops_second_turn(tmp_path):
    from test_acp_conversations import setup as chat_setup
    from greatminds.runtime.daemon import serve
    conversation = chat_setup(tmp_path, scenario='usage-resume',
                              max_reported_session_cost=.3, reported_cost_currency='USD')
    conversation.enqueue('first', request_id='first')
    first = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    assert next(iter(first['runs'].values()))['state'] == 'completed'
    conversation.enqueue('second', request_id='second')
    second = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    runs = sorted(second['runs'].values(), key=lambda run: run['sequence'])
    assert len(runs) == 2
    assert runs[-1]['outcome']['session_strategy'] == 'loaded'
    assert runs[-1]['outcome']['usage_budget']['amount'] == .4
    assert runs[-1]['outcome']['usage_budget']['reason'] == 'cost_limit_reached'
    assert (tmp_path / 'loads.log').read_text().splitlines() == ['test-session']
    again = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    assert len(again['runs']) == 2


def test_unbudgeted_recovery_keeps_uncertainty_without_adding_a_gate(tmp_path):
    store, schema, config, task = setup(tmp_path)
    binding = config.bindings[0]
    run_id = active(store, schema, config, task, tmp_path, 'one')
    store.prepare_usage_session(run_id, owner_id='one', loaded=False)
    store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='begin')
    store.recover_run(run_id, previous_owner='one', owner_id='two')
    resumed = active(store, schema, config, task, tmp_path, 'two')
    store.prepare_usage_session(resumed, owner_id='two', loaded=True)
    assert store.usage_prompt_boundary(resumed, owner_id='two', binding=binding, phase='begin') is None
    ledger = store.snapshot()['runs'][resumed]['usage']['cost_accounting']
    assert ledger['pending'] is True and ledger['error'] == 'session_history_unknown'


def test_ledger_rejects_changed_binding_foreign_owner_and_atomic_partial_prompt(tmp_path, monkeypatch):
    import greatminds.runtime.store as module
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_reported_session_cost=1, reported_cost_currency='USD')
    config = replace(config, bindings=(binding,))
    run_id = active(store, schema, config, task, tmp_path, 'one')
    store.prepare_usage_session(run_id, owner_id='one', loaded=False)
    with pytest.raises(GreatMindsError, match='supervisor'):
        store.usage_prompt_boundary(run_id, owner_id='other', binding=binding, phase='begin')
    with pytest.raises(GreatMindsError, match='binding'):
        store.usage_prompt_boundary(run_id, owner_id='one', binding=replace(binding, max_reported_session_cost=2), phase='begin')
    before = store.path.read_bytes()
    assert store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='admit') is None
    assert store.path.read_bytes() == before
    def fail(*args):
        raise OSError('write failed')
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError, match='write failed'):
        store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='begin')
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('loaded', [False, True])
def test_new_session_bootstrap_and_unknown_loaded_session_are_distinct(tmp_path, loaded):
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_reported_session_cost=1, reported_cost_currency='USD')
    config = replace(config, bindings=(binding,))
    run_id = active(store, schema, config, task, tmp_path, 'one')
    store.prepare_usage_session(run_id, owner_id='one', loaded=loaded)
    verdict = store.usage_prompt_boundary(run_id, owner_id='one', binding=binding, phase='admit')
    if loaded:
        assert verdict['reason'] == 'session_history_unknown'
    else:
        assert verdict is None
