from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss
from greatminds.cli import task as task_mod
from greatminds.cli.stand_profile import ProfileSpec


def _playbook() -> str:
    return yaml.safe_dump([{
        "name": "deploy",
        "hosts": "localhost",
        "tasks": [{"name": "true", "ansible.builtin.command": "/bin/true"}],
    }])


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path
    coord = project / "coordination"
    profiles = coord / "stand-profiles"
    profiles.mkdir(parents=True)
    runtime = project / ".greatminds"
    runtime.mkdir()
    (profiles / "full-deploy.yaml").write_text(_playbook(), "utf-8")
    (profiles / "vite-dev.yaml").write_text(_playbook(), "utf-8")
    (coord / "stand-profiles.yaml").write_text(
        yaml.safe_dump({
            "profiles": {
                "full-deploy": {
                    "file": "full-deploy.yaml",
                    "purpose": "full product deploy",
                    "environment": "stand",
                    "used_for": ["tester_validation", "production_deploy"],
                    "default_for": ["feature_test", "production_deploy"],
                },
                "vite-dev": {
                    "file": "vite-dev.yaml",
                    "purpose": "live UI",
                    "environment": "stand",
                    "used_for": ["live_ui"],
                    "default_for": ["live_developer"],
                    "restore_profile": "full-deploy",
                },
            },
        }, sort_keys=False),
        "utf-8",
    )
    return project, runtime


def test_verified_hook_enqueues_system_production_deploy(
    tmp_path: Path, monkeypatch,
) -> None:
    project, coord = _project(tmp_path)
    task_id = "0397-promote"
    q = coord / "feature_review"
    q.mkdir()
    for name in ("verified", "intent", ".locks"):
        (coord / name).mkdir()
    (q / f"{task_id}.yaml").write_text(
        yaml.safe_dump({"id": task_id, "kind": "feature", "blocks": []}),
        "utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(task_mod, "_schema_cache", {
        "queues": {
            "feature_review": {"kind": "active"},
            "verified": {"kind": "terminal"},
        },
        "transitions": [],
    })
    monkeypatch.setattr(task_mod, "can_role_move", lambda *a, **k: None)
    monkeypatch.setattr(task_mod, "require_scope_match_on_routing",
                        lambda *a, **k: None)
    monkeypatch.setattr(task_mod, "require_target_readiness",
                        lambda *a, **k: None)
    monkeypatch.setattr(task_mod, "enforce_schema_requires",
                        lambda *a, **k: None)
    monkeypatch.setattr(task_mod, "_worktree_hook_pre_move",
                        lambda *a, **k: None)
    monkeypatch.setattr(task_mod, "_worktree_hook_post_move",
                        lambda *a, **k: None)

    from_q = task_mod._do_move(
        coord, "ARCHITECT-REVIEWER", task_id, "verified", "approved")

    assert from_q == "feature_review"
    state = ss.read_stand_state(project / ".greatminds")
    lease = state["active_lease"]
    assert state["state"] == "preparing"
    assert lease["holder_role"] == "COORDD"
    assert lease["profile"] == "full-deploy"
    assert lease["system_lifecycle"] == "production_deploy"
    assert lease["auto_release_on_deploy_success"] is True
    assert lease["worktree"] == str(project)


def test_system_lifecycle_deploy_auto_releases_after_success(
    tmp_path: Path, monkeypatch,
) -> None:
    project, runtime = _project(tmp_path)
    ss.update_stand_state(project / ".greatminds", lambda state: state.update({
        "state": "preparing",
        "active_lease": {
            "lease_id": "L-prod",
            "task": "0397-promote",
            "worktree": str(project),
            "profile": "full-deploy",
            "profile_file": "full-deploy.yaml",
            "holder_role": "COORDD",
            "ttl_seconds": 14400,
            "auto_release_on_deploy_success": True,
            "system_lifecycle": "production_deploy",
        },
        "queue": [],
    }))
    spec = ProfileSpec(
        name="full-deploy",
        format="yaml",
        path=project / "coordination" / "stand-profiles" / "full-deploy.yaml",
        source="main",
    )
    monkeypatch.setattr(
        "greatminds.cli.stand_profile.load_profile",
        lambda *a, **k: spec,
    )
    monkeypatch.setattr(
        "greatminds.cli.stand_executor.dispatch_profile",
        lambda *a, **k: (0, "ok"),
    )
    monkeypatch.setattr(stand_mod, "cleanup_conflicting_vite_before_deploy",
                        lambda *a, **k: False)

    rc, _log = stand_mod.deploy_lease(project / ".greatminds",
                                      lease_id="L-prod")

    assert rc == 0
    state = ss.read_stand_state(project / ".greatminds")
    assert state["state"] == "free"
    assert state["active_lease"] is None
    assert [h["to"] for h in state["history"][-2:]] == ["ready", "free"]


def test_release_of_displacing_profile_queues_restore_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    project, _runtime = _project(tmp_path)
    ss.update_stand_state(project / ".greatminds", lambda state: state.update({
        "state": "ready",
        "active_lease": {
            "lease_id": "L-live",
            "task": "0397-live",
            "worktree": str(project),
            "profile": "vite-dev",
            "profile_file": "vite-dev.yaml",
            "holder_role": "LIVE-DEVELOPER",
            "ttl_seconds": 14400,
        },
        "queue": [{
            "lease_id": "L-test",
            "task": "0397-test",
            "worktree": str(project),
            "profile": "full-deploy",
            "profile_file": "full-deploy.yaml",
            "holder_role": "TESTER",
            "ttl_seconds": 14400,
        }],
    }))
    monkeypatch.chdir(project)
    monkeypatch.setenv("GREATMINDS_ROLE", "LIVE-DEVELOPER")
    monkeypatch.setattr(stand_mod, "_teardown_lease",
                        lambda *a, **k: None)

    result = CliRunner().invoke(stand_mod.stand, [
        "release", "--lease-id", "L-live", "--result", "pass",
    ])

    assert result.exit_code == 0, result.output + str(result.exception)
    state = ss.read_stand_state(project / ".greatminds")
    lease = state["active_lease"]
    assert state["state"] == "preparing"
    assert lease["holder_role"] == "COORDD"
    assert lease["profile"] == "full-deploy"
    assert lease["system_lifecycle"] == "restore_profile"
    assert lease["auto_release_on_deploy_success"] is True
    assert [q["lease_id"] for q in state["queue"]] == ["L-test"]
