import pytest

from greatminds.domain.results import ResultService
from greatminds.runtime.store import RunStore, TaskRevision
from greatminds.runtime.task_timings import interval
from test_domain_results import setup as result_setup
from test_supervisor import setup


@pytest.mark.parametrize('start,end,expected', [(None, 1, None), (2, 1, None),
    (True, 3, None), (0, float('nan'), None), (0, 10**400, None),
    (-1e308, 1e308, None), (1, 1, 0), (1, 4, 3)])
def test_unknown_or_reversed_intervals_are_not_zero(start, end, expected):
    assert interval(start, end) == expected


def test_queue_observation_is_revision_specific_idempotent_and_copied_into_claim(tmp_path):
    store, schema, config, task = setup(tmp_path)
    store.clock = lambda: 10
    store.observe_assignments([task])
    before = store.path.read_bytes()
    store.clock = lambda: 12
    store.observe_assignments([task])
    assert store.path.read_bytes() == before
    run = store.claim(task=task, binding=config.bindings[0], config=config,
                      schema=schema, project=tmp_path, owner_id='owner').run
    assert run['queue_observation']['wait_seconds'] == 2
    store.transition(run['id'], owner_id='owner', event_id='cancel', target='cancelled')
    store.observe_assignments([])
    assert store.snapshot()['queue_observations'] == {}
    assert RunStore(store.runtime).snapshot()['runs'][run['id']]['queue_observation']['wait_seconds'] == 2
    (store.runtime / task.path).write_text('title: Changed\n')
    changed = TaskRevision.capture(store.runtime, store.runtime / task.path)
    store.clock = lambda: 15
    store.observe_assignments([changed])
    observed = store.snapshot()['queue_observations'][task.path]
    assert observed['task_revision'] == changed.sha256 and observed['first_observed_at'] == 15


def test_manual_claim_without_observation_does_not_invent_queue_arrival(tmp_path):
    store, schema, config, task = setup(tmp_path)
    run = store.claim(task=task, binding=config.bindings[0], config=config,
                      schema=schema, project=tmp_path, owner_id='owner').run
    assert 'queue_observation' not in run


def test_applied_transition_has_validation_and_resolution_but_duplicate_is_unchanged(tmp_path):
    store, source, claim, envelope = result_setup(tmp_path)
    applied = ResultService(store).apply(envelope.result_id)
    assert applied['status'] == 'applied'
    validation = applied['validation']
    assert validation['attempts'] == 1 and validation['valid'] is True
    assert validation['seconds'] >= 0
    assert validation['completed_at'] >= validation['started_at']
    assert applied['timings']['resolution_at'] >= applied['timings']['application_started_at']
    progress = store.snapshot()['runs'][claim.run['id']]['domain_progress']
    assert progress['accepted_transition_at'] == applied['updated_at']
    assert progress['claim_to_transition_seconds'] >= 0
    before = store.path.read_bytes()
    assert ResultService(store).apply(envelope.result_id) == applied
    assert store.path.read_bytes() == before


def test_rejected_validation_never_claims_accepted_progress(tmp_path):
    store, source, claim, envelope = result_setup(tmp_path, payload={'to_queue': 'verified'})
    result = ResultService(store).apply(envelope.result_id)
    assert result['status'] == 'rejected'
    assert result['validation']['valid'] is False
    assert result['timings']['application_elapsed_seconds'] is None
    assert 'domain_progress' not in store.snapshot()['runs'][claim.run['id']]


def test_interrupted_validation_is_unknown_then_records_the_retry_attempt(tmp_path, monkeypatch):
    store, source, claim, envelope = result_setup(tmp_path)
    service = ResultService(store)
    def crash(*args):
        raise RuntimeError('interrupted before preparation completed')
    monkeypatch.setattr(service, '_prepare', crash)
    with pytest.raises(RuntimeError, match='interrupted'):
        service.apply(envelope.result_id)
    pending = store.snapshot()['results'][envelope.result_id]['validation']
    assert pending['attempts'] == 1 and pending['seconds'] is None and pending['completed_at'] is None
    result = ResultService(RunStore(store.runtime)).apply(envelope.result_id)
    assert result['status'] == 'applied'
    assert result['validation']['attempts'] == 2 and result['validation']['valid'] is True
    assert result['validation']['attempt_id'] != pending['attempt_id']


def test_application_recovery_keeps_the_original_start_and_validation(tmp_path):
    store, source, claim, envelope = result_setup(tmp_path)
    def crash(stage):
        if stage == 'task_written':
            raise RuntimeError('interrupted application')
    with pytest.raises(RuntimeError):
        ResultService(store, checkpoint=crash).apply(envelope.result_id)
    before = store.snapshot()['results'][envelope.result_id]
    result = ResultService(RunStore(store.runtime)).apply(envelope.result_id)
    assert result['status'] == 'applied'
    assert result['validation'] == before['validation']
    assert result['timings']['application_started_at'] == before['timings']['application_started_at']


def test_conversation_queue_wait_and_resolution_survive_reopen(tmp_path):
    from test_acp_conversations import setup as chat_setup
    from greatminds.runtime.interactions import ConversationStore
    chat = chat_setup(tmp_path)
    chat.clock = lambda: 10
    chat.enqueue('hello', request_id='one')
    doc = chat.snapshot()
    chat.acquire('owner', config_sha256=doc['config_sha256'], schema_sha256=doc['schema_sha256'])
    chat.clock = lambda: 15
    turn = chat.claim_next('owner')
    assert turn['queue_wait_seconds'] == 5
    chat.clock = lambda: 20
    chat.finish('owner', 'one', status='completed', reason='end_turn')
    before = chat.path.read_bytes()
    chat.finish('owner', 'one', status='completed', reason='end_turn')
    assert chat.path.read_bytes() == before
    restored = ConversationStore(tmp_path / '.greatminds', chat.id).snapshot()['turns']['one']
    assert restored['started_to_resolution_seconds'] == 5 and restored['resolved_at'] == 20


def test_queued_cancel_does_not_invent_execution_duration(tmp_path):
    from test_acp_conversations import setup as chat_setup
    chat = chat_setup(tmp_path)
    chat.enqueue('hello', request_id='one')
    chat.cancel('one')
    turn = chat.snapshot()['turns']['one']
    assert turn['resolved_at'] >= turn['queued_at']
    assert turn['started_to_resolution_seconds'] is None


def test_private_timing_summary_is_numeric_and_rejects_malformed_optional_data():
    from greatminds.runtime.task_timings import summary
    observed = summary({'queue_observation': {'wait_seconds': 3, 'secret': 'SECRET'},
                        'domain_progress': ['SECRET']},
                       {'validation': {'seconds': 'SECRET', 'attempts': 2},
                        'timings': {'application_elapsed_seconds': -1}})
    assert observed['observed_queue_wait_seconds'] == 3
    assert observed['preparation_validation_attempts'] == 2
    assert all(value is None for key, value in observed.items()
               if key not in {'observed_queue_wait_seconds', 'preparation_validation_attempts'})


def test_regressing_clock_keeps_accepted_transition_but_no_invented_latency(tmp_path):
    store, source, claim, envelope = result_setup(tmp_path)
    store.clock = lambda: 10
    receipt = ResultService(store).apply(envelope.result_id)
    assert receipt['status'] == 'applied'
    assert receipt['timings']['received_to_resolution_seconds'] is None
    assert store.snapshot()['runs'][claim.run['id']]['domain_progress']['claim_to_transition_seconds'] is None
