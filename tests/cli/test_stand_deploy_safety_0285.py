"""Tests for task 0285: SK deploy safety distinguishes remote /
isolated worktrees from self-modify-on-main-tree.

Pre-0285 SK's whitelist refused all deploys when it saw the main
fleet tree, regardless of target host — so a fully-valid lease
with ``worktree=.worktrees/<id>`` + ``STAND_HOST=avatar`` was
shorted to ``ready`` without ansible-playbook ever running.

0285 adds ``is_deploy_safe(worktree, host, project_dir)`` to the
executor module + clears the stale ``down_reason`` on every
free→preparing transition so a prior incident doesn't poison the
next lease.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand_executor as se
from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss


@pytest.fixture(autouse=True)
def _resolvable_presets(monkeypatch):
    """Lease validates --profile by resolving a file in stand-profiles/;
    these tests don't seed files, so stub the resolver (presets resolve,
    anything else raises like a missing profile file)."""
    from greatminds.core.errors import GreatMindsError

    class _Spec:
        format = "yaml"

    def _fake(_coord, name, **_kw):
        if name in {"full-deploy", "vite-dev", "smoke-only"}:
            return _Spec()
        raise GreatMindsError(f"profile {name!r} has no file")

    monkeypatch.setattr("greatminds.cli.stand_profile.load_profile", _fake)


# ---------- is_deploy_safe ----------


def test_isolated_worktree_always_safe(tmp_path: Path) -> None:
    """``project/.worktrees/<seq>`` paths are always safe — even
    with localhost target — because the worktree is isolated from
    the running fleet checkout."""
    project = tmp_path / "proj"
    wt = project / ".worktrees" / "0285"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")

    for host in ("avatar", "", "localhost", "127.0.0.1", None):
        safe, reason = se.is_deploy_safe(wt, host, project)
        assert safe is True, (
            f"0285: isolated worktree must be safe for host={host!r} "
            f"(reason: {reason})"
        )


def test_main_tree_localhost_is_self_modify_trap(tmp_path: Path) -> None:
    """The motivating bug for 0271 + 0285: main tree + localhost
    target = deploying onto the running host's own checkout. Refuse."""
    project = tmp_path / "proj"
    project.mkdir()
    for host in ("", "localhost", "127.0.0.1", "::1", None):
        safe, reason = se.is_deploy_safe(project, host, project)
        assert safe is False, (
            f"0285: main-tree + host={host!r} must be unsafe"
        )
        assert "self-modify" in reason


def test_main_tree_remote_host_is_safe(tmp_path: Path) -> None:
    """0285 contract: ``worktree==main tree`` + ``STAND_HOST=avatar``
    is a legitimate remote deploy — nothing local is modified."""
    project = tmp_path / "proj"
    project.mkdir()
    safe, reason = se.is_deploy_safe(project, "avatar", project)
    assert safe is True, (
        f"0285: main-tree + remote host must be safe (reason: {reason})"
    )
    assert "avatar" in reason


def test_unknown_worktree_location_refused(tmp_path: Path) -> None:
    """A worktree path that's neither under ``.worktrees`` nor equal
    to the project_dir is suspicious — refuse so the operator has
    to be explicit."""
    project = tmp_path / "proj"
    project.mkdir()
    stranger = tmp_path / "elsewhere"
    stranger.mkdir()
    safe, reason = se.is_deploy_safe(stranger, "avatar", project)
    assert safe is False
    assert "unknown worktree" in reason.lower()


def test_local_hosts_set_normalizes_whitespace_and_case() -> None:
    """Defensive: ``STAND_HOST`` from PROJECT.env can carry stray
    whitespace / capitalization (``Localhost``, ``LOCALHOST``,
    ``  ``); they all count as local."""
    project = Path("/tmp/x")  # path doesn't have to exist for the check
    main = project
    for host in ("LOCALHOST", " localhost ", "Localhost"):
        safe, reason = se.is_deploy_safe(main, host, project)
        assert safe is False, (
            f"0285: variant {host!r} must normalize to local"
        )


# ---------- lease clears stale down_reason ----------


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    state = {
        "state": "free",
        "active_lease": None,
        "queue": [],
        "last_state_change_at": None,
        "last_state_change_by": "STAND-KEEPER",
        # Stale down_reason from a prior incident — must be cleared
        # when the new lease lands.
        "down_reason": "deploy_blocked_self_restart_required",
        "history": [],
    }
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(state), encoding="utf-8",
    )
    wt = project / ".worktrees" / "0285"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    monkeypatch.chdir(project)
    return project


def test_lease_clears_stale_down_reason_on_free_to_preparing(
    tmp_path: Path, monkeypatch,
) -> None:
    """0285: a fresh lease landing on state=free must blank out the
    ``down_reason`` field. Pre-0285 it stayed populated and SK's
    diagnostic loop short-circuited every subsequent deploy."""
    project = _project(tmp_path, monkeypatch)
    wt = project / ".worktrees" / "0285"

    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "lease",
        "--task", "0285-probe",
        "--worktree", str(wt),
        "--profile", "full-deploy",
    ])
    assert result.exit_code == 0, result.output

    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "preparing"
    assert state.get("down_reason") is None, (
        f"0285: lease must clear stale down_reason (got "
        f"{state.get('down_reason')!r})"
    )


def test_lease_clear_does_not_disturb_history(
    tmp_path: Path, monkeypatch,
) -> None:
    """The history list (audit log of state transitions) must NOT
    be wiped by the down_reason reset — only the live field is
    cleared."""
    project = _project(tmp_path, monkeypatch)
    coord = project / "coordination"
    # Seed a history entry.
    state = ss.read_stand_state(coord)
    state.setdefault("history", []).append(
        {"from": "ready", "to": "down", "by": "STAND-KEEPER",
         "at": "2026-05-27T00:00:00Z", "reason": "old failure"}
    )
    (coord / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(state), encoding="utf-8")

    wt = project / ".worktrees" / "0285"
    runner = CliRunner()
    runner.invoke(stand_mod.stand, [
        "lease",
        "--task", "0285-probe",
        "--worktree", str(wt),
        "--profile", "full-deploy",
    ])

    state = ss.read_stand_state(coord)
    history = state.get("history") or []
    assert any(
        h.get("reason") == "old failure" for h in history
    ), "0285: down_reason clear must preserve history audit trail"
