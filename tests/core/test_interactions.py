from concurrent.futures import ThreadPoolExecutor

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.config import RoleBinding
from greatminds.runtime.interactions import ConversationStore


@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore.create(tmp_path, binding=RoleBinding('planner', 'PLANNER', 'fixture'),
                                     config_sha256='config', schema_sha256='schema', workspace=tmp_path)
    store.acquire('daemon1', config_sha256='config', schema_sha256='schema')
    return store


def test_fifo_survives_reopen_and_serializes_simultaneous_claims(conversation):
    conversation.enqueue('first', request_id='z')
    conversation.enqueue('second', request_id='a')
    reopened = ConversationStore(conversation.directory.parents[2], conversation.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: reopened.claim_next('daemon1'), range(2)))
    assert sum(c is not None for c in claims) == 1
    assert next(c for c in claims if c)['id'] == 'z'
    conversation.finish('daemon1', 'z', status='completed', reason='end_turn')
    assert reopened.claim_next('daemon1')['id'] == 'a'


def test_lost_enqueue_ack_can_be_retried_without_duplicate_or_replacement(conversation):
    first = conversation.enqueue('original', request_id='request')
    assert conversation.enqueue('original', request_id='request') == first
    with pytest.raises(GreatMindsError, match='different prompt'):
        conversation.enqueue('replacement', request_id='request')
    assert len(conversation.snapshot()['turns']) == 1


def test_recovery_interrupts_started_turn_and_preserves_queued_input(conversation):
    conversation.enqueue('possibly executed', request_id='first')
    conversation.enqueue('not yet sent', request_id='second')
    conversation.claim_next('daemon1')
    conversation.set_session('daemon1', 'provider-session')
    conversation.acquire('daemon2', config_sha256='config', schema_sha256='schema')
    state = conversation.snapshot()
    assert state['turns']['first']['status'] == 'interrupted'
    assert state['session_id'] == 'provider-session'
    with pytest.raises(GreatMindsError, match='not owned'):
        conversation.append_text('daemon1', 'first', 'late old owner output')
    assert conversation.claim_next('daemon2')['id'] == 'second'
    assert conversation.enqueue('possibly executed', request_id='first')['status'] == 'interrupted'


def test_reconnect_cursor_reads_each_text_event_once_without_mutating_state(conversation):
    conversation.enqueue('hello', request_id='first')
    conversation.claim_next('daemon1')
    conversation.append_text('daemon1', 'first', 'Привет')
    conversation.append_text('daemon1', 'first', '!')
    conversation.finish('daemon1', 'first', status='completed', reason='end_turn')
    before = conversation.path.read_bytes()
    cursor, events = 0, []
    while True:
        page = conversation.events(after=cursor, limit=2)
        events += page['events']; cursor = page['cursor']
        if not page['has_more']:
            break
    assert ''.join(e['text'] for e in events if e['kind'] == 'text') == 'Привет!'
    assert conversation.events(after=cursor)['events'] == []
    assert conversation.path.read_bytes() == before
    assert conversation.path.stat().st_mode & 0o777 == 0o600


def test_cancel_queued_never_sends_and_running_cancel_requires_daemon_completion(conversation):
    conversation.enqueue('active', request_id='active')
    conversation.enqueue('queued', request_id='queued')
    conversation.claim_next('daemon1')
    conversation.cancel('queued')
    conversation.cancel('active')
    conversation.cancel('active')
    state = conversation.snapshot()
    assert state['turns']['queued']['status'] == 'cancelled'
    assert state['turns']['active']['status'] == 'running'
    assert state['turns']['active']['cancel_requested']
    assert conversation.claim_next('daemon1') is None
    conversation.finish('daemon1', 'active', status='cancelled', reason='operator_cancelled')
    assert conversation.claim_next('daemon1') is None


@pytest.mark.parametrize('mode',['bytes','events'])
def test_stream_capture_is_bounded_and_truncation_explicit(conversation,monkeypatch,mode):
    monkeypatch.setattr('greatminds.runtime.interactions.MAX_OUTPUT_BYTES', 5 if mode == 'bytes' else 100)
    monkeypatch.setattr('greatminds.runtime.interactions.MAX_OUTPUT_EVENTS', 1)
    conversation.enqueue('hello', request_id='first'); conversation.claim_next('daemon1')
    conversation.append_text('daemon1', 'first', 'жжж' if mode == 'bytes' else 'a')
    for _ in range(20):
        conversation.append_text('daemon1', 'first', 'late')
    state = conversation.snapshot()
    assert state['turns']['first']['output_truncated']
    text = ''.join(e['text'] for e in state['events'] if e['kind'] == 'text')
    assert text == ('жж' if mode == 'bytes' else 'a')
    assert sum(e['kind'] == 'output_truncated' for e in state['events']) == 1


def test_contract_change_cannot_rebind_pending_prompt(conversation):
    conversation.enqueue('keep', request_id='first')
    before = conversation.path.read_bytes()
    with pytest.raises(GreatMindsError, match='contract changed'):
        conversation.acquire('daemon2', config_sha256='new', schema_sha256='schema')
    assert conversation.path.read_bytes() == before


def test_invalid_inputs_and_full_queue_leave_prior_messages_intact(conversation,monkeypatch):
    monkeypatch.setattr('greatminds.runtime.interactions.MAX_PENDING', 1)
    for text in ['', ' ', 'a' * 65537]:
        with pytest.raises(GreatMindsError):
            conversation.enqueue(text, request_id='bad')
    conversation.enqueue('keep', request_id='one')
    with pytest.raises(GreatMindsError, match='pending turn limit'):
        conversation.enqueue('later', request_id='two')
    assert list(conversation.snapshot()['turns']) == ['one']
