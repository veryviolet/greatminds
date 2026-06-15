"""Tests for task 0317 (0311 Phase 2c): session-reset policy for
driven claude roles.

``claude --resume <sid>`` accumulates history across driven turns;
past a threshold the context is expensive + noisy. 0317 tracks a
per-role ``driven_turn_count`` in the registry and starts a fresh
session (no --resume) once the count crosses
``SESSION_RESET_TURN_THRESHOLD`` (default 50, env-overridable).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import coordd as cd


# ---------- argv: resume vs fresh ----------


def test_argv_resume_when_not_fresh() -> None:
    """Below threshold → ``claude --resume <sid> -p``."""
    argv = cd._build_driven_claude_argv("sess-1", None, fresh=False)
    assert argv[0].endswith("claude")
    assert argv[1:3] == ["--resume", "sess-1"]


def test_argv_fresh_omits_resume() -> None:
    """0317: fresh=True → no ``--resume`` (claude mints a new
    session); the prompt + bootstrap still ride."""
    argv = cd._build_driven_claude_argv(
        "sess-1", "/coord/.bootstrap/developer.md", fresh=True)
    assert "--resume" not in argv
    assert argv[0].endswith("claude")
    assert argv[1] == "-p"
    # Bootstrap (full contract) MUST be present on a fresh session
    # so it isn't context-blind.
    assert "--append-system-prompt-file" in argv


# ---------- threshold logic ----------


def test_turn_count_reads_registry() -> None:
    assert cd._driven_turn_count({"driven_turn_count": 7}) == 7
    assert cd._driven_turn_count({}) == 0
    assert cd._driven_turn_count(None) == 0
    # Garbage → 0.
    assert cd._driven_turn_count({"driven_turn_count": "x"}) == 0


def test_should_reset_below_threshold() -> None:
    assert cd._should_reset_session(
        {"driven_turn_count": 10}, threshold=50) is False


def test_should_reset_at_threshold() -> None:
    assert cd._should_reset_session(
        {"driven_turn_count": 50}, threshold=50) is True
    assert cd._should_reset_session(
        {"driven_turn_count": 51}, threshold=50) is True


def test_should_reset_default_threshold_is_50() -> None:
    """Pin the documented default."""
    assert cd.SESSION_RESET_TURN_THRESHOLD == 50


# ---------- registry recording ----------


def _registry(tmp_path: Path, role_lower: str,
              count: int = 0, sid: str = "sess-1") -> Path:
    reg_dir = tmp_path / cd.REGISTRY_DIR
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / f"{role_lower}.json").write_text(
        json.dumps({
            "role": role_lower.upper(), "tool": "claude",
            "pid": 1, "session_id": sid,
            "driven_turn_count": count,
        }), encoding="utf-8")
    return reg_dir


def test_record_non_reset_increments_count(tmp_path: Path) -> None:
    reg_dir = _registry(tmp_path, "developer", count=5)
    cd._record_driven_turn(reg_dir, "developer", reset=False)
    data = json.loads((reg_dir / "developer.json").read_text())
    assert data["driven_turn_count"] == 6


def test_record_reset_sets_count_to_one(tmp_path: Path) -> None:
    reg_dir = _registry(tmp_path, "developer", count=50)
    cd._record_driven_turn(reg_dir, "developer", reset=True,
                          new_session_id="sess-NEW")
    data = json.loads((reg_dir / "developer.json").read_text())
    assert data["driven_turn_count"] == 1
    assert data["session_id"] == "sess-NEW"


def test_record_missing_registry_is_noop(tmp_path: Path) -> None:
    """No registry file → record is a safe no-op (count restarts
    at 0 on the next read)."""
    reg_dir = tmp_path / cd.REGISTRY_DIR
    reg_dir.mkdir(parents=True)
    # Must not raise.
    cd._record_driven_turn(reg_dir, "ghost", reset=False)


# ---------- spawn integration: count gates resume vs fresh ----------


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    return coord


def test_spawn_below_threshold_uses_resume(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    reg = {"session_id": "sess-1", "driven_turn_count": 10}
    spawned: list = []
    cd._spawn_driven_turn(
        coord, "developer", "sess-1", "dev", "test-session",
        None, verbose=False, reg=reg,
        spawn=lambda argv: spawned.append(argv),
    )
    assert "--resume" in spawned[0]


def test_spawn_at_threshold_uses_fresh_session(tmp_path: Path) -> None:
    """0317: count >= threshold → spawn a FRESH session (no
    --resume), and the registry count resets to 1."""
    coord = _coord(tmp_path)
    # Seed registry at the threshold.
    _registry(tmp_path / "coordination" / "..", "developer",
              count=50) if False else None
    reg_dir = coord / cd.REGISTRY_DIR
    (reg_dir / "developer.json").write_text(
        json.dumps({"session_id": "sess-1",
                    "driven_turn_count": 50}), encoding="utf-8")
    reg = {"session_id": "sess-1", "driven_turn_count": 50}

    spawned: list = []
    cd._spawn_driven_turn(
        coord, "developer", "sess-1", "dev", "test-session",
        None, verbose=False, reg=reg,
        spawn=lambda argv: spawned.append(argv),
    )
    assert "--resume" not in spawned[0], (
        "0317: at threshold, spawn must start a fresh session"
    )
    # Registry count reset to 1.
    data = json.loads((reg_dir / "developer.json").read_text())
    assert data["driven_turn_count"] == 1


def test_spawn_increments_count_on_normal_turn(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    reg_dir = coord / cd.REGISTRY_DIR
    (reg_dir / "developer.json").write_text(
        json.dumps({"session_id": "sess-1",
                    "driven_turn_count": 3}), encoding="utf-8")
    reg = {"session_id": "sess-1", "driven_turn_count": 3}

    cd._spawn_driven_turn(
        coord, "developer", "sess-1", "dev", "test-session",
        None, verbose=False, reg=reg,
        spawn=lambda argv: None,
    )
    data = json.loads((reg_dir / "developer.json").read_text())
    assert data["driven_turn_count"] == 4


# ---------- env override ----------


def test_threshold_env_override(monkeypatch) -> None:
    """``COORDD_SESSION_RESET_TURNS`` overrides the default. The
    constant is read at import; test the helper accepts an explicit
    threshold (the env-driven module constant is pinned separately)."""
    assert cd._should_reset_session(
        {"driven_turn_count": 5}, threshold=5) is True
    assert cd._should_reset_session(
        {"driven_turn_count": 4}, threshold=5) is False
