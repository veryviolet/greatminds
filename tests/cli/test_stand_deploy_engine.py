"""1.6.0 deploy engine: `deploy_lease` runs the lease's YAML profile and
transitions the stand ready (rc==0) / down (rc!=0). The deterministic,
sanctioned deploy path used by coordd (auto) and `stand deploy` (manual).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from greatminds.cli import stand
from greatminds.core.errors import GreatMindsError


def _prepare(coord: Path, profile="full-deploy", lease_id="L1"):
    (coord / ".stand").mkdir(parents=True)
    (coord / ".stand" / "state.yaml").write_text(yaml.safe_dump({
        "state": "preparing",
        "active_lease": {
            "lease_id": lease_id, "profile": profile,
            "worktree": str(coord.parent / "wt"),
            "holder_role": "TESTER", "task": "0001-verify"},
        "queue": [], "history": [],
    }), encoding="utf-8")


def _patch(monkeypatch, *, rc, fmt="yaml"):
    monkeypatch.setattr(
        "greatminds.cli.stand_profile.load_profile",
        lambda _c, p, **_k: SimpleNamespace(
            format=fmt, name=p, source="main", path="/x/stand-profiles"))
    monkeypatch.setattr("greatminds.cli.stand_executor.dispatch_profile",
                        lambda spec, meta, **k: (rc, f"log rc={rc}"))


def _state(coord: Path) -> dict:
    return yaml.safe_load((coord / ".stand" / "state.yaml").read_text())


def test_deploy_lease_ready_on_success(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord)
    _patch(monkeypatch, rc=0)

    rc, _log = stand.deploy_lease(coord, lease_id="L1")

    assert rc == 0
    st = _state(coord)
    assert st["state"] == "ready"
    assert st["active_lease"]["ready_at"]
    # holder notified
    msgs = list((coord / "inbox" / "tester").glob("*.yaml")) \
        if (coord / "inbox" / "tester").is_dir() else []
    assert msgs, "holder should get a ready inbox-info"


def test_deploy_lease_down_on_failure(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord)
    _patch(monkeypatch, rc=2)

    rc, _log = stand.deploy_lease(coord, lease_id="L1")

    assert rc == 2
    st = _state(coord)
    assert st["state"] == "down"
    assert "rc=2" in (st.get("down_reason") or "")
    assert st["active_lease"] is None


def test_deploy_lease_rejects_md_profile(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord)
    _patch(monkeypatch, rc=0, fmt="md")
    with pytest.raises(GreatMindsError) as e:
        stand.deploy_lease(coord, lease_id="L1")
    assert "YAML/ansible" in str(e.value)


def test_deploy_lease_lease_id_mismatch(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord, lease_id="L1")
    _patch(monkeypatch, rc=0)
    with pytest.raises(GreatMindsError):
        stand.deploy_lease(coord, lease_id="OTHER")


# ---------------------------------------------------------------------------
# 0363 (GitHub #9): lease_meta.host resolution. The lease state-file carries
# no host, so coordd-driven deploys must resolve one — profile-name default,
# profile YAML (``vars.deploy_host``) override, lease value wins if present.
# ---------------------------------------------------------------------------


def _patch_capture(monkeypatch, *, rc=0, spec_host=None):
    """Mock load_profile (with an optional ``host`` on the spec) and capture
    the ``lease_meta`` dispatch_profile receives."""
    captured: dict = {}
    monkeypatch.setattr(
        "greatminds.cli.stand_profile.load_profile",
        lambda _c, p, **_k: SimpleNamespace(
            format="yaml", name=p, host=spec_host,
            source="main", path="/x/stand-profiles"))

    def _dispatch(spec, meta, **k):
        captured["meta"] = meta
        return (rc, f"log rc={rc}")

    monkeypatch.setattr(
        "greatminds.cli.stand_executor.dispatch_profile", _dispatch)
    return captured


def test_host_defaults_to_profile_name(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord, profile="mlgpu2")
    cap = _patch_capture(monkeypatch, spec_host=None)

    stand.deploy_lease(coord, lease_id="L1")

    # No host on the lease, no host in the profile → profile name is the host.
    assert cap["meta"]["host"] == "mlgpu2"


def test_profile_yaml_host_overrides_default(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord, profile="mlgpu2")
    cap = _patch_capture(monkeypatch, spec_host="srv5-mlgpu-2.area.zov")

    stand.deploy_lease(coord, lease_id="L1")

    # Profile YAML declared a host → it wins over the profile-name default.
    assert cap["meta"]["host"] == "srv5-mlgpu-2.area.zov"


def test_lease_host_wins_over_profile(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _prepare(coord, profile="mlgpu2")
    # Simulate a lease that DID carry a host (future --host flag).
    st = _state(coord)
    st["active_lease"]["host"] = "lease-host.example"
    (coord / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(st), encoding="utf-8")
    cap = _patch_capture(monkeypatch, spec_host="profile-host.example")

    stand.deploy_lease(coord, lease_id="L1")

    assert cap["meta"]["host"] == "lease-host.example"


@pytest.mark.parametrize('rc', [0, 2])
@pytest.mark.parametrize('replacement', ['new_lease', 'down', 'changed_profile'])
def test_deploy_result_cannot_mutate_replaced_lease(tmp_path, monkeypatch, rc, replacement):
    from greatminds.cli import stand_state as ss
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=rc)
    expected = {}

    def dispatch(*args, **kwargs):
        def replace(state):
            if replacement == 'new_lease':
                state['active_lease']['lease_id'] = 'L2'
            elif replacement == 'down':
                state.update(state='down', active_lease=None, down_reason='operator intervention')
            else:
                state['active_lease']['profile'] = 'different-target'
        expected.update(ss.update_stand_state(coord, replace))
        return rc, 'late deployment result'

    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', dispatch)
    with pytest.raises(GreatMindsError, match='lease changed during deployment'):
        stand.deploy_lease(coord, lease_id='L1')
    assert ss.read_stand_state(coord) == expected


def test_concurrent_deployment_refused_before_external_execution(tmp_path, monkeypatch):
    from greatminds.core.storage import file_lock
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=0)
    def unexpected(*args, **kwargs):
        pytest.fail('a second external deployment was started')
    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', unexpected)
    with file_lock(coord / '.stand/deployment.lock', label='test'):
        with pytest.raises(GreatMindsError, match='stand deployment is being transitioned'):
            stand.deploy_lease(coord, lease_id='L1')
    assert _state(coord)['state'] == 'preparing'


def test_executor_exception_is_durable_and_never_replayed(tmp_path, monkeypatch):
    from greatminds.domain.stand_deployments import DeploymentLedger
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=0)
    calls = []
    def crash(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('synthetic-secret-do-not-persist')
    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', crash)
    with pytest.raises(RuntimeError):
        stand.deploy_lease(coord, lease_id='L1')
    ledger = DeploymentLedger(coord)
    attempt = next(iter(ledger.snapshot()['attempts'].values()))
    assert attempt['status'] == 'needs_recovery'
    assert 'synthetic-secret' not in ledger.path.read_text()
    with pytest.raises(GreatMindsError, match='refusing external replay'):
        stand.deploy_lease(coord, lease_id='L1')
    assert len(calls) == 1


def test_success_records_result_and_applied_transition(tmp_path, monkeypatch):
    from greatminds.domain.stand_deployments import DeploymentLedger
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=0)
    monkeypatch.setattr(stand, '_file_inbox_info', lambda *a, **kw: None)
    stand.deploy_lease(coord, lease_id='L1')
    ledger = DeploymentLedger(coord)
    receipt = next(iter(ledger.snapshot()['attempts'].values()))
    assert receipt['status'] == 'applied' and receipt['exit_code'] == 0
    assert receipt['lease']['lease_id'] == 'L1'
    assert receipt['log_sha256']
    ledger.require_resolved()


def test_process_death_after_external_effect_blocks_replay_across_lease_change(tmp_path, monkeypatch):
    import multiprocessing
    import os
    from greatminds.cli import stand_state as ss
    from greatminds.domain.stand_deployments import DeploymentLedger
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=0)
    effect = tmp_path / 'external-effect'
    def crash(*args, **kwargs):
        effect.write_text('executed once')
        os._exit(73)
    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', crash)
    child = multiprocessing.get_context('fork').Process(target=stand.deploy_lease, args=(coord,), kwargs={'lease_id': 'L1'})
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.kill()
        child.join(timeout=5)
        pytest.fail('fault child did not exit')
    assert child.exitcode == 73 and effect.read_text() == 'executed once'
    receipt = next(iter(DeploymentLedger(coord).snapshot()['attempts'].values()))
    assert receipt['status'] == 'started'
    ss.update_stand_state(coord, lambda s: s['active_lease'].update(lease_id='L2'))
    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', lambda *a, **k: pytest.fail('replayed'))
    with pytest.raises(GreatMindsError, match='refusing external replay'):
        stand.deploy_lease(coord, lease_id='L2')


def test_result_saved_before_failed_stand_publication_blocks_replay(tmp_path, monkeypatch):
    from greatminds.cli import stand_state as ss
    from greatminds.domain.stand_deployments import DeploymentLedger
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=0)
    original = ss.atomic_bytes
    def dispatch(*args, **kwargs):
        def fail(*args, **kwargs):
            raise OSError('state write failed')
        monkeypatch.setattr(ss, 'atomic_bytes', fail)
        return 0, 'executed'
    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', dispatch)
    with pytest.raises(OSError, match='state write failed'):
        stand.deploy_lease(coord, lease_id='L1')
    monkeypatch.setattr(ss, 'atomic_bytes', original)
    receipt = next(iter(DeploymentLedger(coord).snapshot()['attempts'].values()))
    assert receipt['status'] == 'command_finished' and receipt['exit_code'] == 0
    assert _state(coord)['state'] == 'preparing'
    with pytest.raises(GreatMindsError, match='refusing external replay'):
        stand.deploy_lease(coord, lease_id='L1')


def test_deployment_status_exposes_recovery_without_reading_yaml(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from greatminds.domain.stand_deployments import DeploymentLedger
    import json
    coord = tmp_path / 'coordination'
    _prepare(coord)
    attempt = DeploymentLedger(coord).begin(_state(coord)['active_lease'])
    monkeypatch.setattr(stand, 'find_coord_dir', lambda: coord)
    result = CliRunner().invoke(stand.stand, ['deployment-status'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['attempts'][attempt]['status'] == 'started'


@pytest.mark.parametrize('rc', [0, 2])
def test_completed_stand_transition_recovers_receipt_without_execution(tmp_path, monkeypatch, rc):
    from greatminds.domain.stand_deployments import DeploymentLedger
    coord = tmp_path / 'coordination'
    _prepare(coord)
    _patch(monkeypatch, rc=rc)
    monkeypatch.setattr(stand, '_file_inbox_info', lambda *a, **k: None)
    def fail(*args, **kwargs):
        raise OSError('receipt persistence failed')
    monkeypatch.setattr(DeploymentLedger, 'applied', fail)
    with pytest.raises(OSError, match='receipt persistence'):
        stand.deploy_lease(coord, lease_id='L1')
    ledger = DeploymentLedger(coord)
    assert next(iter(ledger.snapshot()['attempts'].values()))['status'] == 'command_finished'
    assert _state(coord)['state'] == ('ready' if rc == 0 else 'down')
    monkeypatch.setattr('greatminds.cli.stand_executor.dispatch_profile', lambda *a, **k: pytest.fail('replayed'))
    ledger.reconcile_applied()
    receipt = next(iter(ledger.snapshot()['attempts'].values()))
    assert receipt['status'] == 'applied' and receipt['recovery'] == 'stand_transition_recorded'
    before = ledger.path.read_bytes()
    ledger.reconcile_applied()
    assert ledger.path.read_bytes() == before
