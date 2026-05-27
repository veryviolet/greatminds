"""Tests for task 0291: SK auto-notifies PLANNER inbox on
``stand down``.

Pre-0291 SK silently flipped state.yaml to ``down`` and idled;
PLANNER had to poll the state file to discover incidents. 0291
makes the notification automatic: SK files an inbox-info to the
schema-declared target (``ARCHITECT-PLANNER`` by default) with the
``down_reason`` + lease_id.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.core.paths import find_canon_dir


def _project(tmp_path: Path, monkeypatch, *,
              active_lease: dict | None = None) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coordination" / "inbox" / "architect-planner").mkdir(
        parents=True,
    )
    state = {
        "state": "preparing",
        "active_lease": active_lease,
        "queue": [],
        "last_state_change_at": "2026-05-27T00:00:00Z",
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


# ---------- schema source-of-truth ----------


def test_schema_declares_stand_keeper_notifications() -> None:
    """``schema.stand_keeper.notifications.on_down`` must default to
    ARCHITECT-PLANNER so SK's helper resolves the target without a
    hardcoded role name in the Python code."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    sk = doc.get("stand_keeper") or {}
    assert sk.get("notifications", {}).get("on_down") == "ARCHITECT-PLANNER"


def test_helper_returns_planner_for_on_down() -> None:
    """The Python helper that drives the auto-notify branch must
    resolve to ARCHITECT-PLANNER for on_down (schema-driven)."""
    target = stand_mod._stand_keeper_notification_target("on_down")
    assert target == "ARCHITECT-PLANNER"


def test_helper_returns_none_for_unknown_event() -> None:
    """An event name the schema doesn't declare → None; SK silently
    skips the send (graceful degradation, never crash)."""
    assert stand_mod._stand_keeper_notification_target("on_zoom") is None


# ---------- stand_down auto-notifies ----------


def test_stand_down_files_inbox_info_to_planner(
    tmp_path: Path, monkeypatch,
) -> None:
    """The canonical flow: SK calls ``stand down --reason X`` → an
    inbox info lands under
    ``coordination/inbox/architect-planner/``; body carries the
    reason + lease_id."""
    project = _project(tmp_path, monkeypatch, active_lease={
        "lease_id": "lease-0291",
        "task": "0291-probe",
        "holder_role": "TESTER",
    })
    sent: list[dict] = []
    monkeypatch.setattr(
        stand_mod, "_file_inbox_info",
        lambda coord, to_role, body, task_ref="": sent.append(
            {"to_role": to_role, "body": body, "task_ref": task_ref}
        ),
    )
    result = CliRunner().invoke(stand_mod.stand, [
        "down", "--reason", "deploy failed: ansible exit 3",
    ])
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
    msg = sent[0]
    assert msg["to_role"] == "ARCHITECT-PLANNER"
    assert "deploy failed: ansible exit 3" in msg["body"]
    assert "lease-0291" in msg["body"]
    assert msg["task_ref"] == "0291-probe"


def test_stand_down_message_omits_lease_id_when_no_active_lease(
    tmp_path: Path, monkeypatch,
) -> None:
    """State=preparing with no active_lease (edge case, e.g. SK
    flipping down from idle) → notification still fires, just
    without the lease_id suffix."""
    project = _project(tmp_path, monkeypatch, active_lease=None)
    sent: list[dict] = []
    monkeypatch.setattr(
        stand_mod, "_file_inbox_info",
        lambda coord, to_role, body, task_ref="": sent.append(
            {"to_role": to_role, "body": body, "task_ref": task_ref}
        ),
    )
    CliRunner().invoke(stand_mod.stand, [
        "down", "--reason", "infra incident",
    ])
    assert sent and sent[0]["body"] == "stand down: infra incident"
    assert sent[0]["task_ref"] == ""


def test_stand_down_state_transition_succeeds_even_when_notify_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    """Notification failure must NOT abort the state transition —
    state.yaml is the FSM source of truth; the inbox-info is a
    convenience. _file_inbox_info raises here; the CLI still
    completes with state=down."""
    project = _project(tmp_path, monkeypatch, active_lease=None)
    monkeypatch.setattr(
        stand_mod, "_file_inbox_info",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("fake send failure")),
    )
    # The helper does subprocess and swallows exceptions internally
    # in real usage. For this test we still expect the CLI to log
    # state→down (the click.echo) — the run completes without
    # raising. Click's CliRunner captures exceptions; assert no
    # uncaught one bubbles.
    result = CliRunner().invoke(stand_mod.stand, [
        "down", "--reason", "noisy",
    ], catch_exceptions=True)
    # Notification failure may surface (Click wraps it), but state.yaml
    # must have already been written. Confirm by reading state.
    from greatminds.cli import stand_state as ss
    state = ss.read_stand_state(project / "coordination")
    assert state["state"] == "down"
    assert state.get("down_reason") == "noisy"


# ---------- stand_up does NOT spam the notification ----------


def test_stand_up_does_not_notify(tmp_path: Path, monkeypatch) -> None:
    """0291 contract: ``on_down`` is the only auto-notification.
    Recovery via ``stand up`` flips state but does not re-notify —
    PLANNER reads the next state poll / lease cycle for the
    all-clear (no signal noise)."""
    project = _project(tmp_path, monkeypatch)
    coord = project / "coordination"
    # Prime down state first.
    from greatminds.cli import stand_state as ss
    state = ss.read_stand_state(coord)
    state["state"] = "down"
    state["down_reason"] = "infra"
    (coord / ".stand" / "state.yaml").write_text(
        yaml.safe_dump(state), encoding="utf-8")

    sent: list = []
    monkeypatch.setattr(
        stand_mod, "_file_inbox_info",
        lambda *a, **kw: sent.append(a) or None,
    )
    result = CliRunner().invoke(stand_mod.stand, [
        "up", "--reason", "fixed",
    ])
    assert result.exit_code == 0
    assert sent == [], (
        "0291: stand up must NOT spam an auto-notification — "
        "on_down is the only declared notification event"
    )
