import asyncio
import json
from pathlib import Path

import pytest
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.planning import PlanningService
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.context import context_document
from greatminds.runtime.daemon import serve
from greatminds.runtime.store import RunStore, TaskRevision
from test_acp_conversations import setup


def planner(root, role='ARCHITECT-PLANNER'):
    chat = setup(root, role=role)
    schema = load_schema_snapshot()
    config = load_execution_config(root/'coordination/execution.yaml', roles=set(schema.document['roles']))
    store = RunStore(root/'.greatminds')
    claim = store.claim(task=TaskRevision.conversation(store.runtime, chat.id), binding=config.bindings[0],
        config=config, schema=schema, project=root, owner_id='fixture', conversation_id=chat.id)
    for target in ['starting', 'running']:
        store.transition(claim.run['id'], owner_id='fixture', event_id=target, target=target)
    return PlanningService(store), claim, schema


def create(service, claim, title='Plan a feature'):
    item = service.request('create', {'stream':'product','kind':'research','scope':'backend','title':title},
        run_id=claim.run['id'], token=claim.token)
    return service.apply(item['id'])


def plan_payload(task_id):
    return {'task_id':task_id,'scope':'backend','fields':{'base_commit':'abc123',
        'assignee_role':'DEVELOPER','stand_required':False,'plan_kind':'full','mode':'A',
        'ready_for_implementation':True},'body':'Implement and independently test the change.',
        'triage':None,'stop_at':None,'audit_only':False}


def test_standalone_planner_creates_and_routes_multiple_tasks_without_rebinding_run(tmp_path):
    service, claim, schema = planner(tmp_path)
    for title in ['First feature', 'Second feature']:
        created = create(service, claim, title)
        assert created['status'] == 'applied', created
        payload = plan_payload(created['task_id'])
        item = service.request('plan', payload, run_id=claim.run['id'], token=claim.token)
        completed = service.apply(item['id'])
        assert completed['status'] == 'applied', completed
        assert completed['destination'].startswith('feature_dev/')
        assert service.request('plan', payload, run_id=claim.run['id'], token=claim.token) == completed
        doc = yaml.safe_load((service.store.runtime/completed['destination']).read_text())
        assert [b['kind'] for b in doc['blocks']] == ['triage','plan']
        assert all(b['provenance']['applied_by'] == 'SYSTEM' for b in doc['blocks'])
    state = service.store.snapshot()
    assert len(state['runs']) == 1 and not state['results']
    assert state['runs'][claim.run['id']]['task_id'].startswith('chat-')
    context = context_document(service.store, claim, schema)
    assert 'WAIT' in context['intake_discipline']
    assert context['planning'] and context['result_format'] is None


@pytest.mark.parametrize('stage', ['prepared','task_written','task_moved','journaled','committed'])
def test_planning_recovers_each_transition_without_duplicate_evidence(tmp_path, stage):
    service, claim, _ = planner(tmp_path)
    created = create(service, claim)
    item = service.request('plan', plan_payload(created['task_id']), run_id=claim.run['id'],token=claim.token)
    def crash(at):
        if at == stage:
            raise RuntimeError('crash')
    with pytest.raises(RuntimeError, match='crash'):
        PlanningService(service.store, checkpoint=crash).apply(item['id'])
    completed = PlanningService(service.store).apply(item['id'])
    assert completed['status'] == 'applied', completed
    doc = yaml.safe_load((service.store.runtime/completed['destination']).read_text())
    assert [b['kind'] for b in doc['blocks']] == ['triage','plan']
    rows = [json.loads(line) for line in (service.store.runtime/'journal.ndjson').read_text().splitlines()]
    assert len(rows) == 3
    assert len({r['operation_id'] for r in rows}) == 3


def test_stale_request_is_rejected_and_reserved_task_cannot_be_claimed(tmp_path):
    service, claim, _ = planner(tmp_path)
    created = create(service, claim)
    item = service.request('plan', plan_payload(created['task_id']), run_id=claim.run['id'], token=claim.token)
    path = service.store.runtime/created['destination']
    path.write_text(path.read_text()+'description: externally changed\n')
    assert service.apply(item['id'])['status'] == 'rejected'
    assert path.exists()


def test_wrong_role_and_identity_reuse_are_rejected(tmp_path):
    service, claim, _ = planner(tmp_path, role='DEVELOPER')
    with pytest.raises(GreatMindsError, match='standalone planner'):
        service.request('create', {'title':'Forbidden'},run_id=claim.run['id'],token=claim.token)


def test_request_identity_prevents_duplicate_intake_and_conflicting_payload(tmp_path):
    service, claim, _ = planner(tmp_path)
    payload = {'stream':'product','kind':'research','scope':'backend','title':'One task'}
    args = dict(run_id=claim.run['id'],token=claim.token,request_id='delivery')
    item = service.request('create',payload,**args)
    assert not list(service.store.runtime.glob('feature_inbox/*.yaml'))
    assert service.request('create',payload,**args) == item
    with pytest.raises(GreatMindsError, match='different contents'):
        service.request('create',dict(payload,title='Other'),**args)
    service.apply(item['id'])
    assert len(list(service.store.runtime.glob('feature_inbox/*.yaml'))) == 1


def test_public_planner_commands_run_through_actual_acp_daemon(tmp_path):
    chat = setup(tmp_path, scenario='planner-intake', timeout_seconds=30)
    chat.enqueue('Approved: plan both tasks.', request_id='approved')
    state = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    run = next(iter(state['runs'].values()))
    assert run['state'] == 'completed', run
    assert len(list((tmp_path/'.greatminds/feature_dev').glob('*.yaml'))) == 2
    assert len(state['planning']) == 4
    assert all(item['status'] == 'applied' for item in state['planning'].values())
    assert state['results'] == {}


def test_pending_planning_excludes_task_claims(tmp_path):
    service, claim, schema = planner(tmp_path)
    created = create(service, claim)
    request = service.request('plan', plan_payload(created['task_id']),run_id=claim.run['id'],token=claim.token)
    config = load_execution_config(tmp_path/'coordination/execution.yaml',roles=set(schema.document['roles']))
    revision = TaskRevision.capture(service.store.runtime, service.store.runtime/created['destination'])
    with pytest.raises(GreatMindsError, match='unresolved planning'):
        service.store.claim(task=revision,binding=config.bindings[0],config=config,schema=schema,
                            project=tmp_path,owner_id='another')
    assert service.apply(request['id'])['status'] == 'applied'


def test_restart_applies_accepted_intake_without_another_model_turn(tmp_path):
    service, claim, _ = planner(tmp_path)
    item = service.request('create',{'stream':'product','kind':'research','scope':'backend','title':'Durable intake'},
                           run_id=claim.run['id'],token=claim.token)
    state = asyncio.run(serve(tmp_path,once=True,environment={}))
    assert state['planning'][item['id']]['status'] == 'applied'
    assert len(state['runs']) == 1
    assert state['runs'][claim.run['id']]['state'] == 'interrupted'
    assert not (tmp_path/'agent-starts.log').exists()


def test_planner_cannot_submit_a_review_approval_or_change_another_roles_task(tmp_path):
    service, claim, _ = planner(tmp_path)
    created = create(service, claim)
    payload = {'task_id':created['task_id'], 'block':{'kind':'review','outcome':'approved','commit':'abc123'}}
    item = service.request('append',payload,run_id=claim.run['id'],token=claim.token)
    assert service.apply(item['id'])['status'] == 'rejected'
    path = service.store.runtime/created['destination']
    assert yaml.safe_load(path.read_text())['blocks'] == []
    target = service.store.runtime/'feature_dev'/path.name
    target.parent.mkdir();path.rename(target)
    with pytest.raises(GreatMindsError,match='does not own'):
        service.request('move',{'task_id':created['task_id'],'to_queue':'verified','reason':'forbidden'},
                        run_id=claim.run['id'],token=claim.token)


def test_task_bound_planner_and_invalid_credentials_cannot_use_intake(tmp_path):
    service, claim, _ = planner(tmp_path)
    with pytest.raises(GreatMindsError):
        service.request('create', {}, run_id=claim.run['id'], token='incorrect')
    with service.store._transaction() as state:
        state['runs'][claim.run['id']]['conversation_task'] = True
    with pytest.raises(GreatMindsError, match='standalone planner'):
        service.request('create', {}, run_id=claim.run['id'], token=claim.token)


def test_approved_planner_chat_reaches_verified_through_worker_pipeline():
    import importlib.util
    import shutil
    import sys
    from greatminds.runtime.interactions import ConversationStore
    tool = Path(__file__).resolve().parents[2]/'tools/productive_benchmark.py'
    spec = importlib.util.spec_from_file_location('planning_pipeline', tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, _, store = module.create_fixture(conversations=0, history=0)
    try:
        (store.runtime/'feature_dev/0001-clamp.yaml').unlink()
        path = root/'coordination/execution.yaml'
        doc = yaml.safe_load(path.read_text())
        doc['agents']['planner'] = {**doc['agents']['fixture'],
            'argv':[sys.executable, str(module.SERVER), 'planner-pipeline']}
        doc['bindings']['idle'].update(agent='planner', timeout_seconds=30)
        path.write_text(yaml.safe_dump(doc))
        schema = load_schema_snapshot()
        config = load_execution_config(path,roles=set(schema.document['roles']))
        binding = next(b for b in config.bindings if b.id == 'idle')
        chat = ConversationStore.create(store.runtime,binding=binding,config_sha256=config.sha256,
                                       schema_sha256=schema.sha256,workspace=root)
        chat.enqueue('Approved: implement and independently validate clamp.',request_id='approved')
        for _ in range(4):
            state = asyncio.run(serve(root,once=True,interval=.2,environment={}))
        assert all(r['state'] == 'completed' for r in state['runs'].values()), state['runs']
        assert len(state['runs']) == 4
        assert len(state['planning']) == 2 and all(r['status']=='applied' for r in state['planning'].values())
        assert len(state['results']) == 3 and all(r['status']=='applied' for r in state['results'].values())
        assert len(list((store.runtime/'verified').glob('*.yaml'))) == 1
        assert sum(c['status']=='succeeded' for c in state['commands'].values()) == 3
        restarted = asyncio.run(serve(root,once=True,environment={}))
        assert len(restarted['runs']) == 4
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize('stage', ['task_written','journaled'])
def test_intake_recovers_without_duplicate_task_or_journal(tmp_path, stage):
    service, claim, _ = planner(tmp_path)
    item = service.request('create',{'stream':'product','kind':'research','scope':'backend','title':'Recover intake'},
                           run_id=claim.run['id'],token=claim.token)
    def crash(at):
        if at == stage:
            raise RuntimeError('crash')
    with pytest.raises(RuntimeError, match='crash'):
        PlanningService(service.store,checkpoint=crash).apply(item['id'])
    assert service.apply(item['id'])['status'] == 'applied'
    assert len(list(service.store.runtime.glob('feature_inbox/*.yaml'))) == 1
    assert len((service.store.runtime/'journal.ndjson').read_text().splitlines()) == 1


def test_duplicate_request_survives_new_run_in_same_conversation(tmp_path):
    service, claim, schema = planner(tmp_path)
    created = create(service, claim)
    service.store.transition(claim.run['id'],owner_id='fixture',event_id='done',target='completed')
    config = load_execution_config(tmp_path/'coordination/execution.yaml',roles=set(schema.document['roles']))
    second = service.store.claim(task=TaskRevision.conversation(service.store.runtime,claim.run['conversation_id']),
        binding=config.bindings[0],config=config,schema=schema,project=tmp_path,owner_id='second',
        conversation_id=claim.run['conversation_id'])
    assert create(service, second) == created
    assert len(list(service.store.runtime.glob('feature_inbox/*.yaml'))) == 1


@pytest.mark.parametrize('status', ['received', 'applying', 'needs_recovery'])
def test_pending_planning_holds_migration_after_originating_run_ends(tmp_path, status):
    from greatminds.runtime.migration_safety import inspect_execution
    service, claim, _ = planner(tmp_path)
    item = service.request('create',{'stream':'product','kind':'research','scope':'backend','title':'Pending'},
                           run_id=claim.run['id'],token=claim.token)
    service._update(item['id'], status=status)
    service.store.transition(claim.run['id'],owner_id='fixture',event_id='done',target='completed')
    assert {'kind':'unresolved_planning','identity':item['id']} in inspect_execution(tmp_path)['holds']


@pytest.mark.parametrize('damaged', ['{invalid', '[]', '{}'])
def test_damaged_planning_operation_is_held_without_stopping_other_intake(tmp_path, damaged):
    service, claim, _ = planner(tmp_path)
    created = create(service, claim)
    item = service.request('plan',plan_payload(created['task_id']),run_id=claim.run['id'],token=claim.token)
    path = service.store.directory/'operations'/f"{item['id']}-0.json"
    def crash(at):
        if at == 'prepared':
            raise RuntimeError('crash')
    with pytest.raises(RuntimeError, match='crash'):
        PlanningService(service.store,checkpoint=crash).apply(item['id'])
    original = path.read_bytes()
    path.write_text(damaged)
    other = service.request('create',{'stream':'product','kind':'research','scope':'backend','title':'Independent'},
                            run_id=claim.run['id'],token=claim.token)
    service.reconcile()
    assert service.get(item['id'])['status'] == 'needs_recovery'
    assert service.get(other['id'])['status'] == 'applied'
    assert (service.store.runtime/created['destination']).exists()
    from greatminds.runtime.diagnostics import diagnose
    findings = diagnose(tmp_path)['findings']
    recovery = next(f for f in findings if f['component']=='planning')
    action = next(a for a in recovery['recovery_actions'] if a['argv'][-1]=='--retry')
    path.write_bytes(original)
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    result = CliRunner().invoke(cli, action['argv'][1:], env={**action['environment'],
        'GREATMINDS_RUN_ID':None, 'GREATMINDS_RUN_TOKEN':None})
    assert result.exit_code == 0, result.output
    service.reconcile()
    completed = service.get(item['id'])
    assert completed['status'] == 'applied' and 'error' not in completed


def test_invalid_planning_field_types_are_rejected_without_runtime_traceback(tmp_path):
    service, claim, _ = planner(tmp_path)
    with pytest.raises(GreatMindsError, match='title'):
        service.request('create',{'stream':'product','title':['not text']},
                        run_id=claim.run['id'],token=claim.token)
    created = create(service, claim)
    revision = TaskRevision.capture(service.store.runtime, service.store.runtime/created['destination'])
    with pytest.raises(GreatMindsError, match='typed decision'):
        service.request('decide',{'task_id':created['task_id'],'revision':revision.sha256,
                        'steps':[{'decision':[], 'payload':{}}]},run_id=claim.run['id'],token=claim.token)
    assert len(service.store.snapshot()['planning']) == 1
