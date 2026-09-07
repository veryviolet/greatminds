import asyncio
from dataclasses import replace

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.input_budget import InputBudgetExceeded
from greatminds.runtime.store import RunStore
from test_supervisor import setup, supervisor
from test_runtime_contract import config_document


@pytest.mark.parametrize('field', ['max_prompt_bytes', 'max_session_input_bytes'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '12'])
def test_input_limits_are_positive_integers(field, value):
    document = config_document()
    document['bindings']['developer'][field] = value
    with pytest.raises(GreatMindsError, match=field):
        parse_execution_config(document, roles={'DEVELOPER'})


def test_oversized_utf8_prompt_never_launches_agent(tmp_path):
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_prompt_bytes=3)
    config = replace(config, bindings=(binding,))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            result = await service.execute(service.claim(task, binding), binding=binding, prompt='яя')
        assert result['reason'] == 'input_budget_exceeded'
        assert result['outcome']['input_budget'] == {
            'budget': 'prompt', 'limit_bytes': 3, 'used_bytes': 0, 'requested_bytes': 4}
        assert result['outcome']['prompt_started'] is False
        assert 'process' not in result
    asyncio.run(check())
    assert not (tmp_path/'agent-starts.log').exists()


def test_session_reservation_survives_recovery_and_reopen(tmp_path):
    store, schema, config, task = setup(tmp_path)
    run = store.claim(task=task, binding=config.bindings[0], config=config, schema=schema,
                      project=tmp_path, owner_id='one').run
    store.transition(run['id'], owner_id='one', event_id='start', target='starting')
    store.transition(run['id'], owner_id='one', event_id='ready', target='running', session_id='session')
    kwargs = dict(owner_id='one', request_id='message', size=4, limit=6)
    first = store.reserve_prompt_input(run['id'], **kwargs)
    assert store.reserve_prompt_input(run['id'], **kwargs) == first
    with pytest.raises(GreatMindsError, match='identity reused'):
        store.reserve_prompt_input(run['id'], **{**kwargs, 'size': 5})
    store.recover_run(run['id'], previous_owner='one', owner_id='two')
    store = RunStore(store.runtime)
    other = store.claim(task=task, binding=config.bindings[0], config=config, schema=schema,
                       project=tmp_path, owner_id='two').run
    store.transition(other['id'], owner_id='two', event_id='start', target='starting')
    store.transition(other['id'], owner_id='two', event_id='ready', target='running', session_id='session')
    with pytest.raises(InputBudgetExceeded) as caught:
        store.reserve_prompt_input(other['id'], owner_id='two', request_id='next', size=3, limit=6)
    assert caught.value.details['used_bytes'] == 4
    assert 'input_reservations' not in store.snapshot()['runs'][other['id']]
    accepted = store.reserve_prompt_input(other['id'], owner_id='two', request_id='next', size=2, limit=6)
    assert accepted['used_bytes'] == 6


def test_exact_prompt_and_session_boundary_sends_real_acp_turn(tmp_path):
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_prompt_bytes=4, max_session_input_bytes=4)
    config = replace(config, bindings=(binding,))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            result = await service.execute(service.claim(task, binding), binding=binding, prompt='яя')
        assert result['state'] == 'completed', result
        assert result['outcome']['input_bytes_reserved'] == 4
        assert result['outcome']['session_input_bytes_reserved'] == 4
        assert sum(item['requested_bytes'] for item in result['input_reservations'].values()) == 4
    asyncio.run(check())


@pytest.mark.parametrize('restart', [False, True])
def test_interactive_budget_counts_user_text_and_loaded_session(tmp_path, restart):
    from test_acp_conversations import setup as conversation_setup
    from greatminds.runtime.daemon import serve
    conversation = conversation_setup(tmp_path, scenario='resume',
        max_prompt_bytes=20000, max_session_input_bytes=15000)
    conversation.enqueue('a' * 10000, request_id='first')
    if restart:
        first = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
        assert next(iter(first['runs'].values()))['state'] == 'completed'
    conversation.enqueue('b' * 10000, request_id='second')
    state = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    turns = conversation.snapshot()['turns']
    assert turns['first']['status'] == 'completed'
    assert turns['second']['status'] == 'failed'
    run = max(state['runs'].values(), key=lambda r: r['sequence'])
    assert run['reason'] == 'input_budget_exceeded'
    assert run['outcome']['input_budget']['budget'] == 'session'
    assert run['outcome']['input_budget']['used_bytes'] > 10000
    assert sum(len(r.get('input_reservations', {})) for r in state['runs'].values()) == 1
    if restart:
        assert run['outcome']['prompt_started'] is False
        assert (tmp_path/'loads.log').read_text().splitlines() == ['test-session']


def test_oversized_interactive_message_is_not_sent(tmp_path):
    from test_acp_conversations import setup as conversation_setup
    from greatminds.runtime.daemon import serve
    conversation = conversation_setup(tmp_path, max_prompt_bytes=4096)
    conversation.enqueue('я' * 3000, request_id='large')
    state = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    run = next(iter(state['runs'].values()))
    assert run['reason'] == 'input_budget_exceeded'
    assert run['outcome']['input_budget']['requested_bytes'] > 6000
    assert run['outcome']['prompt_started'] is False
    assert conversation.snapshot()['turns']['large']['status'] == 'failed'
    assert not (tmp_path/'agent-starts.log').exists()


def test_session_with_unknown_prior_input_is_not_treated_as_empty(tmp_path):
    store, schema, config, task = setup(tmp_path)
    def running(owner, session):
        run = store.claim(task=task, binding=config.bindings[0], config=config,
                          schema=schema, project=tmp_path, owner_id=owner).run
        store.transition(run['id'], owner_id=owner, event_id='start', target='starting')
        store.transition(run['id'], owner_id=owner, event_id='ready', target='running', session_id=session)
        return run['id']
    old = running('old', 'saved')
    store.transition(old, owner_id='old', event_id='done', target='completed',
                     details={'prompt_started': True})
    current = running('current', 'saved')
    with pytest.raises(InputBudgetExceeded) as caught:
        store.reserve_prompt_input(current, owner_id='current', request_id='first', size=1, limit=10)
    assert caught.value.details == {'budget': 'session_history_unknown', 'limit_bytes': 10,
                                   'used_bytes': None, 'requested_bytes': 1}
    store.transition(current, owner_id='current', event_id='done', target='failed')
    fresh = running('fresh', 'new-session')
    assert store.reserve_prompt_input(fresh, owner_id='fresh', request_id='first', size=1, limit=10)['used_bytes'] == 1
