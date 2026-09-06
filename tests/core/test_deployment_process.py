import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import file_lock
from greatminds.domain.stand_deployments import DeploymentLedger
from greatminds.runtime.deployment_process import run_deployment_command
from greatminds.runtime.processes import group_members


def test_external_command_sees_its_persisted_identity(tmp_path):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    script = "import json,os,sys; d=json.load(open(sys.argv[1])); a=d['attempts'][sys.argv[2]]; assert a['process']['pid']==os.getpid(); print('gate passed')"
    result = run_deployment_command([sys.executable, '-c', script, str(ledger.path), attempt], ledger=ledger, attempt_id=attempt)
    assert result.returncode == 0 and result.stdout == 'gate passed\n'
    record = ledger.snapshot()['attempts'][attempt]
    assert record['process_status'] == 'exited'
    assert not group_members(record['process'])
    assert record['argv_sha256']


def test_failed_identity_persistence_never_releases_exec_gate(tmp_path, monkeypatch):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    effect = tmp_path / 'effect'
    def fail(*args, **kwargs):
        raise OSError('persistence failure')
    monkeypatch.setattr(ledger, 'attach_process', fail)
    with pytest.raises(OSError, match='persistence failure'):
        run_deployment_command([sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(effect)], ledger=ledger, attempt_id=attempt)
    assert not effect.exists()


def test_timeout_cleans_descendants_and_retains_uncertain_attempt(tmp_path):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print('started',flush=True);time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        run_deployment_command([sys.executable, '-c', script], ledger=ledger, attempt_id=attempt, timeout=.3)
    record = ledger.snapshot()['attempts'][attempt]
    assert record['process_status'] == 'exited'
    assert not group_members(record['process'])
    with pytest.raises(GreatMindsError, match='refusing external replay'):
        ledger.require_resolved()


def _orphan_owner(root):
    ledger = DeploymentLedger(root)
    with file_lock(root / '.stand/deployment.lock', label='test'):
        attempt = ledger.begin({'lease_id': 'L1'})
        run_deployment_command([sys.executable, '-c', "import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(60)", str(root/'effect')], ledger=ledger, attempt_id=attempt)


def test_owner_death_recovers_child_without_replay_then_requires_operator_resolution(tmp_path):
    owner = multiprocessing.get_context('fork').Process(target=_orphan_owner, args=(tmp_path,))
    owner.start()
    ledger = DeploymentLedger(tmp_path)
    attempt = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path/'effect').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path/'effect').exists()
        attempt = next(iter(ledger.snapshot()['attempts']))
        with pytest.raises(GreatMindsError, match='stand deployment is being transitioned'):
            ledger.recover_process(attempt)
        owner.kill()
        owner.join(timeout=5)
        record = ledger.snapshot()['attempts'][attempt]
        assert group_members(record['process'])
        with pytest.raises(GreatMindsError, match='confirmed process cleanup'):
            ledger.resolve(attempt, reason='cannot resolve yet')
        recovered = ledger.recover_process(attempt)
        assert recovered['cleanup'] == 'confirmed'
        assert not group_members(record['process'])
        with pytest.raises(GreatMindsError, match='refusing external replay'):
            ledger.require_resolved()
        resolved = ledger.resolve(attempt, reason='Synthetic target inspected; partial changes cleared')
        assert resolved['status'] == 'resolved' and 'exit_code' not in resolved
        ledger.require_resolved()
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(timeout=5)
        if attempt and ledger.snapshot()['attempts'][attempt]['status'] not in {'applied','resolved'}:
            ledger.recover_process(attempt)


def test_untracked_older_attempt_cannot_claim_cleanup(tmp_path):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    doc = ledger.snapshot()
    del doc['attempts'][attempt]['launch_protocol']
    ledger.path.write_text(json.dumps(doc))
    with pytest.raises(GreatMindsError, match='no gated child tracking'):
        ledger.recover_process(attempt)


def test_sweep_skips_live_deployment_and_deduplicates_orphan_cleanup(tmp_path):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    original = ledger.path.read_bytes()
    with file_lock(tmp_path/'.stand/deployment.lock', label='live deployment'):
        ledger.reconcile()
    assert ledger.path.read_bytes() == original
    ledger.reconcile()
    record = ledger.snapshot()['attempts'][attempt]
    assert record['status'] == 'needs_recovery' and record['cleanup'] == 'confirmed'
    first = ledger.path.read_bytes()
    ledger.reconcile()
    assert ledger.path.read_bytes() == first
    with pytest.raises(GreatMindsError):
        ledger.require_resolved()


def test_empty_sweep_does_not_create_stand_state(tmp_path):
    DeploymentLedger(tmp_path).reconcile()
    assert not (tmp_path/'.stand').exists()


def test_owner_death_after_identity_commit_does_not_authorize_exec(tmp_path):
    def owner_before_release():
        ledger = DeploymentLedger(tmp_path)
        attempt = ledger.begin({'lease_id': 'L1'})
        attach = ledger.attach_process
        def save_then_die(*args, **kwargs):
            attach(*args, **kwargs)
            os._exit(74)
        ledger.attach_process = save_then_die
        run_deployment_command([sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(tmp_path/'effect')], ledger=ledger, attempt_id=attempt)
    owner = multiprocessing.get_context('fork').Process(target=owner_before_release)
    owner.start()
    owner.join(timeout=10)
    if owner.is_alive():
        owner.kill()
        owner.join(timeout=5)
        pytest.fail('fault owner failed to terminate')
    assert owner.exitcode == 74
    ledger = DeploymentLedger(tmp_path)
    attempt = next(iter(ledger.snapshot()['attempts']))
    assert ledger.snapshot()['attempts'][attempt]['process']
    ledger.recover_process(attempt)
    assert not (tmp_path/'effect').exists()
