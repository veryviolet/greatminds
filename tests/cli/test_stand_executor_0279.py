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
            "hosts": "stand",
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
    monkeypatch.setattr(se.shutil, "which", lambda _name: None)
    with pytest.raises(GreatMindsError) as exc:
        se._ansible_playbook_path()
    msg = str(exc.value)
    assert "ansible-playbook" in msg
    assert "ansible-core" in msg


def test_ansible_playbook_path_returns_resolved(monkeypatch) -> None:
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    assert se._ansible_playbook_path() == "/fake/bin/ansible-playbook"


# ---------- inventory / extra-vars synthesis ----------


def test_build_inventory_includes_user_and_become() -> None:
    inv = se._build_inventory(_lease(host="avatar", user="deploy"))
    assert "[stand]" in inv
    assert "avatar" in inv
    assert "ansible_user=deploy" in inv
    assert "ansible_become=true" in inv


def test_build_inventory_can_disable_become() -> None:
    inv = se._build_inventory(
        _lease(host="avatar", user="deploy", ansible_become=False))
    assert "ansible_become" not in inv


def test_build_inventory_missing_host_rejected() -> None:
    with pytest.raises(GreatMindsError) as exc:
        se._build_inventory({"user": "deploy"})  # no host
    assert "host" in str(exc.value).lower()


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
    """The synthesized command line must include ``ansible-playbook``,
    ``-i <tmp-inventory>``, ``<playbook-path>``, ``--extra-vars @<json>``
    (and no ``--tags`` when prereq_only=False)."""
    spec = _yaml_spec(tmp_path, prereq_only=False)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        # Snapshot the command + the inventory + extra-vars contents
        # before the temp dir is cleaned up.
        captured["cmd"] = list(cmd)
        inv_path = Path(cmd[cmd.index("-i") + 1])
        captured["inventory"] = inv_path.read_text(encoding="utf-8")
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
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    rc, log = se.execute_yaml_profile(spec, _lease())
    assert rc == 0
    assert log == "ok\n"
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/bin/ansible-playbook"
    assert "-i" in cmd and str(spec.path) in cmd
    assert "--extra-vars" in cmd
    assert "--tags" not in cmd
    # Inventory shape: [stand] group + host + user + become.
    assert "[stand]" in captured["inventory"]
    assert "avatar" in captured["inventory"]
    # Extra-vars JSON does NOT include host/user/ansible_become.
    ev = captured["extra_vars"]
    assert "host" not in ev
    assert ev["task_id"] == "0279-test"


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


def test_execute_md_substitutes_vars(tmp_path: Path) -> None:
    """The MD body's ``${var}`` references are substituted from the
    lease meta dict; unknown names stay literal so misconfigurations
    surface in the rendered text."""
    spec = _md_spec(
        tmp_path,
        "Deploy to ${host} as ${user}; lease=${lease_id}; "
        "task=${task_id}. (${unknown_var} stays as-is.)",
    )
    rc, rendered = se.execute_md_profile(spec, _lease(host="avatar"))
    assert rc == 0
    assert "Deploy to avatar as deploy" in rendered
    assert "lease=lease-uuid" in rendered
    assert "task=0279-test" in rendered
    # Unknown vars stay literal (safe_substitute semantics).
    assert "${unknown_var}" in rendered


def test_execute_md_no_subprocess(tmp_path: Path, monkeypatch) -> None:
    """No subprocess invocation may happen during MD execution —
    the executor is meant to be a pure render step."""
    spec = _md_spec(tmp_path, "no subs here")
    calls: list = []
    monkeypatch.setattr(se.subprocess, "run",
                         lambda *a, **k: calls.append(a) or None)
    se.execute_md_profile(spec, _lease())
    assert calls == [], "0279: execute_md_profile must NOT subprocess"


def test_execute_md_rejects_non_md_format(tmp_path: Path) -> None:
    spec = _yaml_spec(tmp_path)
    with pytest.raises(GreatMindsError):
        se.execute_md_profile(spec, _lease())


# ---------- dispatch_profile (single entrypoint) ----------


def test_dispatch_routes_yaml_to_ansible(tmp_path: Path,
                                            monkeypatch) -> None:
    spec = _yaml_spec(tmp_path)
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


def test_dispatch_routes_md_no_subprocess(tmp_path: Path,
                                             monkeypatch) -> None:
    spec = _md_spec(tmp_path, "echo ${task_id}")
    calls: list = []
    monkeypatch.setattr(se.subprocess, "run",
                         lambda *a, **k: calls.append(a))
    rc, rendered = se.dispatch_profile(spec, _lease())
    assert rc == 0
    assert "echo 0279-test" in rendered
    assert calls == [], (
        "0279: MD dispatch must not invoke subprocess.run"
    )
