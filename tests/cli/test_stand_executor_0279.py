"""Tests for task 0279 (0276 Phase C): stand-profile executor.

We don't actually run ``ansible-playbook`` in unit tests — the
subprocess is faked. The contract under test is the COMMAND LINE +
ENVIRONMENT the executor synthesizes from a ProfileSpec + lease
metadata: inventory file content, extra-vars JSON payload,
``--tags prerequisite`` when the spec opts into prereq-only mode,
and the return shape ``(exit_code, log)``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_executor as se
from greatminds.cli.stand_profile import ProfileSpec
from greatminds.core.errors import GreatMindsError


# ---------- fixtures ----------


def _yaml_spec(tmp_path: Path,
                prereq_only: bool = False) -> ProfileSpec:
    path = tmp_path / "stand-profiles" / "full-deploy.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump({
            "name": "full-deploy",
            "hosts": "avatar",
            "tasks": [{"name": "ping", "ansible.builtin.ping": None}],
        }),
        encoding="utf-8",
    )
    return ProfileSpec(
        name="full-deploy",
        format="yaml",
        path=path,
        yaml_data=None,
        deploy_prerequisites_only=prereq_only,
    )


def _md_spec(tmp_path: Path, content: str,
              prereq_only: bool = False) -> ProfileSpec:
    path = tmp_path / "stand-profiles" / "manual.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return ProfileSpec(
        name="manual",
        format="md",
        path=path,
        md_content=content,
        deploy_prerequisites_only=prereq_only,
    )


def _lease(host: str = "avatar",
            user: str = "deploy",
            task_id: str = "0279-test",
            **extras) -> dict:
    base = {
        "lease_id": "lease-uuid",
        "task_id": task_id,
        "worktree": "/opt/greatminds/.worktrees/0279",
        "host": host,
        "user": user,
        "deploy_path": "/srv/stand",
    }
    base.update(extras)
    return base


# ---------- _ansible_playbook_path ----------


def test_ansible_playbook_path_missing_raises(monkeypatch) -> None:
    """No ``ansible-playbook`` on PATH → actionable error pointing
    at Phase D + the one-line install recipe."""
    monkeypatch.setattr(se, "_sibling_ansible_playbook", lambda: None)
    monkeypatch.setattr(se.shutil, "which", lambda _name: None)
    with pytest.raises(GreatMindsError) as exc:
        se._ansible_playbook_path()
    msg = str(exc.value)
    assert "ansible-playbook" in msg
    assert "ansible-core" in msg


def test_ansible_playbook_path_returns_resolved(monkeypatch) -> None:
    monkeypatch.setattr(se, "_sibling_ansible_playbook", lambda: None)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    assert se._ansible_playbook_path() == "/fake/bin/ansible-playbook"


def test_ansible_playbook_path_prefers_active_python_sibling(
    monkeypatch, tmp_path: Path,
) -> None:
    """Avatar/uv regression: absolute venv ``greatminds`` launch with the
    venv bin absent from PATH still has ``ansible-playbook`` beside
    ``sys.executable``. ``uv venv`` symlinks ``venv/bin/python`` to a shared
    interpreter, so resolving the symlink before checking siblings misses the
    venv's console scripts."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    realdir = tmp_path / "uv-python" / "bin"
    realdir.mkdir(parents=True)
    real_py = realdir / "python"
    real_py.write_text("#!/bin/sh\n", encoding="utf-8")
    real_py.chmod(0o755)
    py = bindir / "python"
    py.symlink_to(real_py)
    ansible = bindir / "ansible-playbook"
    ansible.write_text("#!/bin/sh\n", encoding="utf-8")
    ansible.chmod(0o755)

    monkeypatch.setattr(se.sys, "executable", str(py))
    monkeypatch.setattr(se.shutil, "which", lambda _name: None)

    assert se._ansible_playbook_path() == str(ansible)


# ---------- inventory / extra-vars synthesis ----------


def test_executor_has_no_inventory_synthesis() -> None:
    # The clean host scheme retired single-host inventory synthesis +
    # ${}-substitution; the profile author owns hosts (add_host from
    # PROJECT.env vars, or a static inventory shipped alongside).
    assert not hasattr(se, "_build_inventory")
    assert not hasattr(se, "_host_from_playbook")
    assert not hasattr(se, "_substitute")


def test_build_extra_vars_drops_inventory_only_keys() -> None:
    ev = se._build_extra_vars(_lease(host="avatar", user="deploy"))
    # host / user / ansible_become already on inventory line.
    for forbidden in ("host", "user", "ansible_become"):
        assert forbidden not in ev
    # The lease-shape keys are propagated.
    assert ev["lease_id"] == "lease-uuid"
    assert ev["task_id"] == "0279-test"
    assert ev["deploy_path"] == "/srv/stand"


# ---------- execute_yaml_profile command-line shape ----------


def test_execute_yaml_builds_expected_argv(tmp_path: Path,
                                              monkeypatch) -> None:
    """Host-agnostic command line: ``ansible-playbook <profile>
    --extra-vars @<json>`` with NO ``-i`` synthesis, run with
    ``cwd=coord``. The extra-vars JSON carries the WHOLE PROJECT.env
    (so ``{{ STAND_HOST_A }}`` resolves) merged with lease meta, and
    drops the legacy single-host inventory keys."""
    # coord = tmp_path (the profile lives under tmp_path/stand-profiles/).
    # Its PROJECT.env must be funneled into extra-vars.
    (tmp_path / "PROJECT.env").write_text(
        "STAND_HOST_A=node-a\nSTAND_HOST_B=node-b\n", encoding="utf-8")
    spec = _yaml_spec(tmp_path, prereq_only=False)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        assert "-i" not in cmd, "executor must not synthesize an inventory"
        if "--extra-vars" in cmd:
            ev_arg = cmd[cmd.index("--extra-vars") + 1]
            assert ev_arg.startswith("@"), (
                f"expected @file form for extra-vars, got {ev_arg!r}"
            )
            captured["extra_vars"] = json.loads(
                Path(ev_arg[1:]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(se.subprocess, "run", fake_run)
    monkeypatch.setattr(se, "_sibling_ansible_playbook", lambda: None)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    # Safety gate is covered by 0285; here we exercise argv wiring.
    monkeypatch.setattr(se, "is_deploy_safe", lambda *a, **k: (True, ""))

    rc, log = se.execute_yaml_profile(spec, _lease(coord=str(tmp_path)))
    assert rc == 0
    assert log == "ok\n"
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/bin/ansible-playbook"
    assert "-i" not in cmd
    assert any(c.endswith(spec.path.name) for c in cmd)
    assert "--extra-vars" in cmd
    assert "--tags" not in cmd
    # Run in the fleet's coord dir (so author ansible.cfg/inventory applies).
    assert captured["cwd"] == str(tmp_path)
    # Extra-vars carries the WHOLE PROJECT.env + lease meta, minus legacy
    # inventory keys.
    ev = captured["extra_vars"]
    assert ev["STAND_HOST_A"] == "node-a"
    assert ev["STAND_HOST_B"] == "node-b"
    assert ev["task_id"] == "0279-test"
    assert "host" not in ev


def test_execute_yaml_passes_prereq_tag_when_flag_set(
    tmp_path: Path, monkeypatch,
) -> None:
    """``spec.deploy_prerequisites_only=True`` → ``--tags prerequisite``
    appended so ansible runs only tagged tasks."""
    spec = _yaml_spec(tmp_path, prereq_only=True)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(se.subprocess, "run", fake_run)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    se.execute_yaml_profile(spec, _lease())
    cmd = captured["cmd"]
    assert "--tags" in cmd
    assert "prerequisite" in cmd


def test_execute_yaml_propagates_nonzero_exit(tmp_path: Path,
                                                 monkeypatch) -> None:
    spec = _yaml_spec(tmp_path)
    monkeypatch.setattr(se, "_sibling_ansible_playbook", lambda: None)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a[0], returncode=3, stdout="", stderr="ansible failed\n"),
    )
    rc, log = se.execute_yaml_profile(spec, _lease())
    assert rc == 3
    assert "ansible failed" in log


def test_execute_yaml_timeout_returns_124(tmp_path: Path,
                                            monkeypatch) -> None:
    spec = _yaml_spec(tmp_path)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout"))
    monkeypatch.setattr(se.subprocess, "run", fake_run)
    rc, log = se.execute_yaml_profile(
        spec, _lease(), timeout_seconds=1.0)
    assert rc == 124
    assert "timed out" in log


def test_execute_yaml_missing_path_rejected(tmp_path: Path,
                                              monkeypatch) -> None:
    """A ProfileSpec whose path was deleted between load and execute
    surfaces a clear error before any subprocess call."""
    spec = _yaml_spec(tmp_path)
    spec.path.unlink()
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    with pytest.raises(GreatMindsError) as exc:
        se.execute_yaml_profile(spec, _lease())
    assert "does not" in str(exc.value).lower()


def test_execute_yaml_rejects_non_yaml_format(tmp_path: Path,
                                                 monkeypatch) -> None:
    spec = _md_spec(tmp_path, "prose")
    with pytest.raises(GreatMindsError):
        se.execute_yaml_profile(spec, _lease())


# ---------- execute_md_profile ----------


def test_dispatch_routes_yaml_to_ansible(tmp_path: Path,
                                            monkeypatch) -> None:
    spec = _yaml_spec(tmp_path)
    monkeypatch.setattr(se, "_sibling_ansible_playbook", lambda: None)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    seen: list = []
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **k: seen.append(cmd) or subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="x", stderr=""),
    )
    rc, log = se.dispatch_profile(spec, _lease())
    assert rc == 0
    assert seen, "0279: YAML dispatch must reach subprocess.run"
    assert seen[0][0] == "/fake/bin/ansible-playbook"
