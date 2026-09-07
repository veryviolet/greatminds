import asyncio
from dataclasses import replace
import json

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.account_limits import AccountLimits, AccountLimitError, admission, parse_rules
from greatminds.runtime.daemon import assignments, serve
from greatminds.runtime.store import RunStore, TaskRevision
from test_startup_retry_policy import fixture
from test_account_backoff import another_task
from test_acp_daemon import project


RULES = [{'code': 42901, 'kind': 'rate_limit', 'cooldown_seconds': 60},
         {'code': 42902, 'kind': 'quota_exhausted'}]


def configured(tmp_path):
    root, store, schema, config, binding, task, now, _, _ = fixture(tmp_path)
    config = replace(config, agents=(replace(config.agents[0], account_limit_errors=parse_rules(RULES)),))
    def claim(task=task, selected=binding):
        run = store.claim(task=task, binding=selected, config=config, schema=schema,
                          project=root, owner_id='test')
        store.transition(run.run['id'], owner_id='test', target='starting', event_id='start')
        return run
    return root, store, schema, config, binding, task, now, claim


@pytest.mark.parametrize('raw', [None, {}, [{'code': True, 'kind': 'quota_exhausted'}],
    [{'code': -32603, 'kind': 'quota_exhausted'}], [{'code': -32000, 'kind': 'quota_exhausted'}],
    [{'code': 42901, 'kind': 'rate_limit'}], [{'code': 42901, 'kind': 'rate_limit', 'cooldown_seconds': True}],
    [{'code': 42901, 'kind': 'rate_limit', 'cooldown_seconds': 86401}],
    [{'code': 42901, 'kind': 'quota_exhausted', 'cooldown_seconds': 60}], RULES + RULES,
    [{'code': 42901, 'kind': 'rate_limit', 'match_message': 'quota'}]])
def test_only_explicit_bounded_application_codes_are_configurable(raw):
    with pytest.raises(GreatMindsError):
        parse_rules(raw)


def test_shared_cooldown_survives_restart_retry_and_config_change(tmp_path):
    root, store, schema, config, binding, task, now, claim = configured(tmp_path)
    run = claim()
    service = AccountLimits(store)
    signal = service.record(run.run['id'], owner_id='test', rpc_code=42901)
    store.transition(run.run['id'], owner_id='test', target='failed', event_id='failed', reason='account_rate_limit')
    control = store.request_control(run.run['id'], 'retry')
    store.control_status(run.run['id'], control['id'], completed=True)
    second = another_task(store)
    config = replace(config, agents=(replace(config.agents[0], account_limit_errors=()),))
    restarted = RunStore(store.runtime, clock=lambda: now[0])
    assert admission(restarted.snapshot(), binding.account, now[0])['next_at'] == 1060
    for automatic in (True, False):
        with pytest.raises(GreatMindsError, match='account_rate_limit'):
            restarted.claim(task=task, binding=binding, config=config, schema=schema,
                            project=root, owner_id='test', automatic=automatic)
        with pytest.raises(GreatMindsError, match='account_rate_limit'):
            restarted.claim(task=second, binding=binding, config=config, schema=schema,
                            project=root, owner_id='test', automatic=automatic)
    rows = {row[1].task_id: row[2] for row in assignments(restarted, config, schema)}
    assert rows[second.task_id] == 'account_rate_limit'
    assert rows[task.task_id] != 'ready'
    assert signal['rpc_code'] == 42901
    now[0] = 1060
    assert admission(restarted.snapshot(), binding.account, now[0])['reason'] == 'ready'
    assert restarted.claim(task=second, binding=binding, config=config, schema=schema,
                           project=root, owner_id='test').run['account'] == binding.account


def test_concurrent_signal_cannot_shorten_hold_and_stale_resume_fails(tmp_path):
    root, store, schema, config, binding, task, now, claim = configured(tmp_path)
    second = another_task(store)
    other = replace(binding, id='other')
    config = replace(config, bindings=(binding, other))
    first = store.claim(task=task, binding=binding, config=config, schema=schema, project=root, owner_id='test')
    next_run = store.claim(task=second, binding=other, config=config, schema=schema, project=root, owner_id='test')
    service = AccountLimits(store)
    first_signal = service.record(first.run['id'], owner_id='test', rpc_code=42902)
    second_signal = service.record(next_run.run['id'], owner_id='test', rpc_code=42901)
    assert first_signal['hold_id'] != second_signal['hold_id']
    verdict = admission(store.snapshot(), binding.account, 1000000)
    assert verdict['reason'] == 'account_quota_exhausted' and verdict['next_at'] is None
    with pytest.raises(GreatMindsError, match='changed'):
        service.resume(binding.account, hold_id=first_signal['hold_id'], reason='inspected')
    before = store.path.read_bytes()
    assert service.record(next_run.run['id'], owner_id='test', rpc_code=42901) == second_signal
    assert store.path.read_bytes() == before
    resolved = service.resume(binding.account, hold_id=second_signal['hold_id'], reason='inspected')
    before = store.path.read_bytes()
    assert service.resume(binding.account, hold_id=second_signal['hold_id'], reason='inspected') == resolved
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('scenario,expected', [('account-rate-error', 'account_rate_limit'),
    ('account-quota-error', 'account_quota_exhausted'), ('account-generic-error', 'protocol_error'),
    ('account-init-error', 'account_quota_exhausted')])
def test_real_acp_error_holds_account_without_parsing_message_or_restarting(tmp_path, scenario, expected):
    root = project(tmp_path, scenario=scenario)
    path = root / 'coordination/execution.yaml'
    doc = yaml.safe_load(path.read_text())
    doc['agents']['anything-acp']['account_limit_errors'] = RULES
    path.write_text(yaml.safe_dump(doc))
    state = asyncio.run(serve(root, once=True, interval=.2, environment={}))
    run = next(iter(state['runs'].values()))
    assert run['state'] == 'failed' and run['reason'] == expected
    assert 'SECRET-' not in json.dumps(state)
    if expected != 'protocol_error':
        another_task(RunStore(root / '.greatminds'))
        reopened = asyncio.run(serve(root, once=True, interval=.2, environment={}))
        assert len(reopened['runs']) == 1
        assert (root / 'agent-starts.log').read_text().splitlines() == [scenario]
        assert admission(reopened, 'default', 0)['reason'] == expected
    else:
        assert not state.get('account_holds')


@pytest.mark.parametrize('credential', ['GREATMINDS_RUN_ID', 'GREATMINDS_RUN_TOKEN'])
def test_agent_credential_alone_cannot_resume_account(tmp_path, monkeypatch, credential):
    root, store, _, _, binding, _, _, claim = configured(tmp_path)
    signal = AccountLimits(store).record(claim().run['id'], owner_id='test', rpc_code=42902)
    monkeypatch.setenv('GREATMINDS_PROJECT_DIR', str(root))
    monkeypatch.setenv(credential, 'agent')
    result = CliRunner().invoke(cli, ['run', 'account-resume', binding.account,
        '--hold-id', signal['hold_id'], '--reason', 'inspected'])
    assert result.exit_code == 3 and 'operator' in result.output
    assert admission(store.snapshot(), binding.account, 1000000)['reason'] == 'account_quota_exhausted'


def test_other_account_runs_and_wrong_owner_cannot_report_limit(tmp_path):
    root, store, schema, config, binding, _, _, claim = configured(tmp_path)
    first = claim()
    service = AccountLimits(store)
    with pytest.raises(GreatMindsError, match='owner'):
        service.record(first.run['id'], owner_id='foreign', rpc_code=42902)
    assert not store.snapshot().get('account_holds')
    service.record(first.run['id'], owner_id='test', rpc_code=42902)
    other = replace(binding, id='separate', account='separate')
    config = replace(config, bindings=(binding, other))
    assert store.claim(task=another_task(store), binding=other, config=config, schema=schema,
                       project=root, owner_id='test').run['account'] == 'separate'


def test_doctor_exact_scoped_action_resolves_hold_idempotently(tmp_path, monkeypatch):
    from greatminds.runtime.diagnostics import diagnose
    root, store, _, _, binding, _, _, claim = configured(tmp_path)
    AccountLimits(store).record(claim().run['id'], owner_id='test', rpc_code=42902)
    report = diagnose(root, environment={})
    finding = next(f for f in report['findings'] if f['component'] == 'accounts')
    action = finding['recovery_actions'][0]
    monkeypatch.setenv('GREATMINDS_PROJECT_DIR', action['environment']['GREATMINDS_PROJECT_DIR'])
    monkeypatch.chdir(tmp_path.parent)
    argv = action['argv'][1:] + ['--reason', 'account recovery independently checked']
    first = CliRunner().invoke(cli, argv)
    assert first.exit_code == 0, first.output
    before = store.path.read_bytes()
    second = CliRunner().invoke(cli, argv)
    assert second.exit_code == 0 and first.output == second.output
    assert store.path.read_bytes() == before
    assert not [f for f in diagnose(root, environment={})['findings'] if f['component'] == 'accounts']
    assert not (root / 'agent-starts.log').exists()


def test_open_conversation_stops_before_next_prompt_on_shared_account_hold(tmp_path, monkeypatch):
    from test_supervisor import setup, supervisor
    from greatminds.runtime.interactions import ConversationStore
    store, schema, config, task = setup(tmp_path)
    binding = replace(config.bindings[0], max_running=2, timeout_seconds=10)
    config = replace(config, bindings=(binding,),
                     agents=(replace(config.agents[0], account_limit_errors=parse_rules(RULES)),))
    conversation = ConversationStore.create(store.runtime, binding=binding, config_sha256=config.sha256,
        schema_sha256=schema.sha256, workspace=tmp_path)
    conversation.enqueue('first prompt', request_id='first')
    conversation.enqueue('must remain unsent', request_id='second')
    original = store.usage_prompt_boundary
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            donor = service.claim(task, binding)
            conversation.acquire(service.id, config_sha256=config.sha256, schema_sha256=schema.sha256)
            run = service.claim(TaskRevision.conversation(store.runtime, conversation.id), binding,
                                conversation_id=conversation.id)
            def observed(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs.get('phase') == 'finish':
                    AccountLimits(store).record(donor.run['id'], owner_id=service.id, rpc_code=42902)
                return result
            monkeypatch.setattr(store, 'usage_prompt_boundary', observed)
            ended = await service.execute(run, binding=binding, conversation=conversation)
            assert ended['reason'] == 'account_quota_exhausted', ended
            assert len(store.snapshot()['runs'][run.run['id']]['input_reservations']) == 1
            journal = conversation.snapshot()
            assert journal['turns']['first']['status'] == 'completed'
            assert journal['turns']['second']['status'] == 'failed'
    asyncio.run(check())
