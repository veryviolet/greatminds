"""Durable pre-prompt retries are bounded and rechecked by atomic admission."""
from dataclasses import replace

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.observation import configuration
from greatminds.runtime.store import RunStore, TaskRevision
from greatminds.runtime.retry_policy import retry_admission
from greatminds.runtime.daemon import assignments
from test_acp_daemon import project


def fixture(tmp_path, **options):
    import yaml
    root = project(tmp_path)
    if options:
        path = root / 'coordination/execution.yaml'
        document = yaml.safe_load(path.read_text())
        document['bindings']['implementation'].update(options)
        path.write_text(yaml.safe_dump(document))
    schema, config = configuration(root)
    now = [1000.0]
    store = RunStore(root / '.greatminds', clock=lambda: now[0])
    task = TaskRevision.capture(store.runtime, store.runtime / 'feature_dev/0001-example.yaml')
    binding = config.bindings[0]
    def claim():
        return store.claim(task=task, binding=binding, config=config, schema=schema,
                           project=root, owner_id='test', automatic=True)
    def fail(run, *, reason='timeout', prompt_started=False, error_type=None):
        store.transition(run.run['id'], owner_id='test', target='starting', event_id='start')
        store.transition(run.run['id'], owner_id='test', target='failed', event_id='fail', reason=reason,
                         details={'prompt_started': prompt_started, 'pre_prompt_activity': False, 'error_type': error_type})
    return root, store, schema, config, binding, task, now, claim, fail


def test_delay_survives_restart_and_stops_at_budget(tmp_path):
    root, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path)
    first = claim()
    fail(first)
    for delay in (5, 10):
        assert list(assignments(store, config, schema))[0][2] == 'retry_backoff'
        with pytest.raises(GreatMindsError, match='retry_backoff'):
            claim()
        restarted = RunStore(store.runtime, clock=lambda: now[0])
        verdict = retry_admission(restarted.snapshot(), binding, task, config, schema, now[0])
        assert verdict['next_at'] == now[0] + delay
        now[0] += delay
        assert list(assignments(store, config, schema))[0][2] == 'ready'
        fail(claim())
    assert list(assignments(store, config, schema))[0][2] == 'startup_retry_limit'
    with pytest.raises(GreatMindsError, match='startup_retry_limit'):
        claim()
    events = store.snapshot()['events']
    assert sum(e['kind'] == 'startup_retry_dispatch' for e in events) == 2
    assert len(store.snapshot()['runs']) == 3


@pytest.mark.parametrize('reason,prompt,error', [
    ('timeout', True, None), ('configuration_error', False, None),
    ('authentication_required', False, None), ('protocol_error', False, None),
    ('transport_failure', False, 'FileNotFoundError'),
    ('transport_failure', False, 'PermissionError'), ('timeout', None, None),
])
def test_permanent_or_uncertain_outcomes_never_auto_retry(tmp_path, reason, prompt, error):
    _, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path)
    fail(claim(), reason=reason, prompt_started=prompt, error_type=error)
    now[0] += 10000
    assert retry_admission(store.snapshot(), binding, task, config, schema, now[0])['reason'] == 'revision_already_attempted'
    with pytest.raises(GreatMindsError):
        claim()


def test_disabled_retry_and_contract_drift_do_not_dispatch(tmp_path):
    _, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path)
    fail(claim())
    now[0] += 100
    changed = replace(binding, max_startup_retries=0)
    assert retry_admission(store.snapshot(), changed, task, config, schema, now[0])['reason'] != 'ready'
    assert retry_admission(store.snapshot(), binding, task, replace(config, max_running=1), schema, now[0])['reason'] != 'ready'


def test_completed_command_before_prompt_is_not_replayed(tmp_path):
    _, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path)
    run = claim()
    fail(run)
    snapshot = store.snapshot()
    snapshot['commands'] = {'test': {'run_id': run.run['id'], 'status': 'completed'}}
    now[0] += 100
    assert retry_admission(snapshot, binding, task, config, schema, now[0])['reason'] != 'ready'


@pytest.mark.parametrize('field,value', [('max_startup_retries', -1), ('max_startup_retries', True),
    ('max_startup_retries', 21), ('retry_initial_seconds', 0), ('retry_max_seconds', False)])
def test_retry_config_rejects_invalid_budgets(tmp_path, field, value):
    import yaml
    root = project(tmp_path)
    path = root / 'coordination/execution.yaml'
    document = yaml.safe_load(path.read_text())
    document['bindings']['implementation'][field] = value
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(GreatMindsError):
        configuration(root)


def test_supervisor_records_pre_prompt_failure_and_daemon_retries_boundedly(tmp_path, monkeypatch):
    import asyncio
    from greatminds.runtime import daemon, supervisor
    _, store, schema, config, binding, task, now, _, _ = fixture(tmp_path)
    starts = []
    class FailingTransport:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            starts.append(now[0])
            raise TimeoutError()
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(supervisor, 'AcpTransport', FailingTransport)
    monkeypatch.setattr(daemon, 'RunStore', lambda runtime: store)
    for elapsed in (0, 4, 1, 9, 1, 100):
        now[0] += elapsed
        asyncio.run(daemon.serve(tmp_path, once=True, environment={}))
    assert starts == [1000.0, 1005.0, 1015.0]
    assert all(r['outcome']['prompt_started'] is False for r in store.snapshot()['runs'].values())
    assert list(assignments(store, config, schema))[0][2] == 'startup_retry_limit'


@pytest.mark.parametrize('limit', [0, 2])
def test_configured_zero_and_delay_cap_are_enforced(tmp_path, limit):
    _, store, schema, config, binding, task, now, claim, fail = fixture(
        tmp_path, max_startup_retries=limit, retry_initial_seconds=50, retry_max_seconds=10)
    fail(claim())
    verdict = retry_admission(store.snapshot(), binding, task, config, schema, now[0])
    if limit == 0:
        assert verdict['reason'] == 'startup_retry_limit'
    else:
        assert verdict['next_at'] == now[0] + 10
        now[0] += 10
        fail(claim())
        assert retry_admission(store.snapshot(), binding, task, config, schema, now[0])['next_at'] == now[0] + 10


def test_operator_status_exposes_retry_time_and_budget(tmp_path):
    import json
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    _, _, _, _, _, _, _, claim, fail = fixture(tmp_path)
    fail(claim())
    response = CliRunner().invoke(cli, ['run', 'status', '--project-dir', str(tmp_path)])
    assert response.exit_code == 0, response.output
    retry = json.loads(response.output)['assignments'][0]['retry']
    assert retry['next_at'] == 1005.0
    assert retry['failures'] == 1 and retry['max_retries'] == 2


def test_pre_prompt_callback_activity_prevents_automatic_replay(tmp_path):
    _, store, schema, config, binding, task, now, claim, fail = fixture(tmp_path)
    run = claim()
    fail(run)
    state = store.snapshot()
    state['runs'][run.run['id']]['outcome']['pre_prompt_activity'] = True
    assert retry_admission(state, binding, task, config, schema, now[0] + 100)['reason'] != 'ready'
