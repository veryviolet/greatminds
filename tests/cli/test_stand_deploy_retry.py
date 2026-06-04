"""1.6.3: coordd auto-deploy retry/escalation. A deploy that RAISES leaves
the stand stuck in `preparing`; coordd retries it, and after
DEPLOY_MAX_ATTEMPTS escalates to MAINTAINER and forces the stand `down`."""
from __future__ import annotations

import yaml
import pytest

from greatminds.cli import coordd as cd


@pytest.fixture(autouse=True)
def _clean():
    cd._DEPLOYING_LEASES.discard("L1")
    cd._DEPLOY_ATTEMPTS.pop("L1", None)
    yield
    cd._DEPLOYING_LEASES.discard("L1")
    cd._DEPLOY_ATTEMPTS.pop("L1", None)


def _preparing(tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".stand").mkdir(parents=True)
    (coord / ".stand" / "state.yaml").write_text(yaml.safe_dump({
        "state": "preparing", "queue": [], "history": [],
        "active_lease": {"lease_id": "L1", "profile": "mlgpu2",
                         "worktree": str(tmp_path / "wt"),
                         "holder_role": "TESTER", "task": "0056"}}),
        encoding="utf-8")
    return coord


def _state(coord):
    return yaml.safe_load((coord / ".stand" / "state.yaml").read_text())


def test_raise_retries_then_escalates_and_forces_down(tmp_path, monkeypatch):
    coord = _preparing(tmp_path)
    esc = []
    monkeypatch.setattr(cd, "_escalate_to_maintainer",
                        lambda *a, **k: esc.append(a))

    def _boom(c, *, lease_id=None, **k):
        raise RuntimeError("ansible boom")
    monkeypatch.setattr("greatminds.cli.stand.deploy_lease", _boom)

    # attempts 1..MAX-1: count up, no escalation, stand stays preparing.
    for _ in range(cd.DEPLOY_MAX_ATTEMPTS - 1):
        cd._maybe_auto_deploy_stand(coord, False, run_async=False)
    assert not esc
    assert cd._DEPLOY_ATTEMPTS.get("L1") == cd.DEPLOY_MAX_ATTEMPTS - 1
    assert _state(coord)["state"] == "preparing"

    # final attempt: escalate + force the stand down.
    cd._maybe_auto_deploy_stand(coord, False, run_async=False)
    assert esc, "must escalate to MAINTAINER after DEPLOY_MAX_ATTEMPTS"
    assert _state(coord)["state"] == "down"
    assert "L1" not in cd._DEPLOY_ATTEMPTS


def test_success_clears_attempts(tmp_path, monkeypatch):
    coord = _preparing(tmp_path)
    cd._DEPLOY_ATTEMPTS["L1"] = 1
    monkeypatch.setattr("greatminds.cli.stand.deploy_lease",
                        lambda c, *, lease_id=None, **k: (0, "ok"))
    cd._maybe_auto_deploy_stand(coord, False, run_async=False)
    assert "L1" not in cd._DEPLOY_ATTEMPTS


def test_noop_when_not_preparing(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    (coord / ".stand").mkdir(parents=True)
    (coord / ".stand" / "state.yaml").write_text(
        yaml.safe_dump({"state": "free"}), encoding="utf-8")
    called = []
    monkeypatch.setattr("greatminds.cli.stand.deploy_lease",
                        lambda *a, **k: called.append(1) or (0, ""))
    assert cd._maybe_auto_deploy_stand(coord, False, run_async=False) is False
    assert not called
