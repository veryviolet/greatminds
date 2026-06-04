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
    monkeypatch.setattr("greatminds.cli.stand_profile.load_profile",
                        lambda _c, p: SimpleNamespace(format=fmt, name=p))
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
