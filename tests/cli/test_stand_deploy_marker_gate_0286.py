"""Tests for task 0286: SK deploy bypass closure.

Three pieces that together kill the "SK marks ready without
running ansible" foot-gun:

  1. ``execute_yaml_profile`` calls ``is_deploy_safe`` and refuses
     when the worktree+host combination would self-modify (rc=126
     + marker captures the refusal).
  2. ``execute_yaml_profile`` / ``execute_md_profile`` drop a
     deploy marker at ``<coord>/.stand/deploy-<lease_id>.log``.
  3. ``greatminds stand ready --lease-id X`` refuses with
     exit_code=2 if no marker exists for the lease.
"""
from __future__ import annotations

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


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / ".stand").mkdir(parents=True)
    (tmp_path / "proj" / ".worktrees" / "0286").mkdir(parents=True)
    return coord


def _yaml_spec(tmp_path: Path) -> ProfileSpec:
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump({
        "name": "x", "hosts": "stand", "tasks": [{"name": "y"}],
    }), encoding="utf-8")
    return ProfileSpec(name="x", format="yaml", path=p,
                        yaml_data=None,
                        deploy_prerequisites_only=False)


def _md_spec(tmp_path: Path) -> ProfileSpec:
    p = tmp_path / "spec.md"
    p.write_text("body", encoding="utf-8")
    return ProfileSpec(name="m", format="md", path=p,
                        md_content="body",
                        deploy_prerequisites_only=False)


def _lease(coord: Path, project_dir: Path,
           lease_id: str = "lease-0286",
           host: str = "avatar",
           worktree_subdir: str = "0286",
           **extras) -> dict:
    base = {
        "lease_id": lease_id,
        "task_id": "0286-probe",
        "worktree": str(project_dir / ".worktrees" / worktree_subdir),
        "host": host,
        "user": "deploy",
        "deploy_path": "/srv/stand",
        "coord": str(coord),  # 0286: tell executor where to write marker
    }
    base.update(extras)
    return base


# ---------- marker path helper ----------


def test_deploy_marker_path_under_dot_stand(tmp_path: Path) -> None:
    coord = tmp_path / "c"
    p = se.deploy_marker_path(coord, "abc123")
    assert p == coord / ".stand" / "deploy-abc123.log"


# ---------- is_deploy_safe wired in execute_yaml_profile ----------


def test_yaml_refuses_when_unsafe_writes_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    """A main-tree + localhost lease (the self-modify trap) must
    NOT reach the subprocess. The marker captures the refusal so
    operators can see WHY in stand status."""
    coord = _coord(tmp_path)
    project = tmp_path / "proj"
    spec = _yaml_spec(tmp_path)
    lease = _lease(coord, project,
                    worktree_subdir=".",  # → /proj (main tree)
                    host="localhost")
    # Patch worktree to the project root explicitly.
    lease["worktree"] = str(project)

    # subprocess.run must NOT be called.
    monkeypatch.setattr(se.subprocess, "run",
                         lambda *a, **k: pytest.fail(
                             "0286: unsafe deploy must NOT reach subprocess"))
    monkeypatch.setattr(se.shutil, "which",
                         lambda _: "/fake/bin/ansible-playbook")

    rc, log = se.execute_yaml_profile(spec, lease)
    assert rc == 126
    assert "is_deploy_safe" in log
    marker = se.deploy_marker_path(coord, "lease-0286")
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert "rc=126" in text
    assert "is_deploy_safe" in text


def test_yaml_safe_deploy_runs_subprocess_and_writes_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    """Isolated worktree → safe → subprocess runs → marker written
    with rc + captured log."""
    coord = _coord(tmp_path)
    project = tmp_path / "proj"
    spec = _yaml_spec(tmp_path)
    lease = _lease(coord, project)

    monkeypatch.setattr(se.shutil, "which",
                         lambda _: "/fake/bin/ansible-playbook")
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="ok\n", stderr=""),
    )

    rc, log = se.execute_yaml_profile(spec, lease)
    assert rc == 0
    marker = se.deploy_marker_path(coord, "lease-0286")
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert "rc=0" in text
    assert "ok" in text
    assert "host=avatar" in text


def test_yaml_subprocess_failure_still_writes_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    """Even a nonzero ansible run must drop the marker — `stand
    ready` then refuses on the rc, not on missing evidence."""
    coord = _coord(tmp_path)
    project = tmp_path / "proj"
    spec = _yaml_spec(tmp_path)
    lease = _lease(coord, project)

    monkeypatch.setattr(se.shutil, "which",
                         lambda _: "/fake/bin/ansible-playbook")
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            args=cmd, returncode=3, stdout="", stderr="oops"),
    )

    rc, _ = se.execute_yaml_profile(spec, lease)
    assert rc == 3
    marker = se.deploy_marker_path(coord, "lease-0286")
    assert marker.is_file()
    assert "rc=3" in marker.read_text(encoding="utf-8")


def _stand_ready_project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    state = {
        "state": "preparing",
        "active_lease": {
            "lease_id": "lease-0286",
            "task": "0286-probe",
            "worktree": str(project / ".worktrees" / "0286"),
            "profile": "full-deploy",
            "holder_role": "TESTER",
            "ttl_seconds": 14400,
            "granted_at": "2026-05-27T12:00:00Z",
        },
        "queue": [],
        "last_state_change_at": "2026-05-27T12:00:00Z",
        "last_state_change_by": "TESTER",
        "down_reason": None,
        "history": [],
    }
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(state), encoding="utf-8",
    )
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.setenv("GREATMINDS_ROLE", "STAND-KEEPER")
    monkeypatch.chdir(project)
    return project


def test_stand_ready_refuses_without_marker(tmp_path: Path,
                                              monkeypatch) -> None:
    """The motivating bug: SK calling ``stand ready`` without first
    running the executor. Pre-0286 the transition went through;
    post-0286 it must fail with the actionable message."""
    project = _stand_ready_project(tmp_path, monkeypatch)
    monkeypatch.setattr(stand_mod, "_file_inbox_info",
                         lambda *a, **kw: None)

    result = CliRunner().invoke(stand_mod.stand, [
        "ready", "--lease-id", "lease-0286",
    ])
    assert result.exit_code != 0
    msg = result.output + (str(result.exception)
                            if result.exception else "")
    assert "deploy marker" in msg
    assert "execute_yaml_profile" in msg or "dispatch_profile" in msg

    # State.yaml unchanged.
    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "preparing"


def test_stand_ready_accepts_when_marker_present(tmp_path: Path,
                                                    monkeypatch) -> None:
    """With the marker in place, the transition completes normally
    (preserves the 0244 behavior + the 0286 evidence requirement
    additively)."""
    project = _stand_ready_project(tmp_path, monkeypatch)
    monkeypatch.setattr(stand_mod, "_file_inbox_info",
                         lambda *a, **kw: None)

    # Seed the marker as if the executor had run.
    marker = se.deploy_marker_path(project / "coordination",
                                    "lease-0286")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rc=0\n---log---\nok\n", encoding="utf-8")

    result = CliRunner().invoke(stand_mod.stand, [
        "ready", "--lease-id", "lease-0286",
    ])
    assert result.exit_code == 0, result.output

    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "ready"


# The "dispatch_profile before stand ready" rule is pinned in schema
# (roles.STAND-KEEPER.event_triggers.on_lease_preparing) and ENFORCED by
# the deploy-marker gate exercised by the tests above; the per-role
# prose doc that restated it is gone (system prompt = static bootstrap).
