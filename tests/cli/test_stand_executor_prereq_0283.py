"""Tests for task 0283 (0276 Phase G): deploy_prerequisites_only
mode in stand executor + lease-CLI override.

YAML path: ``--tags prerequisite`` already wired in Phase C; Phase G
adds the lease-level override (CLI flag wins over spec value).

MD path: when the flag is true, prepend a machine-readable notice
so the LLM sees the mode switch as the first line of its prompt
context.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand_executor as se
from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss
from greatminds.cli.stand_profile import ProfileSpec


@pytest.fixture(autouse=True)
def _resolvable_presets(monkeypatch):
    """Lease-CLI tests isolate state writes from profile file IO."""
    from greatminds.core.errors import GreatMindsError
    from greatminds.cli.stand_profile_registry import (
        ProfileEntry, ProfileRegistry,
    )

    class _Spec:
        format = "yaml"

    def _fake_registry(coord, **_kw):
        entries = {
            name: ProfileEntry(
                name=name,
                file=f"{name}.yaml",
                purpose=f"{name} test profile",
                used_for=("tester_validation",),
                default_for=(),
            )
            for name in {"full-deploy", "vite-dev", "smoke-only"}
        }
        return ProfileRegistry(
            path=Path(coord) / "stand-profiles.yaml",
            source="test",
            profiles=entries,
        )

    def _fake(_coord, name, **_kw):
        if name in {"full-deploy", "vite-dev", "smoke-only"}:
            return _Spec()
        raise GreatMindsError(f"profile {name!r} has no file")

    monkeypatch.setattr(
        "greatminds.cli.stand_profile_registry.load_registry",
        _fake_registry,
    )
    monkeypatch.setattr("greatminds.cli.stand_profile.load_profile", _fake)


# ---------- fixtures ----------


def _yaml_spec(tmp_path: Path,
                prereq_only: bool = False) -> ProfileSpec:
    path = tmp_path / "stand-profiles" / "full-deploy.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump({
            "name": "full-deploy",
            "hosts": "stand",
            "tasks": [
                {"name": "prep", "ansible.builtin.ping": None,
                 "tags": ["prerequisite"]},
                {"name": "deploy", "ansible.builtin.command": "true"},
            ],
        }),
        encoding="utf-8",
    )
    return ProfileSpec(
        name="full-deploy",
        format="yaml",
        path=path,
        deploy_prerequisites_only=prereq_only,
    )


def _md_spec(tmp_path: Path,
              content: str = "# deploy\nrun ${task_id}\n",
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


def _lease(**extras) -> dict:
    base = {
        "lease_id": "uuid",
        "task_id": "0283-probe",
        "worktree": "/opt/greatminds/.worktrees/0283",
        "host": "avatar",
        "user": "deploy",
        "deploy_path": "/srv/stand",
    }
    base.update(extras)
    return base


# ---------- YAML: lease-level override ----------


def _capture_argv(monkeypatch) -> list:
    captured: list = []
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(se.subprocess, "run", fake_run)
    return captured


def test_yaml_lease_override_true_adds_tag_even_when_spec_false(
    tmp_path: Path, monkeypatch,
) -> None:
    """The lease flag wins over a spec that opts OUT — i.e. a fresh
    lease can force prereq-only even if the profile authored it as a
    full deploy."""
    spec = _yaml_spec(tmp_path, prereq_only=False)
    cmd = _capture_argv(monkeypatch)
    se.execute_yaml_profile(
        spec, _lease(deploy_prerequisites_only=True))
    assert cmd, "subprocess.run must be reached"
    argv = cmd[0]
    assert "--tags" in argv
    assert se.PREREQ_TAG in argv


def test_yaml_lease_override_false_strips_spec_default(
    tmp_path: Path, monkeypatch,
) -> None:
    """The lease can ALSO override a spec that opts IN — explicit
    false on the lease defeats the spec's True value. Operators
    who want a one-off full-deploy after the warmup leases ran can
    pass ``--no-...`` (CLI passes False explicitly)."""
    spec = _yaml_spec(tmp_path, prereq_only=True)
    cmd = _capture_argv(monkeypatch)
    se.execute_yaml_profile(
        spec, _lease(deploy_prerequisites_only=False))
    argv = cmd[0]
    assert "--tags" not in argv


def test_yaml_no_lease_value_falls_back_to_spec(
    tmp_path: Path, monkeypatch,
) -> None:
    """When the lease doesn't carry the flag at all, the spec's
    value still drives the decision (backwards-compat with leases
    pre-0283)."""
    spec = _yaml_spec(tmp_path, prereq_only=True)
    cmd = _capture_argv(monkeypatch)
    se.execute_yaml_profile(spec, _lease())  # no flag in lease
    argv = cmd[0]
    assert "--tags" in argv and se.PREREQ_TAG in argv


# ---------- MD: prereq notice injection ----------


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump({"state": "free"}), encoding="utf-8",
    )
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    monkeypatch.chdir(project)
    # Worktree path the validator accepts.
    wt = project / ".worktrees" / "0283"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    return project


def test_lease_cli_flag_persists_into_active_lease(
    tmp_path: Path, monkeypatch,
) -> None:
    """``greatminds stand lease --deploy-prerequisites-only`` sets
    ``active_lease.deploy_prerequisites_only=true`` in state.yaml."""
    project = _project(tmp_path, monkeypatch)
    wt = project / ".worktrees" / "0283"

    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "lease",
        "--task", "0283-probe",
        "--worktree", str(wt),
        "--profile", "full-deploy",
        "--deploy-prerequisites-only",
    ])
    assert result.exit_code == 0, result.output

    state = ss.read_stand_state(project / "coordination")
    assert state.get("state") == "preparing"
    assert state["active_lease"]["deploy_prerequisites_only"] is True


def test_lease_cli_without_flag_omits_field(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without the flag, the active_lease record stays minimal — no
    ``deploy_prerequisites_only`` key at all (executor falls back to
    spec value)."""
    project = _project(tmp_path, monkeypatch)
    wt = project / ".worktrees" / "0283"

    runner = CliRunner()
    runner.invoke(stand_mod.stand, [
        "lease",
        "--task", "0283-probe",
        "--worktree", str(wt),
        "--profile", "full-deploy",
    ])
    state = ss.read_stand_state(project / "coordination")
    assert "deploy_prerequisites_only" not in state["active_lease"]
