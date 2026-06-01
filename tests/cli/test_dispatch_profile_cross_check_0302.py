"""Tests for task 0302: ``dispatch_profile`` cross-checks
``spec.name`` against ``lease_meta['profile']``.

Pre-0302 SK could silently run the wrong playbook against the
wrong lease — upstream issue #7 reported a lease for profile
``orange`` running ``mlgpu2.yaml`` because a title-derived
fallback won. 0302 closes the gap at the dispatch entry: a
spec/lease mismatch aborts before any subprocess.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_executor as se
from greatminds.cli.stand_profile import ProfileSpec
from greatminds.core.errors import GreatMindsError


def _yaml_spec(tmp_path: Path, name: str = "full-deploy") -> ProfileSpec:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump({
        "name": name, "hosts": "stand",
        "tasks": [{"name": "x"}],
    }), encoding="utf-8")
    return ProfileSpec(name=name, format="yaml", path=path,
                        yaml_data=None,
                        deploy_prerequisites_only=False)


def _md_spec(tmp_path: Path, name: str = "manual") -> ProfileSpec:
    path = tmp_path / f"{name}.md"
    path.write_text("body\n", encoding="utf-8")
    return ProfileSpec(name=name, format="md", path=path,
                        md_content="body", deploy_prerequisites_only=False)


def _lease(profile: str, **extras) -> dict:
    base = {
        "lease_id": "lease-uuid",
        "task_id": "0302-probe",
        "worktree": "/opt/greatminds/.worktrees/0302",
        "host": "avatar",
        "user": "deploy",
        "deploy_path": "/srv/stand",
        "profile": profile,
    }
    base.update(extras)
    return base


# ---------- mismatch refused ----------


def test_dispatch_refuses_yaml_spec_lease_profile_mismatch(
    tmp_path: Path, monkeypatch,
) -> None:
    """0302: spec.name='mlgpu2' + lease.profile='orange' must
    raise GreatMindsError BEFORE the subprocess runs."""
    spec = _yaml_spec(tmp_path, name="mlgpu2")
    lease = _lease(profile="orange")

    # If we reach subprocess.run, the gate failed.
    monkeypatch.setattr(se.subprocess, "run",
                         lambda *a, **kw: pytest.fail(
                             "0302: subprocess must NOT be reached "
                             "on spec/lease mismatch"))
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    with pytest.raises(GreatMindsError) as exc:
        se.dispatch_profile(spec, lease)
    msg = str(exc.value)
    assert "mlgpu2" in msg
    assert "orange" in msg
    assert "refusing" in msg or "wrong playbook" in msg


def test_dispatch_refuses_md_spec_lease_profile_mismatch(
    tmp_path: Path,
) -> None:
    """Same gate for the MD path — wrong prose is just as
    dangerous as wrong YAML."""
    spec = _md_spec(tmp_path, name="liveness-prose")
    lease = _lease(profile="smoke-only")
    with pytest.raises(GreatMindsError) as exc:
        se.dispatch_profile(spec, lease)
    assert "liveness-prose" in str(exc.value)
    assert "smoke-only" in str(exc.value)


# ---------- match proceeds ----------


def test_dispatch_proceeds_when_yaml_spec_matches_lease(
    tmp_path: Path, monkeypatch,
) -> None:
    """spec.name == lease.profile → dispatch reaches subprocess."""
    spec = _yaml_spec(tmp_path, name="full-deploy")
    lease = _lease(profile="full-deploy")

    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    import subprocess as _sp
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **kw: _sp.CompletedProcess(
            args=cmd, returncode=0, stdout="ok", stderr=""),
    )
    rc, log = se.dispatch_profile(spec, lease)
    assert rc == 0


def test_dispatch_proceeds_when_md_spec_matches_lease(
    tmp_path: Path,
) -> None:
    spec = _md_spec(tmp_path, name="liveness-prose")
    lease = _lease(profile="liveness-prose")
    rc, _ = se.dispatch_profile(spec, lease)
    assert rc == 0


# ---------- backwards-compat for legacy callers ----------


def test_dispatch_allows_lease_without_profile_key(
    tmp_path: Path, monkeypatch,
) -> None:
    """Older test fixtures + scripts may pass lease_meta without a
    ``profile`` key. The cross-check skips when the field is
    absent — refusing here would break compatibility. SK's real
    runtime always supplies ``profile``."""
    spec = _yaml_spec(tmp_path, name="full-deploy")
    lease = {  # no 'profile' key
        "lease_id": "x", "task_id": "y", "host": "h",
        "worktree": "/opt/greatminds/.worktrees/0302",
    }
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    import subprocess as _sp
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **kw: _sp.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""),
    )
    # Must not raise.
    se.dispatch_profile(spec, lease)


def test_dispatch_refuses_when_lease_profile_is_empty_string(
    tmp_path: Path,
) -> None:
    """An empty-string ``profile`` is treated as mismatched
    (defense in depth — no plausible spec.name is empty)."""
    spec = _yaml_spec(tmp_path, name="full-deploy")
    lease = _lease(profile="")
    with pytest.raises(GreatMindsError):
        se.dispatch_profile(spec, lease)
