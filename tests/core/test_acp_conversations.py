import asyncio
from pathlib import Path
import sys

import pytest
import yaml

from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.daemon import serve
from greatminds.runtime.interactions import ConversationStore

SERVER = Path(__file__).resolve().parents[1] / 'fixtures/acp_server.py'


def setup(root, scenario='echo', role='ARCHITECT-PLANNER', **limits):
    (root/'coordination').mkdir()
    path=root/'coordination/execution.yaml'
    path.write_text(yaml.safe_dump({'version':1,'agents':{'fixture':{
        'transport':'acp','argv':[sys.executable,str(SERVER),scenario],
        'adapter_version':'fixture','harness_version':'fixture'}},
        'bindings':{'chat':{'agent':'fixture','role':role,'timeout_seconds':5,**limits}}}))
    schema=load_schema_snapshot()
    config=load_execution_config(path,roles=set(schema.document['roles']))
    store=ConversationStore.create(root/'.greatminds',binding=config.bindings[0],
        config_sha256=config.sha256,schema_sha256=schema.sha256,workspace=root)
    return store


@pytest.mark.parametrize('role',['ARCHITECT-PLANNER','DEVELOPER','TESTER','ARCHITECT-REVIEWER'])
def test_multiple_user_turns_use_one_daemon_owned_session_without_task(tmp_path, role):
    store=setup(tmp_path,role=role)
    store.enqueue('first user prompt',request_id='z')
    store.enqueue('second user prompt',request_id='a')
    state=asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert len(state['runs'])==1,state
    run=next(iter(state['runs'].values()))
    assert run['state']=='completed',run
    assert run['conversation_id']==store.id
    assert state['results']=={}
    journal=store.snapshot()
    assert [t['status'] for t in journal['turns'].values()]==['completed','completed'],journal
    assert [e['turn_id'] for e in journal['events'] if e['kind']=='started']==['z','a']
    assert any(e['kind']=='text' for e in journal['events'])
    assert (tmp_path/'agent-starts.log').read_text().splitlines()==['echo']
    restarted=asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert len(restarted['runs'])==1


def test_later_message_loads_saved_session_and_does_not_duplicate_history(tmp_path):
    store=setup(tmp_path,scenario='resume')
    store.enqueue('first',request_id='first')
    asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    store.enqueue('second',request_id='second')
    state=asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert all(r['state']=='completed' for r in state['runs'].values()),state
    assert (tmp_path/'loads.log').read_text().splitlines()==['test-session']
    assert 'historical reply' not in str(store.events())


def test_missing_load_capability_does_not_silently_start_new_conversation(tmp_path):
    store=setup(tmp_path)
    store.enqueue('first',request_id='first')
    asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    store.enqueue('second',request_id='second')
    asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert store.snapshot()['turns']['second']['status']=='failed'


def test_live_conversation_permission_uses_the_shared_operator_broker(tmp_path):
    from greatminds.runtime.store import RunStore
    from greatminds.runtime.permissions import PermissionService
    store=setup(tmp_path,scenario='permission-execute')
    store.enqueue('request protected operation',request_id='first')
    runs=RunStore(tmp_path/'.greatminds')
    async def check():
        daemon=asyncio.create_task(serve(tmp_path,once=True,interval=.2,environment={}))
        try:
            async with asyncio.timeout(5):
                while not runs.snapshot().get('permissions'):
                    await asyncio.sleep(.05)
                request=next(iter(runs.snapshot()['permissions'].values()))
                PermissionService(runs).answer(request['id'],'no')
                await daemon
            assert store.snapshot()['turns']['first']['status']=='completed'
            assert PermissionService(runs).get(request['id'])['status']=='consumed'
        finally:
            if not daemon.done():
                daemon.cancel()
            await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(check())


def test_interrupt_reaches_active_transport_and_finishes_turn(tmp_path):
    store=setup(tmp_path,scenario='cancel-output')
    store.enqueue('long work',request_id='first')
    async def check():
        daemon=asyncio.create_task(serve(tmp_path,once=True,interval=.2,environment={}))
        try:
            async with asyncio.timeout(8):
                while not any(e['kind']=='text' for e in store.events()['events']):
                    await asyncio.sleep(.05)
                store.cancel('first')
                state=await daemon
            assert store.snapshot()['turns']['first']['status']=='cancelled'
            assert next(iter(state['runs'].values()))['state']=='cancelled'
        finally:
            if not daemon.done():
                daemon.cancel()
            await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(check())


def test_chat_cli_create_send_attach_is_idempotent_and_read_only(tmp_path):
    import json
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    setup(tmp_path)
    runner=CliRunner()
    args=['chat','--project-dir',str(tmp_path)]
    result=runner.invoke(cli,[*args,'create','chat'])
    assert result.exit_code==0,result.output
    cid=json.loads(result.output)['conversation_id']
    for _ in range(2):
        result=runner.invoke(cli,[*args,'send',cid,'--request-id','request','--message','hello'])
        assert result.exit_code==0,result.output
    store=ConversationStore(tmp_path/'.greatminds',cid)
    assert len(store.snapshot()['turns'])==1
    asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    before=store.path.read_bytes()
    result=runner.invoke(cli,[*args,'attach',cid])
    assert result.exit_code==0,result.output
    page=json.loads(result.output)
    assert any(e['kind']=='text' for e in page['events'])
    result=runner.invoke(cli,[*args,'attach',cid,'--after',str(page['cursor'])])
    assert json.loads(result.output)['events']==[]
    assert store.path.read_bytes()==before
    rejected=runner.invoke(cli,[*args,'send',cid,'--message','self approval'],env={'GREATMINDS_RUN_ID':'agent'})
    assert rejected.exit_code!=0


def test_connected_daemon_accepts_later_input_in_same_live_session(tmp_path):
    store=setup(tmp_path)
    store.enqueue('first',request_id='first')
    async def check():
        daemon=asyncio.create_task(serve(tmp_path,interval=.2,environment={}))
        try:
            async with asyncio.timeout(8):
                while store.snapshot()['turns']['first']['status']!='completed':
                    await asyncio.sleep(.05)
                cursor=store.events()['cursor']
                store.enqueue('later input after initial turn',request_id='second')
                while store.snapshot()['turns']['second']['status']!='completed':
                    await asyncio.sleep(.05)
                assert any(e['kind']=='text' for e in store.events(after=cursor)['events'])
                assert (tmp_path/'agent-starts.log').read_text().splitlines()==['echo']
        finally:
            daemon.cancel()
            await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(check())


def test_changed_contract_retains_input_and_explains_dispatch_hold(tmp_path):
    store=setup(tmp_path)
    store.enqueue('keep this message',request_id='first')
    path=tmp_path/'coordination/execution.yaml'
    config=yaml.safe_load(path.read_text());config['max_running']=2
    path.write_text(yaml.safe_dump(config))
    state=asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert state['runs']=={}
    assert store.snapshot()['turns']['first']['status']=='queued'
    assert 'contract changed' in store.events()['dispatch']['reason']


def test_conversation_shares_background_capacity_and_cannot_submit_task_result(tmp_path):
    from dataclasses import replace
    from greatminds.core.errors import GreatMindsError
    from greatminds.runtime.store import RunStore, TaskRevision, ResultEnvelope
    setup(tmp_path,role='DEVELOPER')
    schema=load_schema_snapshot()
    config=replace(load_execution_config(tmp_path/'coordination/execution.yaml',roles=set(schema.document['roles'])),max_running=1)
    binding=config.bindings[0]
    conversation=ConversationStore.create(tmp_path/'.greatminds',binding=binding,
        config_sha256=config.sha256,schema_sha256=schema.sha256,workspace=tmp_path)
    store=RunStore(tmp_path/'.greatminds')
    claim=store.claim(task=TaskRevision.conversation(store.runtime,conversation.id),binding=binding,
        config=config,schema=schema,project=tmp_path,owner_id='daemon',conversation_id=conversation.id)
    queue=store.runtime/'feature_dev';queue.mkdir()
    path=queue/'0001-work.yaml';path.write_text('id: 0001-work\n')
    with pytest.raises(GreatMindsError,match='capacity'):
        store.claim(task=TaskRevision.capture(store.runtime,path),binding=binding,
            config=config,schema=schema,project=tmp_path,owner_id='daemon')
    with pytest.raises(GreatMindsError,match='no assigned task'):
        store.receive_result(ResultEnvelope('result',claim.run['id'],claim.run['task_id'],
            claim.run['task_revision'],schema.sha256,'no_change',{}),token=claim.token)
    assert store.snapshot()['results']=={}


@pytest.mark.parametrize('phase',['queued','active','idle'])
def test_close_stops_admission_and_releases_daemon_session(tmp_path,phase):
    from greatminds.core.errors import GreatMindsError
    from greatminds.runtime.store import RunStore, TERMINAL
    store=setup(tmp_path,scenario='cancel-output' if phase=='active' else 'echo')
    store.enqueue('first',request_id='first')
    async def check():
        daemon=None
        try:
            if phase!='queued':
                daemon=asyncio.create_task(serve(tmp_path,interval=.2,environment={}))
                async with asyncio.timeout(8):
                    while True:
                        journal=store.snapshot()
                        if phase=='idle' and journal['turns']['first']['status']=='completed':
                            break
                        if phase=='active' and any(e['kind']=='text' for e in journal['events']):
                            break
                        await asyncio.sleep(.05)
            store.request_close()
            store.request_close()
            with pytest.raises(GreatMindsError,match='closed'):
                store.enqueue('cannot race shutdown',request_id='new')
            if daemon is None:
                await serve(tmp_path,once=True,interval=.2,environment={})
            else:
                async with asyncio.timeout(8):
                    while not store.snapshot()['closed']:
                        await asyncio.sleep(.05)
            assert store.snapshot()['closed']
            assert sum(e['kind']=='closed' for e in store.events()['events'])==1
            runs=RunStore(tmp_path/'.greatminds').snapshot()['runs']
            assert all(run['state'] in TERMINAL for run in runs.values())
            from greatminds.runtime.processes import group_members
            assert all(not group_members(run['process']) for run in runs.values() if run.get('process'))
            if phase=='queued':
                assert not runs
                assert store.snapshot()['turns']['first']['status']=='cancelled'
        finally:
            if daemon is not None:
                daemon.cancel()
                await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(check())
    before=store.path.read_bytes()
    asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    assert store.path.read_bytes()==before


def test_close_works_after_contract_change_and_attach_follow_ends(tmp_path):
    import json
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    store=setup(tmp_path)
    store.enqueue('queued',request_id='first')
    path=tmp_path/'coordination/execution.yaml'
    config=yaml.safe_load(path.read_text());config['bindings']={}
    path.write_text(yaml.safe_dump(config))
    args=['chat','--project-dir',str(tmp_path)]
    runner=CliRunner()
    result=runner.invoke(cli,[*args,'close',store.id])
    assert result.exit_code==0,result.output
    assert json.loads(result.output)['close_requested']
    asyncio.run(serve(tmp_path,once=True,interval=.2,environment={}))
    result=runner.invoke(cli,[*args,'attach',store.id,'--follow'])
    assert result.exit_code==0,result.output
    assert json.loads(result.output)['closed']
