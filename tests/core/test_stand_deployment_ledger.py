import pytest

from greatminds.domain.stand_deployments import DeploymentLedger
from greatminds.core.errors import GreatMindsError


def test_intent_retains_lease_identity_without_arbitrary_secret_fields(tmp_path):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1', 'task': 'T1', 'password': 'do-not-save', 'environment': {'KEY': 'secret'}})
    record = ledger.snapshot()['attempts'][attempt]
    assert record['lease'] == {'lease_id': 'L1', 'task': 'T1'}
    assert 'do-not-save' not in ledger.path.read_text()
    with pytest.raises(GreatMindsError, match='refusing external replay'):
        ledger.begin({'lease_id': 'L2'})


@pytest.mark.parametrize('document', ['{', '[]', '{"version":2,"attempts":{}}', '{"version":1,"attempts":{"x":{"id":"x","status":"unknown"}}}'])
def test_corrupt_ledger_never_becomes_empty_history(tmp_path, document):
    ledger = DeploymentLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text(document)
    with pytest.raises(GreatMindsError):
        ledger.require_resolved()


def test_same_lease_history_without_attempt_identity_does_not_prove_application(tmp_path):
    from greatminds.cli import stand_state as ss
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    ledger.finished(attempt, 0, 'done')
    ss.update_stand_state(tmp_path, lambda state: ss.record_transition(state, 'preparing', 'ready', 'COORDD', lease_id='L1'))
    ledger.reconcile_applied()
    assert ledger.snapshot()['attempts'][attempt]['status'] == 'command_finished'
    with pytest.raises(GreatMindsError):
        ledger.require_resolved()


def test_resolution_repeat_preserves_receipt_and_rejects_changed_explanation(tmp_path):
    ledger = DeploymentLedger(tmp_path)
    attempt = ledger.begin({'lease_id': 'L1'})
    with pytest.raises(GreatMindsError, match='cleanup'):
        ledger.resolve(attempt, reason='inspected')
    ledger.recover_process(attempt)  # No process was launched through the gate.
    resolved = ledger.resolve(attempt, reason='inspected')
    before = ledger.path.read_bytes()
    assert ledger.resolve(attempt, reason='inspected') == resolved
    assert ledger.path.read_bytes() == before
    with pytest.raises(GreatMindsError, match='different explanation'):
        ledger.resolve(attempt, reason='different')
    assert ledger.path.read_bytes() == before
