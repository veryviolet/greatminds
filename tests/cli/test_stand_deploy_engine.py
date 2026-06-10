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
