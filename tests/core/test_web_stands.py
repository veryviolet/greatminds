import asyncio
from pathlib import Path
import sys

import pytest
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.service_environment import read_environment
from greatminds.runtime.stand_operations import StandOperations
from greatminds.web.service import WebService
from greatminds.web.stands import Stands


@pytest.fixture
def stands(tmp_path):
    (tmp_path / '.greatminds').mkdir()
    (tmp_path / 'coordination').mkdir()
    (tmp_path / '.greatminds/PROJECT.env').write_text('STAND_HOST=localhost\nPRIVATE_TOKEN=secret-value\n')
    return Stands(WebService(tmp_path))


def test_connections_hide_secrets_preserve_values_and_reject_stale_save(stands):
    before = stands.snapshot()
    assert before['connections'] == {'STAND_HOST': 'localhost'}
    assert 'secret-value' not in str(before)
    stands.save_connections({'connections': {'STAND_HOST_GPU': 'gpu-box', 'STAND_USER_GPU': 'tester'}, 'revision': before['connection_revision']})
    assert read_environment(stands.runtime / 'PROJECT.env')['PRIVATE_TOKEN'] == 'secret-value'
    with pytest.raises(GreatMindsError, match='changed'):
        stands.save_connections({'connections': {}, 'revision': before['connection_revision']})


def test_request_is_durable_idempotent_and_executed_by_daemon_worker(stands):
    body = {'request_id': 'check-one', 'action': 'check', 'machine_key': 'STAND_HOST'}
    queued = stands.operations.submit(body)
    assert stands.operations.submit(body) == queued
    with pytest.raises(GreatMindsError, match='another operation'):
        stands.operations.submit({**body, 'machine_key': 'STAND_HOST_GPU'})
    # A new service/worker consumes the request; there is no web process owner.
    worker = StandOperations(stands.project, stands.runtime)
    async def check():
        await worker.poll()
        await asyncio.wait_for(worker.future, 5)
        await worker.close()
    asyncio.run(check())
    receipt = worker.snapshot()['operations']['check-one']
    assert receipt['status'] == 'completed', receipt
    assert 'local execution available' in receipt['log']


def test_interrupted_operation_is_never_automatically_replayed(stands):
    stands.operations.submit({'request_id': 'lost', 'action': 'doctor'})
    stands.operations.update('lost', status='running')
    worker = StandOperations(stands.project, stands.runtime)
    asyncio.run(worker.poll())
    assert worker.future is None
    assert worker.snapshot()['operations']['lost']['status'] == 'needs_review'


def test_stand_files_cannot_read_arbitrary_paths(stands):
    with pytest.raises(GreatMindsError, match='Unknown'):
        stands.file('../outside.env')
    with pytest.raises(GreatMindsError, match='Unknown'):
        stands.file('.greatminds/PROJECT.env')


def test_deployment_preview_verifies_artifact_and_redacts_secrets(stands):
    import hashlib
    from greatminds.core.storage import atomic_json
    from greatminds.domain.stand_deployments import DeploymentLedger
    ledger = DeploymentLedger(stands.runtime)
    identity = ledger.begin({'lease_id': 'lease-one'})
    path = stands.runtime / '.stand/deployment-output' / identity / 'stdout'
    path.parent.mkdir(parents=True)
    raw = b'hello secret-value'
    path.write_bytes(raw)
    doc = ledger.snapshot()
    doc['attempts'][identity]['output'] = {'stdout': {'path': str(path), 'captured_bytes': len(raw),
        'captured_sha256': hashlib.sha256(raw).hexdigest()}}
    atomic_json(ledger.path, doc)
    assert stands.deployment_output(identity)['stdout']['text'] == 'hello [redacted]'
    path.write_bytes(b'tampered')
    with pytest.raises(GreatMindsError, match='changed'):
        stands.deployment_output(identity)
