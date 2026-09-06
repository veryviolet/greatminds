import asyncio
import json

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.daemon import serve
from greatminds.runtime.interactions import ConversationStore
from greatminds.runtime.store import RunStore, TaskRevision
from test_acp_conversations import setup


def task_project(root, scenario='echo', kind='research'):
    setup(root,scenario=scenario,role='DEVELOPER')
    queue=root/'.greatminds/feature_dev';queue.mkdir()
    path=queue/'0001-task.yaml'
    path.write_text(yaml.safe_dump({'id':'0001-task','stream':'product','kind':kind,
        'scope':'backend','reporter':'USER','opened_at':'2026-09-06T10:00:00Z',
        'priority':'normal','title':'Interactive work','blocks':[]}))
    args=['chat','--project-dir',str(root)]
    result=CliRunner().invoke(cli,[*args,'create','chat','--task','0001-task'])
    assert result.exit_code==0,result.output
    store=ConversationStore(root/'.greatminds',json.loads(result.output)['conversation_id'])
    return store,path


def test_task_context_and_result_flow_through_shared_domain_service(tmp_path):
    store,path=task_project(tmp_path,'task-chat')
    store.enqueue('inspect the assigned task',request_id='first')
    async def check():
        daemon=asyncio.create_task(serve(tmp_path,interval=.2,environment={}))
        try:
            async with asyncio.timeout(10):
                while True:
                    state=RunStore(tmp_path/'.greatminds').snapshot()
                    if state['results'] and all(r['status']=='applied' for r in state['results'].values()):
                        return state
                    await asyncio.sleep(.05)
        finally:
            daemon.cancel();await asyncio.gather(daemon,return_exceptions=True)
    state=asyncio.run(check())
    run=next(iter(state['runs'].values()))
    assert run['state']=='completed',run
    assert run['task_id']=='0001-task' and run['conversation_task']
    context=json.loads((tmp_path/'chat-task-context.json').read_text())
    assert context['task']['id']=='0001-task' and context['result_format']
    assert next(iter(state['results'].values()))['status']=='applied',state
    assert store.snapshot()['turns']['first']['status']=='completed'
    assert path.exists()


def test_changed_task_is_held_without_consuming_queued_input(tmp_path):
    store,path=task_project(tmp_path)
    store.enqueue('keep queued',request_id='first')
    path.write_text(path.read_text()+'changed: true\n')
    state=asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert state['runs']=={}
    assert store.snapshot()['turns']['first']['status']=='queued'
    assert 'stale task revision' in store.events()['dispatch']['reason']


def test_attached_conversation_and_background_work_cannot_claim_task_together(tmp_path):
    conversation,path=task_project(tmp_path)
    runtime=tmp_path/'.greatminds'
    store=RunStore(runtime);schema=load_schema_snapshot()
    config=load_execution_config(tmp_path/'coordination/execution.yaml',roles=set(schema.document['roles']))
    binding=config.bindings[0];revision=TaskRevision.capture(runtime,path)
    claim=store.claim(task=revision,binding=binding,config=config,schema=schema,project=tmp_path,owner_id='owner')
    with pytest.raises(GreatMindsError,match='active run'):
        store.claim(task=TaskRevision.conversation(runtime,conversation.id),binding=binding,config=config,
            schema=schema,project=tmp_path,owner_id='owner',conversation_id=conversation.id)
    assert len(store.snapshot()['runs'])==1


def test_permission_wait_with_live_process_cannot_apply_result(tmp_path):
    import os
    from greatminds.domain.results import ResultService
    from greatminds.runtime.processes import process_identity
    from test_domain_results import setup as result_setup
    store,source,claim,envelope=result_setup(tmp_path,finish=False)
    with store._transaction() as state:
        state['runs'][claim.run['id']].update(state='waiting_input',process=process_identity(os.getpid()))
    before=source.read_bytes()
    service=ResultService(store)
    service.reconcile()
    assert service.apply(envelope.result_id)['status']=='received'
    assert source.read_bytes()==before
    assert not (store.directory/'operations').exists()


def test_attached_feature_uses_required_task_worktree(tmp_path):
    import subprocess
    subprocess.run(['git','init','-b','main',str(tmp_path)],check=True,capture_output=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                    'commit','--allow-empty','-m','initial'],cwd=tmp_path,check=True,capture_output=True)
    store,_=task_project(tmp_path,kind='feature')
    store.enqueue('inspect task workspace',request_id='first')
    state=asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    run=next(iter(state['runs'].values()))
    assert run['state']=='completed',run
    assert run['workspace']==str(tmp_path/'.worktrees/0001-task')
    assert run['workspace_identity']['branch']=='task/0001-task'
    assert (tmp_path/'.worktrees/0001-task/agent-starts.log').exists()


def test_connected_task_dialogue_revalidates_before_next_prompt(tmp_path):
    store,path=task_project(tmp_path)
    store.enqueue('first',request_id='first')
    async def check():
        daemon=asyncio.create_task(serve(tmp_path,interval=.2,environment={}))
        try:
            async with asyncio.timeout(10):
                while store.snapshot()['turns']['first']['status']!='completed':
                    await asyncio.sleep(.05)
                path.write_text(path.read_text()+'changed: true\n')
                store.enqueue('second',request_id='second')
                while store.snapshot()['turns']['second']['status'] not in {'failed','completed'}:
                    await asyncio.sleep(.05)
            assert store.snapshot()['turns']['second']['status']=='failed'
            assert not any(e['kind']=='text' and e['turn_id']=='second' for e in store.events()['events'])
            assert (tmp_path/'agent-starts.log').read_text().splitlines()==['echo']
        finally:
            daemon.cancel();await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(check())
