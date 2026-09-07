"""Completed turns without a workflow advance cannot create an endless queue loop."""
import asyncio
import json

import pytest
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.daemon import assignments
from greatminds.runtime.retry_policy import no_progress, retry_admission
from greatminds.runtime.store import RunStore, TaskRevision
from test_startup_retry_policy import fixture


def complete(store, claim):
    run_id = claim.run['id']
    store.transition(run_id, owner_id='test', target='starting', event_id='start')
    store.transition(run_id, owner_id='test', target='running', event_id='running')
    store.transition(run_id, owner_id='test', target='completed', event_id='end', reason='turn_ended',
                     details={'prompt_started': True, 'updates': 100})


def test_configured_continuations_are_delayed_and_bounded_across_restart(tmp_path):
    _, store, schema, config, binding, task, now, claim, _ = fixture(tmp_path, max_no_progress_turns=3)
    for turn in range(3):
        complete(store, claim())
        restarted = RunStore(store.runtime, clock=lambda: now[0])
        verdict = retry_admission(restarted.snapshot(), binding, task, config, schema, now[0])
        assert verdict['turns'] == turn + 1
        expected = 'no_progress_limit' if turn == 2 else 'no_progress_backoff'
        assert verdict['reason'] == expected
        assert list(assignments(store, config, schema))[0][2] == expected
        with pytest.raises(GreatMindsError, match=expected):
            claim()
        now[0] += 5
    assert len(store.snapshot()['runs']) == 3
    assert sum(e['kind'] == 'no_progress_continuation' for e in store.snapshot()['events']) == 2


def test_content_only_revision_change_does_not_reset_exhausted_budget(tmp_path):
    root, store, schema, config, binding, task, _, claim, _ = fixture(tmp_path)
    complete(store, claim())
    path = store.runtime / task.path
    path.write_text(path.read_text() + '\n# formatting change\n')
    revised = TaskRevision.capture(store.runtime, path)
    assert revised.sha256 != task.sha256
    assert list(assignments(store, config, schema))[0][2] == 'no_progress_limit'
    with pytest.raises(GreatMindsError, match='no_progress_limit'):
        store.claim(task=revised, binding=binding, config=config, schema=schema,
                    project=root, owner_id='test', automatic=True)


@pytest.mark.parametrize('decision,status,resets', [('handoff', 'applied', True),
    ('blocked', 'applied', True), ('handoff', 'rejected', False), ('no_change', 'applied', False)])
def test_only_applied_queue_decisions_reset_progress_counter(tmp_path, decision, status, resets):
    _, store, _, _, binding, task, _, claim, _ = fixture(tmp_path)
    run = claim()
    complete(store, run)
    state = store.snapshot()
    state['results']['fixture'] = {'status': status, 'envelope': {'run_id': run.run['id'], 'decision': decision}}
    assert no_progress(state, binding, task)['turns'] == (0 if resets else 1)


def test_explicit_retry_is_one_attempt_not_a_counter_reset(tmp_path):
    _, store, _, _, binding, task, _, claim, _ = fixture(tmp_path)
    run = claim()
    complete(store, run)
    control = store.request_control(run.run['id'], 'retry')
    store.control_status(run.run['id'], control['id'], completed=True)
    complete(store, claim())
    assert no_progress(store.snapshot(), binding, task)['turns'] == 2
    with pytest.raises(GreatMindsError, match='no_progress_limit'):
        claim()


def test_completed_command_prevents_automatic_continuation(tmp_path):
    _, store, schema, config, binding, task, now, claim, _ = fixture(tmp_path, max_no_progress_turns=3)
    run = claim()
    complete(store, run)
    state = store.snapshot()
    state['commands'] = {'fixture': {'run_id': run.run['id'], 'status': 'completed'}}
    assert retry_admission(state, binding, task, config, schema, now[0]+100)['reason'] != 'ready'


def test_status_exposes_budget_not_token_stream_as_progress(tmp_path):
    _, store, _, _, _, _, _, claim, _ = fixture(tmp_path)
    complete(store, claim())
    response = CliRunner().invoke(cli, ['run', 'status', '--project-dir', str(tmp_path)])
    assert response.exit_code == 0, response.output
    progress = json.loads(response.output)['assignments'][0]['progress']
    assert progress == {'reason': 'no_progress_limit', 'turns': 1, 'max_turns': 1}


def test_real_acp_turns_stop_after_configured_budget(tmp_path):
    from greatminds.runtime import daemon
    from test_acp_daemon import project
    import yaml
    root = project(tmp_path)
    path = root / 'coordination/execution.yaml'
    doc = yaml.safe_load(path.read_text())
    doc['bindings']['implementation'].update(max_no_progress_turns=2, retry_initial_seconds=1)
    path.write_text(yaml.safe_dump(doc))
    async def run():
        await daemon.serve(root, once=True, environment={})
        await asyncio.sleep(1.1)
        await daemon.serve(root, once=True, environment={})
        return await daemon.serve(root, once=True, environment={})
    state = asyncio.run(run())
    assert len(state['runs']) == 2
    assert all(r['state'] == 'completed' for r in state['runs'].values())
    assert (root / 'agent-starts.log').read_text().splitlines() == ['echo', 'echo']


@pytest.mark.parametrize('decision,expected', [('needs_input', 'human_input_required'),
    ('no_change', 'no_change_reported')])
def test_semantic_hold_survives_content_only_revision(tmp_path, decision, expected):
    _, store, schema, config, binding, task, now, claim, _ = fixture(tmp_path, max_no_progress_turns=3)
    run = claim()
    complete(store, run)
    state = store.snapshot()
    state['results']['fixture'] = {'status': 'applied', 'envelope': {'run_id': run.run['id'], 'decision': decision}}
    path = store.runtime / task.path
    path.write_text(path.read_text() + '\n# result updated metadata\n')
    changed = TaskRevision.capture(store.runtime, path)
    assert retry_admission(state, binding, changed, config, schema, now[0]+100)['reason'] == expected


@pytest.mark.parametrize('value', [0, -1, True, 21])
def test_invalid_no_progress_limit_is_rejected(tmp_path, value):
    with pytest.raises(GreatMindsError, match='max_no_progress_turns'):
        fixture(tmp_path, max_no_progress_turns=value)


def test_changed_task_after_failed_prompt_still_requires_explicit_retry(tmp_path):
    _, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path, max_no_progress_turns=3)
    fail(claim(), reason='timeout', prompt_started=True)
    path = store.runtime / task.path
    path.write_text(path.read_text() + '\n# changed before transport failure\n')
    changed = TaskRevision.capture(store.runtime, path)
    assert retry_admission(store.snapshot(), binding, changed, config, schema, now[0]+100)['reason'] != 'ready'
