from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from greatminds.cli import stand_state as ss
from greatminds.core.storage import file_lock
from greatminds.domain.stand_deployments import DeploymentLedger
from greatminds.domain.stand_leases import StandLeaseService, holder_hold
from greatminds.runtime.store import RunStore


NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


def setup(tmp_path):
    store = RunStore(tmp_path)
    lease = {'lease_id': 'expired', 'task': '0001-work', 'holder_role': 'TESTER',
             'ttl_seconds': 60, 'granted_at': (NOW-timedelta(hours=1)).isoformat()}
    ss.update_stand_state(tmp_path, lambda s:s.update(state='ready', active_lease=lease))
    return store, StandLeaseService(store, clock=lambda: NOW)


def test_expired_dead_holder_is_reclaimed_once_and_fifo_promoted(tmp_path):
    store, service = setup(tmp_path)
    ss.update_stand_state(tmp_path, lambda s:s['queue'].append({'lease_id':'next','task':'0002-work','holder_role':'TESTER','ttl_seconds':60}))
    service.reconcile()
    state = ss.read_stand_state(tmp_path)
    assert state['state'] == 'preparing' and state['active_lease']['lease_id'] == 'next'
    assert state['history'][0]['by'] == 'SYSTEM'
    assert state['history'][0]['lease_id'] == 'expired'
    before = ss.state_file_path(tmp_path).read_bytes()
    service.reconcile()
    assert ss.state_file_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize('ttl', [0, -1, True, float('nan'), float('inf'), '60'])
def test_invalid_expiry_is_never_reclaimed(tmp_path, ttl):
    _, service = setup(tmp_path)
    ss.update_stand_state(tmp_path, lambda s:s['active_lease'].update(ttl_seconds=ttl))
    assert service.inspect()['status'] == 'invalid_expiry'
    service.reconcile()
    assert ss.read_stand_state(tmp_path)['active_lease']['lease_id'] == 'expired'


def test_live_or_unreadable_legacy_holder_prevents_reclaim(tmp_path):
    _, service = setup(tmp_path)
    registry = tmp_path/'.agent_registry/tester.json'
    registry.parent.mkdir()
    registry.write_text(json.dumps({'pid':os.getpid()}))
    assert service.inspect()['status'] == 'legacy_holder_alive'
    registry.write_text('{')
    assert service.inspect()['status'] == 'legacy_holder_unknown'
    service.reconcile()
    assert ss.read_stand_state(tmp_path)['active_lease']


def test_unresolved_deployment_and_active_lock_prevent_reclaim(tmp_path):
    _, service = setup(tmp_path)
    with file_lock(tmp_path/'.stand/deployment.lock', label='active deployment'):
        service.reconcile()
    assert ss.read_stand_state(tmp_path)['active_lease']
    DeploymentLedger(tmp_path).begin({'lease_id':'older'})
    assert service.inspect()['status'] == 'deployment_unresolved'
    service.reconcile()
    assert ss.read_stand_state(tmp_path)['active_lease']


@pytest.mark.parametrize('status', ['running','waiting_input','waiting_auth','claimed'])
def test_nonterminal_acp_holder_prevents_expiry_even_without_registry(tmp_path, status):
    store, _ = setup(tmp_path)
    snapshot = store.snapshot()
    snapshot['runs']['r'] = {'role':'TESTER','task_id':'another-task','state':status}
    assert holder_hold(tmp_path, ss.read_stand_state(tmp_path)['active_lease'], snapshot) == 'active_run'


def test_no_stand_creates_no_stand_state(tmp_path):
    StandLeaseService(RunStore(tmp_path)).reconcile()
    assert not (tmp_path/'.stand').exists()


@pytest.mark.parametrize('kind', ['run','command','result'])
def test_durable_unresolved_work_blocks_both_daemon_and_manual_reclaim(tmp_path, monkeypatch, kind):
    from click.testing import CliRunner
    from greatminds.cli import stand
    store, service = setup(tmp_path)
    with store._transaction() as state:
        if kind == 'run':
            state['runs']['r'] = {'role':'TESTER','task_id':'0001-work','state':'waiting_input'}
        elif kind == 'command':
            state['commands'] = {'c':{'task_id':'0001-work','status':'needs_recovery'}}
        else:
            state['results']['r'] = {'envelope':{'task_id':'0001-work'},'status':'needs_recovery'}
    service.reconcile()
    assert ss.read_stand_state(tmp_path)['active_lease']
    monkeypatch.setattr(stand, 'find_coord_dir', lambda:tmp_path)
    result = CliRunner().invoke(stand.stand, ['reclaim'], env={'GREATMINDS_ROLE':'MAINTAINER'})
    assert result.exit_code != 0
    assert 'blocked' in result.output
    assert ss.read_stand_state(tmp_path)['active_lease']


def test_new_claim_after_initial_inspection_is_rechecked_before_reclaim(tmp_path, monkeypatch):
    store, service = setup(tmp_path)
    inspect = service.inspect
    calls = []
    def claim_after_inspection(*args, **kwargs):
        result = inspect(*args, **kwargs)
        if not calls:
            calls.append(1)
            with store._transaction() as snapshot:
                snapshot['runs']['new'] = {'role':'TESTER','task_id':'0001-work','state':'claimed'}
        return result
    monkeypatch.setattr(service, 'inspect', claim_after_inspection)
    service.reconcile()
    assert ss.read_stand_state(tmp_path)['active_lease']['lease_id'] == 'expired'
