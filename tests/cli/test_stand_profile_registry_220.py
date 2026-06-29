from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import setup as setup_mod
from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_profile_registry as reg
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


def _playbook() -> str:
    return yaml.safe_dump([{
        "name": "smoke",
        "hosts": "localhost",
        "tasks": [{"name": "true", "ansible.builtin.command": "/bin/true"}],
    }])


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    coord.mkdir()
    (coord / "stand-profiles").mkdir()
    runtime = tmp_path / ".greatminds"
    runtime.mkdir()
    return coord


def _write_registry(coord: Path, body: dict) -> None:
    (coord / "stand-profiles.yaml").write_text(
        yaml.safe_dump(body, sort_keys=False),
        encoding="utf-8",
    )


def test_setup_seeds_project_stand_profile_registry(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    status = setup_mod._seed_stand_profile_registry(coord, find_canon_dir())
    assert status == "written"
    registry = reg.load_registry(coord)
    assert set(registry.profiles) == {"smoke-only", "full-deploy", "vite-dev"}
    assert registry.profiles["full-deploy"].default_for == (
        "feature_test", "explorer", "reviewer", "production_deploy",
    )
    assert registry.profiles["vite-dev"].restore_profile == "full-deploy"


def test_registry_maps_profile_name_to_different_yaml_file(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "full.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "full-deploy": {
                "file": "full.yaml",
                "purpose": "full validation",
                "environment": "stand",
                "used_for": ["tester_validation", "reviewer_validation"],
                "default_for": ["feature_test", "reviewer"],
            }
        }
    })
    registry = reg.load_registry(coord)
    assert registry.require("full-deploy").file == "full.yaml"
    errors, warnings = reg.doctor_registry(coord)
    assert errors == []
    assert warnings == []


def test_registry_rejects_unknown_used_for_token(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _write_registry(coord, {
        "profiles": {
            "x": {
                "file": "x.yaml",
                "purpose": "bad token",
                "used_for": ["whatever"],
                "default_for": ["feature_test"],
            }
        }
    })
    with pytest.raises(GreatMindsError) as exc:
        reg.load_registry(coord)
    assert "unknown used_for" in str(exc.value)


def test_registry_rejects_duplicate_default_tokens(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _write_registry(coord, {
        "profiles": {
            "a": {
                "file": "a.yaml",
                "purpose": "first",
                "used_for": ["tester_validation"],
                "default_for": ["feature_test"],
            },
            "b": {
                "file": "b.yaml",
                "purpose": "second",
                "used_for": ["reviewer_validation"],
                "default_for": ["feature_test"],
            },
        }
    })
    with pytest.raises(GreatMindsError) as exc:
        reg.load_registry(coord)
    assert "claimed by both" in str(exc.value)


def test_registry_rejects_unknown_restore_profile(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _write_registry(coord, {
        "profiles": {
            "vite-dev": {
                "file": "vite-dev.yaml",
                "purpose": "live UI",
                "used_for": ["live_ui"],
                "default_for": ["live_developer"],
                "restore_profile": "full-deploy",
            }
        }
    })
    with pytest.raises(GreatMindsError) as exc:
        reg.load_registry(coord)
    assert "restore_profile" in str(exc.value)
    assert "not registered" in str(exc.value)


def test_stand_profiles_doctor_reports_missing_playbook(tmp_path: Path, monkeypatch) -> None:
    coord = _coord(tmp_path)
    _write_registry(coord, {
        "profiles": {
            "full-deploy": {
                "file": "full.yaml",
                "purpose": "full validation",
                "used_for": ["tester_validation"],
                "default_for": ["feature_test"],
            }
        }
    })
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(stand_mod.stand, ["profiles", "doctor"])
    assert result.exit_code != 0
    assert "full-deploy" in result.output
    assert "full.yaml" in result.output


def test_stand_profiles_list_shows_registry_entries(tmp_path: Path, monkeypatch) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "full.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "full-deploy": {
                "file": "full.yaml",
                "purpose": "full validation",
                "environment": "stand",
                "used_for": ["tester_validation"],
                "default_for": ["feature_test"],
            }
        }
    })
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(stand_mod.stand, ["profiles", "list"])
    assert result.exit_code == 0
    assert "full-deploy" in result.output
    assert "file=full.yaml" in result.output


def test_lease_rejects_unregistered_profile_even_when_file_exists(
    tmp_path: Path, monkeypatch,
) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "rogue.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "full-deploy": {
                "file": "rogue.yaml",
                "purpose": "registered",
                "used_for": ["tester_validation"],
                "default_for": ["feature_test"],
            }
        }
    })
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    result = CliRunner().invoke(stand_mod.stand, [
        "lease", "--task", "0099-task", "--worktree", str(wt),
        "--profile", "rogue",
    ])
    assert result.exit_code != 0
    assert "not registered" in (result.output + str(result.exception))


def test_production_profile_policy_is_valid_registry_data(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "production.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "production": {
                "file": "production.yaml",
                "purpose": "Production deployment and post-deploy verification",
                "environment": "production",
                "requires_explicit_user_approval": True,
                "allowed_roles": ["ARCHITECT-REVIEWER", "MAINTAINER"],
                "used_for": ["production_deploy", "production_post_deploy_review"],
                "default_for": ["production_deploy", "production_review"],
            }
        }
    })
    registry = reg.load_registry(coord)
    entry = registry.require("production")
    assert entry.environment == "production"
    assert entry.requires_explicit_user_approval is True
    assert entry.allowed_roles == ("ARCHITECT-REVIEWER", "MAINTAINER")


def test_production_profile_rejects_unlisted_holder_role(
    tmp_path: Path, monkeypatch,
) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "production.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "production": {
                "file": "production.yaml",
                "purpose": "Production deployment",
                "environment": "production",
                "requires_explicit_user_approval": True,
                "allowed_roles": ["ARCHITECT-REVIEWER", "MAINTAINER"],
                "used_for": ["production_deploy"],
                "default_for": ["production_deploy"],
            }
        }
    })
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    result = CliRunner().invoke(stand_mod.stand, [
        "lease", "--task", "0099-task", "--worktree", str(wt),
        "--profile", "production", "--profile-approval", "USER_APPROVED",
    ])
    out = result.output + str(result.exception)
    assert result.exit_code != 0
    assert "may only be leased by" in out


def test_production_profile_requires_explicit_approval_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "production.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "production": {
                "file": "production.yaml",
                "purpose": "Production deployment",
                "environment": "production",
                "requires_explicit_user_approval": True,
                "allowed_roles": ["ARCHITECT-REVIEWER", "MAINTAINER"],
                "used_for": ["production_deploy"],
                "default_for": ["production_deploy"],
            }
        }
    })
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")
    result = CliRunner().invoke(stand_mod.stand, [
        "lease", "--task", "0099-task", "--worktree", str(wt),
        "--profile", "production",
    ])
    out = result.output + str(result.exception)
    assert result.exit_code != 0
    assert "--profile-approval USER_APPROVED" in out


def test_production_profile_lease_succeeds_with_policy_satisfied(
    tmp_path: Path, monkeypatch,
) -> None:
    coord = _coord(tmp_path)
    (coord / "stand-profiles" / "production.yaml").write_text(_playbook(), "utf-8")
    _write_registry(coord, {
        "profiles": {
            "production": {
                "file": "production.yaml",
                "purpose": "Production deployment",
                "environment": "production",
                "requires_explicit_user_approval": True,
                "allowed_roles": ["ARCHITECT-REVIEWER", "MAINTAINER"],
                "used_for": ["production_deploy"],
                "default_for": ["production_deploy"],
            }
        }
    })
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")
    result = CliRunner().invoke(stand_mod.stand, [
        "lease", "--task", "0099-task", "--worktree", str(wt),
        "--profile", "production", "--profile-approval", "USER_APPROVED",
    ])
    assert result.exit_code == 0, result.output + str(result.exception)
    state = yaml.safe_load(
        (tmp_path / ".greatminds" / ".stand" / "state.yaml").read_text("utf-8")
    )
    lease = state["active_lease"]
    assert lease["profile"] == "production"
    assert lease["profile_file"] == "production.yaml"
    assert lease["profile_approval"] == "explicit-user-approval"
