import asyncio
import json
from pathlib import Path
import sys

import pytest
import yaml

from greatminds.cli import stand_state as ss
from greatminds.runtime.config import StandDeploymentPolicy, parse_execution_config
from greatminds.runtime.daemon import serve
from greatminds.runtime.stand_scheduler import StandScheduler
from greatminds.runtime.store import RunStore
from greatminds.core.errors import GreatMindsError


def setup(root, monkeypatch, *, authorized=True, script="print('PLAY RECAP\\nsynthetic : ok=1')", approval_required=False):
    coord = root/'coordination'
    coord.mkdir()
    config = {'version':1,'agents':{'fixture':{'transport':'acp','argv':[sys.executable,'-c','raise Exception("must not launch")'],'adapter_version':'fixture','harness_version':'fixture'}},
              'bindings':{},'stand':{'authorized':authorized,'profiles':['smoke'],'timeout_seconds':10}}
    (coord/'execution.yaml').write_text(yaml.safe_dump(config))
    (coord/'stand-profiles.yaml').write_text(yaml.safe_dump({'profiles':{'smoke':{'file':'smoke.yaml','purpose':'Synthetic local profile','used_for':['deploy_only'],'requires_explicit_user_approval':approval_required}}}))
    profiles = coord/'stand-profiles'
    profiles.mkdir()
    (profiles/'smoke.yaml').write_text(yaml.safe_dump({'name':'smoke','hosts':'synthetic','tasks':[{'name':'fixture','ansible.builtin.debug':{'msg':'fixture'}}]}))
    executable = root/'fake-ansible'
    executable.write_text('#!'+sys.executable+'\n'+script+'\n')
    executable.chmod(0o700)
    monkeypatch.setattr('greatminds.cli.stand_executor._ansible_playbook_path',lambda:str(executable))
    monkeypatch.setattr('greatminds.cli.stand._file_inbox_info',lambda *a,**k:None)
    runtime = root/'.greatminds'
    ss.update_stand_state(runtime,lambda s:s.update(state='preparing',active_lease={
        'lease_id':'L1','task':'0001-work','holder_role':'TESTER','worktree':str(root),
        'profile':'smoke','ttl_seconds':14400,'granted_at':ss.now_iso()}))
    return runtime


def test_authorized_idle_daemon_deploys_without_agent_and_does_not_replay(tmp_path, monkeypatch):
    runtime = setup(tmp_path,monkeypatch)
    snapshot = asyncio.run(serve(tmp_path,once=True,environment={}))
    assert snapshot['runs']=={}
    state=ss.read_stand_state(runtime)
    assert state['state']=='ready'
    ledger=json.loads((runtime/'.stand/deployments.json').read_text())
    assert len(ledger['attempts'])==1
    assert next(iter(ledger['attempts'].values()))['status']=='applied'
    before=(runtime/'.stand/schedule.json').read_bytes()
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert (runtime/'.stand/schedule.json').read_bytes()==before
    assert len(json.loads((runtime/'.stand/deployments.json').read_text())['attempts'])==1


@pytest.mark.parametrize('blocked',['disabled','paused','profile'])
def test_dispatch_requires_explicit_policy_and_unpaused_state(tmp_path,monkeypatch,blocked):
    runtime=setup(tmp_path,monkeypatch,authorized=blocked!='disabled')
    if blocked=='paused': RunStore(runtime).set_paused(True)
    if blocked=='profile': ss.update_stand_state(runtime,lambda s:s['active_lease'].update(profile='other'))
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert ss.read_stand_state(runtime)['state']=='preparing'
    assert not (runtime/'.stand/deployments.json').exists()


def test_profile_user_approval_cannot_be_replaced_by_generic_authorization(tmp_path,monkeypatch):
    runtime=setup(tmp_path,monkeypatch,approval_required=True)
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert ss.read_stand_state(runtime)['state']=='preparing'
    assert not (runtime/'.stand/deployments.json').exists()
    schedule=(runtime/'.stand/schedule.json').read_bytes()
    assert next(iter(json.loads(schedule)['tickets'].values()))['status']=='failed'
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert (runtime/'.stand/schedule.json').read_bytes()==schedule


def test_shutdown_cancels_deployment_worker_without_blocking_event_loop(tmp_path,monkeypatch):
    runtime=setup(tmp_path,monkeypatch,script="import pathlib,time;pathlib.Path('started').touch();time.sleep(60)")
    async def exercise():
        daemon=asyncio.create_task(serve(tmp_path,once=True,environment={},interval=.2))
        try:
            for _ in range(100):
                if (runtime/'started').exists(): break
                await asyncio.sleep(.05)
            assert (runtime/'started').exists()
            daemon.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(daemon,timeout=8)
        finally:
            if not daemon.done():
                daemon.cancel()
                await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(exercise())
    from greatminds.runtime.processes import group_members
    attempt=next(iter(json.loads((runtime/'.stand/deployments.json').read_text())['attempts'].values()))
    assert attempt['status']=='needs_recovery'
    assert not group_members(attempt['process'])


@pytest.mark.parametrize('stand',[{'authorized':'yes'}, {'authorized':True}, {'profiles':['x','x']}, {'profiles':['x'],'max_output_bytes':67108865}, {'timeout_seconds':0}, {'extra':'unknown'}])
def test_stand_policy_is_strict(stand):
    with pytest.raises(GreatMindsError):
        parse_execution_config({'version':1,'agents':{},'bindings':{},'stand':stand},roles=set())


def test_lease_change_after_selection_cannot_deploy_unapproved_profile(tmp_path,monkeypatch):
    from greatminds.cli import stand
    runtime=setup(tmp_path,monkeypatch)
    execute=stand._deploy_lease_locked
    def change_then_execute(*args,**kwargs):
        ss.update_stand_state(runtime,lambda s:s['active_lease'].update(profile='unapproved'))
        return execute(*args,**kwargs)
    monkeypatch.setattr(stand,'_deploy_lease_locked',change_then_execute)
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert not (runtime/'.stand/deployments.json').exists()
    assert ss.read_stand_state(runtime)['active_lease']['profile']=='unapproved'


def test_lost_ack_after_selected_ticket_does_not_replay_on_restart(tmp_path,monkeypatch):
    import greatminds.runtime.stand_scheduler as module
    runtime=setup(tmp_path,monkeypatch)
    scheduler=StandScheduler(RunStore(runtime),StandDeploymentPolicy(('smoke',),True))
    atomic=module.atomic_json
    def fail_after_commit(path,value):
        atomic(path,value)
        raise OSError('simulated lost acknowledgement')
    monkeypatch.setattr(module,'atomic_json',fail_after_commit)
    with pytest.raises(OSError):
        scheduler._execute()
    assert not (runtime/'.stand/deployments.json').exists()
    replacement=StandScheduler(RunStore(runtime),StandDeploymentPolicy(('smoke',),True))
    assert replacement.inspect()['status']=='already_attempted'
    assert replacement.inspect()['outcome']=='selected'


@pytest.mark.parametrize('case',['approved','role_mismatch','file_mismatch'])
def test_registry_policy_is_revalidated_for_each_automatic_deployment(tmp_path,monkeypatch,case):
    runtime=setup(tmp_path,monkeypatch,approval_required=case=='approved')
    if case=='approved':
        ss.update_stand_state(runtime,lambda s:s['active_lease'].update(profile_approval='explicit-user-approval'))
    elif case=='role_mismatch':
        path=tmp_path/'coordination/stand-profiles.yaml'
        registry=yaml.safe_load(path.read_text())
        registry['profiles']['smoke']['allowed_roles']=['EXPLORER']
        path.write_text(yaml.safe_dump(registry))
    else:
        ss.update_stand_state(runtime,lambda s:s['active_lease'].update(profile_file='other.yaml'))
    asyncio.run(serve(tmp_path,once=True,environment={}))
    if case=='approved':
        assert ss.read_stand_state(runtime)['state']=='ready'
    else:
        assert ss.read_stand_state(runtime)['state']=='preparing'
        assert not (runtime/'.stand/deployments.json').exists()


def test_nonzero_profile_result_is_reported_as_failed_without_replay(tmp_path,monkeypatch):
    runtime=setup(tmp_path,monkeypatch,script='import sys;sys.exit(2)')
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert ss.read_stand_state(runtime)['state']=='down'
    ticket=next(iter(json.loads((runtime/'.stand/schedule.json').read_text())['tickets'].values()))
    assert ticket['status']=='failed' and ticket['exit_code']==2
    before=(runtime/'.stand/deployments.json').read_bytes()
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert (runtime/'.stand/deployments.json').read_bytes()==before


@pytest.mark.parametrize('changed',['source','profile','environment'])
def test_changed_deployment_inputs_cannot_mark_stand_ready(tmp_path,monkeypatch,changed):
    target={'source':'../source.py','profile':'../coordination/stand-profiles/smoke.yaml','environment':'PROJECT.env'}[changed]
    script=f"import pathlib;pathlib.Path({target!r}).write_text('SECRET=changed-value');print('PLAY RECAP\\nsynthetic : ok=1')"
    runtime=setup(tmp_path,monkeypatch,script=script)
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert ss.read_stand_state(runtime)['state']=='preparing'
    ledger=(runtime/'.stand/deployments.json').read_text()
    attempt=next(iter(json.loads(ledger)['attempts'].values()))
    assert attempt['process_returncode']==0
    assert attempt['status']=='needs_recovery' and attempt['inputs_match'] is False
    assert attempt['inputs_before']!=attempt['inputs_after']
    if changed=='environment':
        assert attempt['inputs_before']['source']==attempt['inputs_after']['source']
        assert attempt['inputs_before']['environment']!=attempt['inputs_after']['environment']
    assert 'changed-value' not in ledger


@pytest.mark.parametrize('change',['none','source','environment','revision','output','output_symlink','wrong_task','unresolved'])
def test_deployment_receipt_is_revalidated_when_consumed(tmp_path,monkeypatch,change):
    from greatminds.domain.stand_evidence import require_fresh_deployment
    from greatminds.domain.stand_deployments import DeploymentLedger
    runtime=setup(tmp_path,monkeypatch)
    asyncio.run(serve(tmp_path,once=True,environment={}))
    assert require_fresh_deployment(runtime,'L1',task_id='0001-work')['inputs_match']
    if change=='none':
        monkeypatch.setenv('GREATMINDS_ROLE','TESTER')
        monkeypatch.setenv('GREATMINDS_RUN_TOKEN','ephemeral-run-token')
        assert require_fresh_deployment(runtime,'L1',task_id='0001-work')['inputs_match']
        return
    if change=='source': (tmp_path/'source.py').write_text('changed')
    if change=='environment': (runtime/'PROJECT.env').write_text('SECRET=changed')
    if change=='revision':
        path=tmp_path/'coordination/execution.yaml'
        config=yaml.safe_load(path.read_text());config['stand']['environment_revision']='2'
        path.write_text(yaml.safe_dump(config))
    if change=='output':
        attempt=next(iter(DeploymentLedger(runtime).snapshot()['attempts'].values()))
        Path(attempt['output']['stdout']['path']).write_text('tampered')
    if change=='output_symlink':
        attempt=next(iter(DeploymentLedger(runtime).snapshot()['attempts'].values()))
        path=Path(attempt['output']['stdout']['path'])
        replacement=path.with_name('replacement');path.rename(replacement)
        path.symlink_to(replacement)
    if change=='unresolved': DeploymentLedger(runtime).begin({'lease_id':'L1','task':'0001-work'})
    with pytest.raises(GreatMindsError):
        require_fresh_deployment(runtime,'L1',task_id='different-task' if change=='wrong_task' else '0001-work')


def test_domain_gate_and_manual_ready_reject_stale_managed_deployment(tmp_path,monkeypatch):
    from greatminds.cli import task,gate_check,stand
    from click.testing import CliRunner
    runtime=setup(tmp_path,monkeypatch)
    asyncio.run(serve(tmp_path,once=True,environment={}))
    data={'id':'0001-work','blocks':[{'kind':'plan','stand_required':True,'base_commit':'fixture'},
        {'kind':'tests','test_result':'pass','stand_evidence':{'lease_id':'L1','commit':'fixture'}}]}
    queue=runtime/'feature_test';queue.mkdir()
    (queue/'0001-work.yaml').write_text(yaml.safe_dump(data))
    monkeypatch.setattr(task,'find_coord_dir',lambda:runtime)
    assert task._evaluate_gate_check(data)=='pass'
    runner=CliRunner()
    result=runner.invoke(gate_check.gate_check,['0001-work','--project-dir',str(tmp_path)])
    assert result.exit_code==0,result.output
    (tmp_path/'source.py').write_text('changed after deployment')
    assert task._evaluate_gate_check(data)=='fail'
    result=runner.invoke(gate_check.gate_check,['0001-work','--project-dir',str(tmp_path)])
    assert result.exit_code==1,result.output
    ss.update_stand_state(runtime,lambda s:s.update(state='preparing'))
    monkeypatch.setattr(stand,'find_coord_dir',lambda:runtime)
    result=runner.invoke(stand.stand,['ready','--lease-id','L1'])
    assert result.exit_code!=0 and 'stale' in result.output
    assert ss.read_stand_state(runtime)['state']=='preparing'


@pytest.mark.parametrize('changed_field',[None,'profile','holder_role'])
def test_manual_ready_requires_same_deployed_lease(tmp_path,monkeypatch,changed_field):
    from greatminds.cli import stand
    from click.testing import CliRunner
    runtime=setup(tmp_path,monkeypatch)
    asyncio.run(serve(tmp_path,once=True,environment={}))
    def prepare(state):
        state['state']='preparing'
        if changed_field:
            state['active_lease'][changed_field]='replacement'
    ss.update_stand_state(runtime,prepare)
    monkeypatch.setattr(stand,'find_coord_dir',lambda:runtime)
    result=CliRunner().invoke(stand.stand,['ready','--lease-id','L1'])
    if changed_field:
        assert result.exit_code!=0 and 'lease changed' in result.output
        assert ss.read_stand_state(runtime)['state']=='preparing'
    else:
        assert result.exit_code==0,result.output
        assert ss.read_stand_state(runtime)['state']=='ready'
