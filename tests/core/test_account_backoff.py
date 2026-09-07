"""Shared account startup delay covers new tasks and survives store reopen."""
from dataclasses import replace
import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.store import RunStore, TaskRevision
from greatminds.runtime.retry_policy import account_backoff
from greatminds.runtime.daemon import assignments
from test_startup_retry_policy import fixture


def another_task(store):
    path = store.runtime / 'feature_dev/0002-another.yaml'
    path.write_text('id: 0002-another\n')
    return TaskRevision.capture(store.runtime, path)


def test_new_task_and_other_binding_wait_but_other_account_runs(tmp_path):
    root, store, schema, config, binding, _, now, claim, fail = fixture(tmp_path)
    fail(claim())
    task = another_task(store)
    same = replace(binding, id='other-binding')
    separate = replace(binding, id='separate-binding', account='separate')
    config = replace(config, bindings=(binding, same, separate))
    rows = {(b.id, t.task_id): reason for b, t, reason in assignments(store, config, schema)}
    assert rows[(same.id, task.task_id)] == 'account_backoff'
    assert rows[(separate.id, task.task_id)] == 'ready'
    with pytest.raises(GreatMindsError, match='account_backoff'):
        store.claim(task=task, binding=same, config=config, schema=schema, project=root, owner_id='test')
    restarted = RunStore(store.runtime, clock=lambda: now[0])
    assert account_backoff(restarted.snapshot(), binding.account, config, now[0])['next_at'] == 1005
    run = restarted.claim(task=task, binding=separate, config=config, schema=schema, project=root, owner_id='test')
    assert run.run['account'] == 'separate'


def test_failure_streak_spans_tasks_and_is_capped(tmp_path):
    root, store, schema, config, binding, _, now, claim, fail = fixture(tmp_path)
    config = replace(config, account_retry_max_seconds=6)
    fail(claim())
    assert account_backoff(store.snapshot(), binding.account, config, now[0])['next_at'] == 1005
    now[0] = 1005
    run = store.claim(task=another_task(store), binding=binding, config=config, schema=schema, project=root, owner_id='test')
    fail(run)
    verdict = account_backoff(store.snapshot(), binding.account, config, now[0])
    assert verdict['next_at'] == 1011 and verdict['failures'] == 2
    assert account_backoff(store.snapshot(), binding.account, config, 1011)['reason'] == 'ready'


def test_prompt_start_resets_connectivity_streak_without_claiming_task_progress(tmp_path):
    root, store, schema, config, binding, _, now, claim, fail = fixture(tmp_path)
    fail(claim())
    now[0] += 5
    run = store.claim(task=another_task(store), binding=binding, config=config, schema=schema, project=root, owner_id='test')
    store.transition(run.run['id'], owner_id='test', target='starting', event_id='start')
    store.transition(run.run['id'], owner_id='test', target='running', session_id='session', event_id='running')
    assert account_backoff(store.snapshot(), binding.account, config, now[0])['failures'] == 0
    store.transition(run.run['id'], owner_id='test', target='completed', reason='turn_ended', event_id='end',
                     details={'prompt_started': True})
    assert account_backoff(store.snapshot(), binding.account, config, now[0])['reason'] == 'ready'
    assert store.snapshot()['results'] == {}


def test_conversation_startup_failure_also_holds_account(tmp_path):
    _, store, _, config, binding, _, now, claim, fail = fixture(tmp_path)
    run = claim()
    fail(run)
    snapshot = store.snapshot()
    snapshot['runs'][run.run['id']]['conversation_id'] = 'synthetic-conversation'
    assert account_backoff(snapshot, binding.account, config, now[0])['reason'] == 'account_backoff'


@pytest.mark.parametrize('field,value', [('account_retry_initial_seconds', 0),
    ('account_retry_max_seconds', True), ('account_retry_max_seconds', -1)])
def test_invalid_account_delays_are_rejected(tmp_path, field, value):
    import yaml
    from greatminds.runtime.observation import configuration
    from test_acp_daemon import project
    root = project(tmp_path)
    path = root / 'coordination/execution.yaml'
    document = yaml.safe_load(path.read_text())
    document[field] = value
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(GreatMindsError):
        configuration(root)


def test_explicit_retry_does_not_bypass_shared_delay(tmp_path):
    _, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path)
    run = claim()
    fail(run)
    control = store.request_control(run.run['id'], 'retry')
    store.control_status(run.run['id'], control['id'], completed=True)
    assert list(assignments(store, config, schema))[0][2] == 'account_backoff'
    with pytest.raises(GreatMindsError, match='account_backoff'):
        claim()
    now[0] += 5
    assert claim().run['state'] == 'claimed'


def test_status_exposes_shared_account_counter(tmp_path):
    import json
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    _, _, _, _, binding, _, _, claim, fail = fixture(tmp_path)
    fail(claim())
    response = CliRunner().invoke(cli, ['run', 'status', '--project-dir', str(tmp_path)])
    assert response.exit_code == 0, response.output
    account = json.loads(response.output)['accounts'][binding.account]
    assert account['failures'] == 1 and account['next_at'] == 1005


def test_interactive_admission_waits_without_consuming_user_message(tmp_path):
    from greatminds.runtime.interactions import ConversationStore
    root, store, schema, config, binding, _, now, claim, fail = fixture(tmp_path)
    fail(claim())
    conversation = ConversationStore.create(store.runtime, binding=binding,
        config_sha256=config.sha256, schema_sha256=schema.sha256, workspace=root)
    conversation.enqueue('synthetic user request', request_id='message')
    task = TaskRevision.conversation(store.runtime, conversation.id)
    with pytest.raises(GreatMindsError, match='account_backoff'):
        store.claim(task=task, binding=binding, config=config, schema=schema,
                    project=root, owner_id='test', conversation_id=conversation.id)
    assert conversation.snapshot()['turns']['message']['status'] == 'queued'
    now[0] += 5
    run = store.claim(task=task, binding=binding, config=config, schema=schema,
                      project=root, owner_id='test', conversation_id=conversation.id)
    assert run.run['conversation_id'] == conversation.id


def test_connectivity_reset_keeps_original_time_after_crash_recovery(tmp_path):
    root, store, schema, config, binding, _, now, claim, fail = fixture(tmp_path, max_running=3)
    fail(claim())
    now[0] = 1005
    healthy = store.claim(task=another_task(store), binding=binding, config=config, schema=schema,
                          project=root, owner_id='test')
    store.transition(healthy.run['id'], owner_id='test', target='starting', event_id='start')
    store.transition(healthy.run['id'], owner_id='test', target='running', session_id='session', event_id='ready')
    assert account_backoff(store.snapshot(), binding.account, config, now[0])['failures'] == 0
    now[0] = 1006
    path = store.runtime / 'feature_dev/0003-third.yaml'
    path.write_text('id: 0003-third\n')
    newer = store.claim(task=TaskRevision.capture(store.runtime, path), binding=binding,
                         config=config, schema=schema, project=root, owner_id='test')
    fail(newer)
    before = account_backoff(store.snapshot(), binding.account, config, now[0])
    assert before['failures'] == 1 and before['next_at'] == 1011
    now[0] = 1007
    # No synthetic process was launched, so recovery requires no termination.
    store.recover_run(healthy.run['id'], previous_owner='test', owner_id='restarted')
    reopened = RunStore(store.runtime, clock=lambda: now[0])
    assert reopened.snapshot()['runs'][healthy.run['id']]['startup_ready_at'] == 1005
    assert account_backoff(reopened.snapshot(), binding.account, config, now[0]) == before


def test_permission_resume_is_not_a_new_connectivity_reset(tmp_path):
    root, store, schema, config, binding, _, now, claim, fail = fixture(tmp_path, max_running=3)
    healthy = claim()
    store.transition(healthy.run['id'], owner_id='test', target='starting', event_id='start')
    store.transition(healthy.run['id'], owner_id='test', target='running', session_id='session', event_id='ready')
    now[0] += 1
    fail(store.claim(task=another_task(store), binding=binding, config=config, schema=schema,
                     project=root, owner_id='test'))
    before = account_backoff(store.snapshot(), binding.account, config, now[0])
    store.transition(healthy.run['id'], owner_id='test', target='waiting_input', event_id='ask')
    now[0] += 1
    store.transition(healthy.run['id'], owner_id='test', target='running', session_id='session', event_id='resume')
    assert account_backoff(store.snapshot(), binding.account, config, now[0]) == before
