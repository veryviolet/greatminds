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


def test_md_prereq_prepends_notice_to_rendered(tmp_path: Path) -> None:
    """Spec opts in → rendered content starts with the canonical
    PREREQUISITES ONLY notice, the body follows after a blank line."""
    spec = _md_spec(tmp_path,
                     content="Deploy step 1.\nDeploy step 2.\n",
                     prereq_only=True)
    rc, rendered = se.execute_md_profile(spec, _lease())
    assert rc == 0
    assert rendered.startswith("**Mode: PREREQUISITES ONLY**")
    assert "Deploy step 1." in rendered  # body still present


def test_md_no_prereq_no_notice(tmp_path: Path) -> None:
    spec = _md_spec(tmp_path, content="body\n", prereq_only=False)
    _, rendered = se.execute_md_profile(spec, _lease())
    assert not rendered.startswith("**Mode: PREREQUISITES ONLY**")


def test_md_lease_override_true_injects_notice(tmp_path: Path) -> None:
    """Spec is False but the lease carries True → notice STILL
    injected (CLI flag wins)."""
    spec = _md_spec(tmp_path, content="body\n", prereq_only=False)
    _, rendered = se.execute_md_profile(
        spec, _lease(deploy_prerequisites_only=True))
    assert "PREREQUISITES ONLY" in rendered


def test_md_lease_override_false_strips_notice(tmp_path: Path) -> None:
    """Spec is True but the lease carries False → no notice."""
    spec = _md_spec(tmp_path, content="body\n", prereq_only=True)
    _, rendered = se.execute_md_profile(
        spec, _lease(deploy_prerequisites_only=False))
    assert "PREREQUISITES ONLY" not in rendered


def test_md_notice_mentions_tester_handoff(tmp_path: Path) -> None:
    """The notice must tell SK to call ``stand ready`` and hand off
    to TESTER — otherwise the LLM might try to do the full deploy
    after the prereqs."""
    spec = _md_spec(tmp_path, content="x\n", prereq_only=True)
    _, rendered = se.execute_md_profile(spec, _lease())
    assert "stand ready" in rendered
    assert "TESTER" in rendered


# ---------- lease CLI flag persists into active_lease ----------


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
    (project / ".worktrees" / "0283").mkdir(parents=True)
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
