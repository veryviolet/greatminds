"""Tests for coordd's stalled-agent sweep (task 0017).

The sweep is what closes the Anthropic-server-side-rate-limit hole:
a turn aborts before ScheduleWakeup is called, leaving the agent's pid
alive but heartbeat cold forever. The sweep detects that state and
nudges the agent through its input_sock.

These tests exercise ``_stalled_agent_sweep`` directly (not the whole
coordd loop). External effects — the socket write — are mocked via a
``push_to_role`` monkeypatch so we capture which roles got nudged
without actually opening unix sockets.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as cd


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


CANONICAL_WINDOWS = [
    {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude", "mode": "chat"},
    {"name": "dev", "role": "DEVELOPER", "tool": "claude", "mode": "loop"},
    {"name": "ui", "role": "UI-DEVELOPER", "tool": "claude", "mode": "loop"},
    {"name": "tester", "role": "TESTER", "tool": "claude", "mode": "loop"},
    {"name": "maintainer", "role": "MAINTAINER", "tool": "claude", "mode": "chat"},
]


@pytest.fixture
def project(tmp_path):
    """Build a fresh project tree with coord.yaml + coordination/."""
    coord = tmp_path / "coordination"
    (coord / ".agent_registry").mkdir(parents=True)
    cfg = {"session": "test", "project_dir": str(tmp_path),
           "windows": CANONICAL_WINDOWS}
    (tmp_path / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_push(monkeypatch):
    """Replace press_enter (preferred path since 0051 iter-6) AND push_to_role
    (fallback path) with the same recorder.

    Iter-6 routes the stalled sweep through ``press_enter`` when coord.yaml
    has a session+window for the role; otherwise it falls back to
    ``push_to_role``. The recorder captures the same shape from both paths
    so existing tests (which check ``.calls``) still work and the new
    ``test_stalled_sweep_delegates_to_press_enter`` test verifies which
    primitive was called.
    """
    calls: list[tuple] = []
    result = {"return": True}
    # Track WHICH primitive each call came through.
    primitives: list[str] = []

    def fake_push(coord, role, file_path, verbose, bypass_fresh_guard=False):
        calls.append((role, file_path, bypass_fresh_guard))
        primitives.append("push_to_role")
        return result["return"]

    # Look up heartbeat age from the project coord_dir so the recorded
    # payload string can include it (matches push_to_role's prior
    # f"<stalled-sweep: heartbeat {age:.0f}s old>" format that existing
    # tests assert on).
    def fake_press_enter(coord_dir, session, window, role_lower, agent_type,
                         *, mode="wake", verify=True, verify_timeout_s=30.0):
        hb = Path(coord_dir) / f"heartbeat.{role_lower}"
        try:
            age = time.time() - hb.stat().st_mtime
        except OSError:
            age = 0.0
        payload = f"<stalled-sweep via press_enter: heartbeat {age:.0f}s old>"
        calls.append((role_lower, payload, True))
        primitives.append("press_enter")
        if result["return"]:
            return (True, f"fake press_enter ok (role={role_lower})")
        return (False, f"fake press_enter False (role={role_lower})")

    monkeypatch.setattr(cd, "push_to_role", fake_push)
    # press_enter is imported inside the sweep function (lazy import) — patch
    # the source module so the lazy import resolves to the fake.
    from greatminds.cli import _send_enter
    monkeypatch.setattr(_send_enter, "press_enter", fake_press_enter)
    return type("FakePush", (), {"calls": calls, "result": result,
                                  "primitives": primitives})()


def _set_heartbeat(project_dir: Path, role_lower: str, age_seconds: float):
    """Write a heartbeat file with mtime set to `age_seconds` ago."""
    coord = project_dir / "coordination"
    p = coord / f"heartbeat.{role_lower}"
    p.write_text("", encoding="utf-8")
    target = time.time() - age_seconds
    os.utime(p, (target, target))
    return p


def _read_coord_yaml(project_dir: Path) -> dict:
    return yaml.safe_load((project_dir / "coord.yaml").read_text())


# ---------------------------------------------------------------------------
# Plan-required cases (10)
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_no_nudge(project, fake_push):
    _set_heartbeat(project, "developer", age_seconds=10)  # well under threshold
    cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                            threshold_seconds=600.0)
    assert fake_push.calls == []


def test_stale_loop_mode_nudged(project, fake_push):
    _set_heartbeat(project, "developer", age_seconds=900)
    n = cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                                threshold_seconds=600.0)
    assert n == 1
    roles = [c[0] for c in fake_push.calls]
    assert "developer" in roles
    # File-path string used for logging should mention the stalled sweep.
    assert "stalled-sweep" in fake_push.calls[0][1]
    assert "900" in fake_push.calls[0][1]  # heartbeat age included


def test_stale_chat_mode_not_nudged(project, fake_push):
    _set_heartbeat(project, "architect-planner", age_seconds=900)
    cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                            threshold_seconds=600.0)
    assert fake_push.calls == []  # chat mode → skip


def test_stale_dead_pid_no_nudge(project, fake_push):
    """push_to_role itself skips dead pids and returns False; sweep should
    NOT count this as a nudge. We simulate push_to_role's False return."""
    _set_heartbeat(project, "developer", age_seconds=900)
    fake_push.result["return"] = False  # push_to_role saw dead pid, refused
    n = cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                                threshold_seconds=600.0)
    assert n == 0  # nudge count tracks only successful pushes
    # But push_to_role WAS called (it decides; sweep delegates).
    assert len(fake_push.calls) == 1


def test_stale_no_registry_handled_inside_push_to_role(project, fake_push):
    """No registry → push_to_role returns False (we already test that in
    test_restart.py / test_daemon.py). Sweep simply forwards; no crash."""
    _set_heartbeat(project, "tester", age_seconds=900)
    fake_push.result["return"] = False  # simulate "no registry / no socket"
    n = cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                                threshold_seconds=600.0)
    assert n == 0


def test_stale_orphan_role_skipped_with_warn(project, fake_push, capsys):
    """Heartbeat present but no matching role in coord.yaml → WARN + skip."""
    _set_heartbeat(project, "ghost-role", age_seconds=900)
    cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                            threshold_seconds=600.0, verbose=True)
    assert fake_push.calls == []
    captured = capsys.readouterr()
    assert "orphan heartbeat" in captured.err.lower()
    assert "ghost-role" in captured.err


def test_custom_threshold_in_coord_yaml(project, fake_push):
    """Lower the threshold via coord.yaml: coordd: { stalled_threshold_seconds: 30 }."""
    cfg = _read_coord_yaml(project)
    cfg["coordd"] = {"stalled_threshold_seconds": 30}
    (project / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    # _read_coordd_config returns the parsed value.
    interval, threshold = cd._read_coordd_config(project)
    assert threshold == 30.0

    _set_heartbeat(project, "developer", age_seconds=60)  # > 30
    n = cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                                threshold_seconds=threshold)
    assert n == 1


def test_invalid_config_falls_back_to_default(project, fake_push, capsys):
    cfg = _read_coord_yaml(project)
    cfg["coordd"] = {"stalled_threshold_seconds": -1}
    (project / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    interval, threshold = cd._read_coordd_config(project)
    assert threshold == cd.STALLED_THRESHOLD_DEFAULT
    captured = capsys.readouterr()
    assert "invalid" in captured.err.lower()
    assert "stalled_threshold_seconds" in captured.err


def test_multiple_stale_agents_in_one_sweep(project, fake_push):
    _set_heartbeat(project, "developer", age_seconds=900)
    _set_heartbeat(project, "tester",    age_seconds=1200)
    _set_heartbeat(project, "ui-developer", age_seconds=800)
    n = cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                                threshold_seconds=600.0)
    assert n == 3
    roles = {c[0] for c in fake_push.calls}
    assert {"developer", "tester", "ui-developer"} <= roles


def test_fast_mode_heartbeat_skipped(project, fake_push):
    """heartbeat.ui-developer.fast → FAST mode chat session → skip."""
    _set_heartbeat(project, "ui-developer.fast", age_seconds=900)
    cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                            threshold_seconds=600.0)
    assert fake_push.calls == [], \
        "FAST-mode UI-DEVELOPER must NOT be nudged"


# ---------------------------------------------------------------------------
# _read_coordd_config — defaults
# ---------------------------------------------------------------------------


def test_config_defaults_when_no_coordd_block(project):
    interval, threshold = cd._read_coordd_config(project)
    assert interval == cd.STALLED_SWEEP_INTERVAL_DEFAULT
    assert threshold == cd.STALLED_THRESHOLD_DEFAULT


def test_config_defaults_when_no_coord_yaml(tmp_path):
    interval, threshold = cd._read_coordd_config(tmp_path)
    assert interval == cd.STALLED_SWEEP_INTERVAL_DEFAULT
    assert threshold == cd.STALLED_THRESHOLD_DEFAULT


def test_sub_60s_threshold_still_nudges_via_press_enter(
    project, monkeypatch,
):
    """Iter-6: with press_enter as the primary path (no fresh-guard
    of its own — the sweep's own staleness gate is the authority), a
    custom ``stalled_threshold_seconds: 30`` config must still nudge
    even though heartbeat (35s) is less than the legacy 60s guard.

    This test exercises the REAL press_enter via the actual lazy
    import in _stalled_agent_sweep; only the socket I/O is stubbed.
    """
    role_lower = "developer"
    _set_heartbeat(project, role_lower, age_seconds=35)

    # Registry entry with a fake input_sock so press_enter prefers
    # the socket channel.
    reg_dir = project / "coordination" / ".agent_registry"
    fake_sock = reg_dir / f"{role_lower}.sock"
    fake_sock.touch()
    (reg_dir / f"{role_lower}.json").write_text(
        json.dumps({
            "role": "DEVELOPER",
            "tool": "claude",
            "pid": os.getpid(),  # alive
            "tty": "/dev/pts/0",
            "input_sock": str(fake_sock),
        }),
        encoding="utf-8",
    )

    sends: list[bytes] = []

    class FakeSocket:
        def settimeout(self, *_a, **_kw): pass
        def connect(self, *_a, **_kw): pass
        def sendall(self, data): sends.append(data)
        def close(self): pass

    import socket as _socket_mod
    monkeypatch.setattr(_socket_mod, "socket",
                        lambda *_a, **_kw: FakeSocket())
    # Don't actually sleep WAKE_GAP / verify timeout.
    monkeypatch.setattr(cd.time, "sleep", lambda *_a, **_kw: None)
    from greatminds.cli import _send_enter
    monkeypatch.setattr(_send_enter.time, "sleep", lambda *_a, **_kw: None)
    # Heartbeat poll: simulate one immediate advance so press_enter returns True.
    monkeypatch.setattr(_send_enter, "_poll_heartbeat_advance",
                        lambda *a, **kw: True)
    # capture-pane mock so the verify path doesn't shell out.
    monkeypatch.setattr(_send_enter, "_capture_pane",
                        lambda session, window: "developer> ticking")

    # Sub-60s threshold via coord.yaml.
    cfg = _read_coord_yaml(project)
    cfg["coordd"] = {"stalled_threshold_seconds": 30}
    (project / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    interval, threshold = cd._read_coordd_config(project)
    assert threshold == 30.0

    n = cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                                threshold_seconds=threshold)
    assert n == 1, "sub-60s-threshold config must produce a real nudge"
    # Press_enter wake-mode wrote WAKE_TEXT then \r via the input_sock.
    sent_blob = b"".join(sends)
    assert b"continue your tick" in sent_blob
    assert b"\r" in sent_blob


def test_config_picks_up_both_keys(project):
    cfg = _read_coord_yaml(project)
    cfg["coordd"] = {
        "stalled_sweep_interval_seconds": 120,
        "stalled_threshold_seconds": 300,
    }
    (project / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    interval, threshold = cd._read_coordd_config(project)
    assert interval == 120.0
    assert threshold == 300.0


# ---------------------------------------------------------------------------
# task 0051 iter-6: stalled sweep delegates to the press_enter primitive
# when coord.yaml has session+window for the role. REVIEWER-required
# regression net: 'add regression coverage proving the higher-level path
# uses the primitive'.
# ---------------------------------------------------------------------------


def test_stalled_sweep_delegates_to_press_enter(project, fake_push):
    """Iter-6 contract: when session+window are present in coord.yaml,
    the stalled sweep routes through ``press_enter`` (the unified
    primitive from 0051) — NOT directly to ``push_to_role``. This is
    the single regression net for the architectural unification."""
    _set_heartbeat(project, "developer", age_seconds=900)
    cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                            threshold_seconds=600.0)
    # The developer role IS in coord.yaml's windows fixture, so press_enter
    # is the primitive that should fire.
    assert "press_enter" in fake_push.primitives
    assert "push_to_role" not in fake_push.primitives


def test_stalled_sweep_falls_back_to_push_to_role_when_no_session(
    project, fake_push,
):
    """When coord.yaml lacks session/window (rare; orphan config), the
    sweep falls back to push_to_role — keeps the existing input_sock
    nudge path functional."""
    # Strip session from coord.yaml so press_enter can't address the pane.
    cfg = _read_coord_yaml(project)
    cfg.pop("session", None)
    (project / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _set_heartbeat(project, "developer", age_seconds=900)
    cd._stalled_agent_sweep(project, _read_coord_yaml(project),
                            threshold_seconds=600.0)
    assert "push_to_role" in fake_push.primitives
    assert "press_enter" not in fake_push.primitives
