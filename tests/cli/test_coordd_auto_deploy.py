"""1.6.0: coordd auto-deploys a `preparing` stand lease (no STAND-KEEPER).

A `.stand` state change to `preparing` makes coordd run the lease's
deploy via the deterministic engine in a background thread — dedup by
lease_id, no LLM, no classifier in the loop.
"""
from __future__ import annotations

import threading
from pathlib import Path

import yaml

from greatminds.cli import coordd as cd


def _state(coord: Path, state: str, lease=True):
    (coord / ".stand").mkdir(parents=True, exist_ok=True)
    doc = {"state": state, "queue": [], "history": [], "active_lease": None}
    if lease:
        doc["active_lease"] = {
            "lease_id": "L9", "profile": "full-deploy",
            "worktree": str(coord.parent / "wt"), "holder_role": "TESTER",
            "task": "0001"}
    (coord / ".stand" / "state.yaml").write_text(yaml.safe_dump(doc),
                                                 encoding="utf-8")


def test_no_deploy_when_not_preparing(tmp_path):
    coord = tmp_path / "coordination"
    _state(coord, "free", lease=False)
    assert cd._maybe_auto_deploy_stand(coord, verbose=False) is False


def test_deploy_dispatched_when_preparing(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _state(coord, "preparing")
    done = threading.Event()
    seen = {}

    def fake_deploy(c, *, lease_id=None, **k):
        seen["lease_id"] = lease_id
        seen["coord"] = c
        done.set()
        return (0, "ok")

    monkeypatch.setattr("greatminds.cli.stand.deploy_lease", fake_deploy)
    cd._DEPLOYING_LEASES.discard("L9")

    started = cd._maybe_auto_deploy_stand(coord, verbose=False)
    assert started is True
    assert done.wait(timeout=3), "deploy thread should run deploy_lease"
    assert seen["lease_id"] == "L9"


def test_dedup_skips_when_already_deploying(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    _state(coord, "preparing")
    monkeypatch.setattr("greatminds.cli.stand.deploy_lease",
                        lambda *a, **k: (0, "ok"))
    cd._DEPLOYING_LEASES.add("L9")
    try:
        assert cd._maybe_auto_deploy_stand(coord, verbose=False) is False
    finally:
        cd._DEPLOYING_LEASES.discard("L9")
