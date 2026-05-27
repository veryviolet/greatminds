"""Tests for task 0289: ``stand release`` / ``stand down`` /
``stand up`` must nullify ``active_lease`` in state.yaml on
transition.

Pre-0289 only ``stand release`` cleared ``active_lease`` correctly.
``stand down`` left the orphan record alongside state=down, and
``stand up`` did not double-check on the down→free transition —
both let stale lease metadata leak forward and confuse subsequent
ticks (TESTER could read an "active lease" while state=free).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss


def _make_active_lease(holder_role: str = "TESTER",
                        lease_id: str = "lease-0289") -> dict:
    return {
        "lease_id": lease_id,
        "task": "0289-probe",
        "worktree": "/opt/greatminds/.worktrees/0289",
        "profile": "full-deploy",
        "holder_role": holder_role,
        "ttl_seconds": 14400,
        "enqueued_at": "2026-05-27T00:00:00Z",
        "granted_at": "2026-05-27T00:01:00Z",
    }


def _project(tmp_path: Path, monkeypatch, *,
              state: str, active_lease: dict | None) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    payload = {
        "state": state,
        "active_lease": active_lease,
        "queue": [],
        "last_state_change_at": "2026-05-27T00:00:00Z",
        "last_state_change_by": "TESTER",
        "down_reason": None,
        "history": [],
    }
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8",
    )
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.chdir(project)
    return project


# ---------- stand_release nullifies active_lease ----------


def test_release_clears_active_lease_on_pass(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, monkeypatch, state="ready",
                        active_lease=_make_active_lease())
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "release", "--lease-id", "lease-0289", "--result", "pass",
    ])
    assert result.exit_code == 0, result.output
    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "free"
    assert state["active_lease"] is None


def test_release_clears_active_lease_on_fail(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, monkeypatch, state="ready",
                        active_lease=_make_active_lease())
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "release", "--lease-id", "lease-0289", "--result", "fail",
    ])
    assert result.exit_code == 0
    state = ss.read_stand_state(project / "coordination")
    assert state["active_lease"] is None


# ---------- stand_down nullifies active_lease ----------


def test_down_clears_active_lease(
    tmp_path: Path, monkeypatch,
) -> None:
    """0289 contract: ``stand down`` MUST set active_lease to None.
    Pre-0289 the orphan lease record stayed put alongside state=down
    and tripped up SK's later diagnostics."""
    project = _project(tmp_path, monkeypatch, state="preparing",
                        active_lease=_make_active_lease())
    monkeypatch.setenv("GREATMINDS_ROLE", "STAND-KEEPER")
    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "down", "--reason", "deploy failed: ansible exit 3",
    ])
    assert result.exit_code == 0, result.output
    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "down"
    assert state["active_lease"] is None
    assert state.get("down_reason") == "deploy failed: ansible exit 3"


def test_down_clears_active_lease_from_ready(
    tmp_path: Path, monkeypatch,
) -> None:
    """``stand down`` from ready state — the lease was already
    handed off but a post-ready infra event takes the stand down;
    lease record should still be cleared."""
    project = _project(tmp_path, monkeypatch, state="ready",
                        active_lease=_make_active_lease())
    monkeypatch.setenv("GREATMINDS_ROLE", "STAND-KEEPER")
    runner = CliRunner()
    runner.invoke(stand_mod.stand,
                   ["down", "--reason", "post-ready infra incident"])
    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "down"
    assert state["active_lease"] is None


# ---------- stand_up nullifies active_lease (defense in depth) ----------


def test_up_clears_active_lease(
    tmp_path: Path, monkeypatch,
) -> None:
    """``stand up`` must also clear active_lease as a defense in
    depth — older state files written before 0289 could land in
    down state with an orphan still attached."""
    # Hand-craft a state.yaml that simulates the pre-0289 bug:
    # state=down + active_lease populated.
    project = _project(tmp_path, monkeypatch, state="down",
                        active_lease=_make_active_lease())
    monkeypatch.setenv("GREATMINDS_ROLE", "STAND-KEEPER")
    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "up", "--reason", "infra fixed",
    ])
    assert result.exit_code == 0, result.output
    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "free"
    assert state["active_lease"] is None
    assert state.get("down_reason") is None


# ---------- history preserved ----------


def test_release_preserves_history(
    tmp_path: Path, monkeypatch,
) -> None:
    """Audit-trail safety net: nullifying active_lease must not wipe
    the transition history list."""
    project = _project(tmp_path, monkeypatch, state="ready",
                        active_lease=_make_active_lease())
    coord = project / "coordination"
    # Seed an old transition.
    state = ss.read_stand_state(coord)
    state.setdefault("history", []).append({
        "from": "preparing", "to": "ready", "by": "STAND-KEEPER",
        "at": "2026-05-27T00:02:00Z",
        "reason": "deploy ok",
    })
    (coord / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(state), encoding="utf-8")

    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    CliRunner().invoke(stand_mod.stand, [
        "release", "--lease-id", "lease-0289", "--result", "pass",
    ])

    state = ss.read_stand_state(coord)
    assert state["active_lease"] is None
    history = state.get("history") or []
    assert any(h.get("reason") == "deploy ok" for h in history), (
        "0289: clearing active_lease must not wipe history"
    )


# ---------- queue handling preserved ----------


def test_release_does_not_clear_queued_leases(
    tmp_path: Path, monkeypatch,
) -> None:
    """Releasing the active lease must not touch the FIFO queue —
    the next lease becomes active on a subsequent SK tick."""
    project = _project(tmp_path, monkeypatch, state="ready",
                        active_lease=_make_active_lease())
    coord = project / "coordination"
    state = ss.read_stand_state(coord)
    state["queue"] = [
        _make_active_lease(holder_role="EXPLORER",
                            lease_id="next-lease")
    ]
    (coord / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(state), encoding="utf-8")

    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    CliRunner().invoke(stand_mod.stand, [
        "release", "--lease-id", "lease-0289", "--result", "pass",
    ])

    state = ss.read_stand_state(coord)
    assert state["active_lease"] is None
    queue = state.get("queue") or []
    assert len(queue) == 1
    assert queue[0]["lease_id"] == "next-lease"
