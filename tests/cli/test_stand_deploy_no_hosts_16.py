"""issue #16 / TESTER 0360 report: a stand deploy must NOT mark the stand
ready after an empty-inventory / no-hosts-matched ansible run.

Root cause: with lease host=None the deploy play's hosts pattern matched no
host, ansible exited rc=0 having run ZERO tasks, and coordd treated rc=0 as
deploy success → stand `preparing → ready` without anything being
provisioned (silently invalidating stand_required validation). The deploy
path now defensively converts such a vacuous rc=0 run into a failure so the
stand transitions `down` instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from greatminds.cli import stand
from greatminds.cli import stand_executor as se
from greatminds.cli.stand_profile import ProfileSpec


# ---------- vacuous_deploy_reason (pure) ----------

_REAL_RECAP = (
    "PLAY [deploy] ******\n"
    "TASK [ping] ******\nok: [node-a]\n"
    "PLAY RECAP ******\n"
    "node-a : ok=3 changed=1 unreachable=0 failed=0 skipped=0\n"
)
_NO_HOST_RECAP = (
    "[WARNING]: Could not match supplied host pattern, ignoring: avatar\n"
    "PLAY [deploy] ******\nskipping: no hosts matched\n"
    "PLAY RECAP ******\n"
)


def test_reason_flags_no_hosts_matched():
    assert se.vacuous_deploy_reason("skipping: no hosts matched\n")
    assert se.vacuous_deploy_reason(
        "[WARNING]: Could not match supplied host pattern, ignoring: x")


def test_reason_flags_empty_recap():
    assert se.vacuous_deploy_reason(_NO_HOST_RECAP)


def test_reason_none_for_real_deploy():
    assert se.vacuous_deploy_reason(_REAL_RECAP) is None


def test_reason_none_for_empty_log():
    # cannot prove a no-op without captured output → trust the raw rc
    assert se.vacuous_deploy_reason("") is None


# ---------- execute_yaml_profile rc conversion ----------

def _yaml_spec(tmp_path: Path) -> ProfileSpec:
    path = tmp_path / "stand-profiles" / "full-deploy.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "name": "full-deploy", "hosts": "avatar",
        "tasks": [{"name": "ping", "ansible.builtin.ping": None}],
    }), encoding="utf-8")
    return ProfileSpec(name="full-deploy", format="yaml", path=path,
                       yaml_data=None, deploy_prerequisites_only=False)


def _fake_ansible(monkeypatch, *, rc, stdout):
    monkeypatch.setattr(se.shutil, "which",
                        lambda _n: "/fake/bin/ansible-playbook")
    monkeypatch.setattr(se, "is_deploy_safe", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout=stdout, stderr=""))


def test_execute_converts_rc0_no_hosts_to_failure(tmp_path, monkeypatch):
    """ansible rc=0 + no-hosts output → execute_yaml_profile returns the
    synthetic no-hosts failure rc, and the deploy marker records it."""
    spec = _yaml_spec(tmp_path)
    _fake_ansible(monkeypatch, rc=0, stdout=_NO_HOST_RECAP)

    rc, log = se.execute_yaml_profile(
        spec, {"coord": str(tmp_path), "host": None,
               "lease_id": "L1", "task_id": "0001"})

    assert rc == se.DEPLOY_NO_HOSTS_RC and rc != 0
    assert "matched no hosts" in log or "lists no hosts" in log
    # marker reflects the failure rc, not the raw ansible 0
    marker = se.deploy_marker_path(tmp_path, "L1")
    assert marker.is_file()
    assert str(se.DEPLOY_NO_HOSTS_RC) in marker.read_text(encoding="utf-8")


def test_execute_keeps_rc0_for_real_deploy(tmp_path, monkeypatch):
    """A genuine deploy (host in PLAY RECAP) still returns rc=0."""
    spec = _yaml_spec(tmp_path)
    _fake_ansible(monkeypatch, rc=0, stdout=_REAL_RECAP)

    rc, _log = se.execute_yaml_profile(
        spec, {"coord": str(tmp_path), "host": "node-a",
               "lease_id": "L2", "task_id": "0001"})

    assert rc == 0


# ---------- deploy_lease end-to-end: down, not ready ----------

def _prepare(coord: Path, lease_id="L1"):
    (coord / ".stand").mkdir(parents=True)
    (coord / ".stand" / "state.yaml").write_text(yaml.safe_dump({
        "state": "preparing",
        "active_lease": {
            "lease_id": lease_id, "profile": "full-deploy",
            "worktree": str(coord.parent / "wt"),
            "holder_role": "TESTER", "task": "0001-verify", "host": None},
        "queue": [], "history": [],
    }), encoding="utf-8")


def test_deploy_lease_goes_down_on_vacuous_run(tmp_path, monkeypatch):
    """The false-ready the report described: a no-hosts rc=0 deploy must
    leave the stand `down`, never `ready`."""
    coord = tmp_path / "coordination"
    _prepare(coord)
    monkeypatch.setattr("greatminds.cli.stand_profile.load_profile",
                        lambda _c, p: SimpleNamespace(format="yaml", name=p))
    # the executor converts the vacuous rc=0 ansible run to a failure rc
    monkeypatch.setattr(
        "greatminds.cli.stand_executor.dispatch_profile",
        lambda spec, meta, **k: (se.DEPLOY_NO_HOSTS_RC,
                                 "ansible matched no hosts"))

    rc, _log = stand.deploy_lease(coord, lease_id="L1")

    assert rc == se.DEPLOY_NO_HOSTS_RC
    st = yaml.safe_load((coord / ".stand" / "state.yaml").read_text())
    assert st["state"] == "down"
    assert st["active_lease"] is None
