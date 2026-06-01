"""Tests for task 0315 (0311 Phase 2a): coordd driver core for
driven claude roles.

Pre-0315 coordd only WOKE agents (press_enter / SIGINT) — a
persistent agent process then ran its own loop. The driven model
inverts that: coordd RUNS each turn via ``claude --resume -p``;
the pane is idle bash between turns. 0315 adds the driver
mechanism + a per-role run-lock (no second turn while one runs;
event mid-turn → pending → re-fire after).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as cd


# ---------- argv construction ----------


def test_build_driven_argv_resume_and_prompt() -> None:
    """0315: the driven turn command is ``claude --resume <sid> -p
    'continue your tick'``."""
    argv = cd._build_driven_claude_argv("sess-123", None)
    assert argv[:5] == ["claude", "--resume", "sess-123", "-p",
                         "continue your tick"]
    # No --append-system-prompt-file when bootstrap is None (0316
    # adds it).
    assert "--append-system-prompt-file" not in argv


def test_build_driven_argv_includes_bootstrap_when_present() -> None:
    """When a bootstrap file is supplied (0316 path), it rides via
    ``--append-system-prompt-file``."""
    argv = cd._build_driven_claude_argv(
        "sess-123", "/coord/.bootstrap/developer.md")
    assert "--append-system-prompt-file" in argv
    idx = argv.index("--append-system-prompt-file")
    assert argv[idx + 1] == "/coord/.bootstrap/developer.md"


# ---------- run-lock semantics ----------


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    return coord


def test_spawn_acquires_lock_and_calls_spawn(tmp_path: Path) -> None:
    """First event → run-lock acquired, spawn seam invoked with the
    argv."""
    coord = _coord(tmp_path)
    spawned: list = []
    ok, diag = cd._spawn_driven_turn(
        coord, "developer", "sess-1", "dev", "test-session",
        None, verbose=False,
        spawn=lambda argv: spawned.append(argv),
    )
    assert ok is True
    assert len(spawned) == 1
    assert spawned[0][:2] == ["claude", "--resume"]


def test_spawn_run_lock_blocks_second_turn(tmp_path: Path) -> None:
    """0315 run-lock: with the lock already held, a second event
    must NOT spawn — it sets the pending marker instead."""
    coord = _coord(tmp_path)
    # Simulate a turn already running.
    cd._driven_run_lock_path(coord, "developer").touch()

    spawned: list = []
    ok, diag = cd._spawn_driven_turn(
        coord, "developer", "sess-1", "dev", "test-session",
        None, verbose=False,
        spawn=lambda argv: spawned.append(argv),
    )
    assert ok is False
    assert spawned == [], (
        "0315: must NOT spawn a second turn while one is running"
    )
    assert cd._driven_pending_path(coord, "developer").exists(), (
        "0315: mid-turn event must set the pending marker"
    )
    assert "pending" in diag.lower()


def test_spawn_seam_leaves_lock_for_completion_hook(
    tmp_path: Path,
) -> None:
    """With an explicit ``spawn`` seam (test / async path), the
    lock is left in place for a completion hook to clear — proves
    the run-lock survives the spawn call so concurrent events
    serialize."""
    coord = _coord(tmp_path)
    cd._spawn_driven_turn(
        coord, "tester", "sess-x", "tester", "test-session",
        None, verbose=False, spawn=lambda argv: None,
    )
    assert cd._driven_run_lock_path(coord, "tester").exists(), (
        "0315: run-lock must persist after spawn seam for the "
        "completion hook to clear"
    )


# ---------- lifecycle gating ----------


def _project_with_lifecycle(tmp_path: Path, *,
                            role: str, lifecycle: str,
                            tool: str) -> tuple[Path, Path]:
    """Build a toy project: coord.yaml maps role → window+tool;
    schema declares the role's lifecycle + owner of feature_dev."""
    project = tmp_path / "project"
    coord = project / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": "dev", "role": role, "tool": tool, "mode": "loop"},
        ],
    }), encoding="utf-8")
    canon = project / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {"feature_dev": {"owner": role}},
        "roles": {role: {"lifecycle": lifecycle}},
        "event_wake": {"by_tool": {
            "claude": "tmux_send_keys",
            "codex": "sigint_deepest_descendant",
        }},
    }), encoding="utf-8")
    # Registry with a session_id for the driven role.
    (coord / cd.REGISTRY_DIR / f"{role.lower()}.json").write_text(
        '{"role": "%s", "tool": "%s", "pid": 1, '
        '"session_id": "sess-abc"}' % (role, tool),
        encoding="utf-8",
    )
    return coord, canon


def test_route_event_drives_claude_driven_role(
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end: a file in feature_dev owned by a driven claude
    role → coordd spawns a driven turn (not a press_enter wake)."""
    coord, canon = _project_with_lifecycle(
        tmp_path, role="DEVELOPER", lifecycle="driven", tool="claude")

    spawned: list = []
    monkeypatch.setattr(
        cd, "_tmux_send_keys_driven",
        lambda session, pane, argv: spawned.append(argv),
    )
    # press_enter must NOT be the path taken.
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: pytest.fail(
            "0315: driven role must NOT use press_enter wake"),
    )

    woke = cd._route_queue_event(
        coord, canon, "feature_dev", "0001-x.yaml", verbose=False)
    assert woke is True
    assert len(spawned) == 1
    assert spawned[0][:2] == ["claude", "--resume"]
    assert "sess-abc" in spawned[0]


def test_route_event_non_driven_uses_legacy_wake(
    tmp_path: Path, monkeypatch,
) -> None:
    """0315: a role whose lifecycle != driven keeps the legacy
    press_enter wake path (no claude --resume spawn)."""
    coord, canon = _project_with_lifecycle(
        tmp_path, role="ARCHITECT-PLANNER", lifecycle="interactive",
        tool="claude")

    monkeypatch.setattr(
        cd, "_tmux_send_keys_driven",
        lambda *a, **kw: pytest.fail(
            "0315: interactive role must NOT be driven-spawned"),
    )
    calls: list = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: calls.append(a) or (True, "wake ok"),
    )

    woke = cd._route_queue_event(
        coord, canon, "feature_dev", "0001-x.yaml", verbose=False)
    assert woke is True
    assert calls, "0315: interactive role must use press_enter wake"


def test_route_event_driven_without_session_falls_back(
    tmp_path: Path, monkeypatch,
) -> None:
    """A driven claude role with NO session_id in the registry
    can't be --resume'd → falls back to the legacy wake mechanism
    rather than spawning a broken command."""
    coord, canon = _project_with_lifecycle(
        tmp_path, role="DEVELOPER", lifecycle="driven", tool="claude")
    # Overwrite registry without session_id.
    (coord / cd.REGISTRY_DIR / "developer.json").write_text(
        '{"role": "DEVELOPER", "tool": "claude", "pid": 1}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cd, "_tmux_send_keys_driven",
        lambda *a, **kw: pytest.fail(
            "0315: must NOT spawn driven turn without session_id"),
    )
    calls: list = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: calls.append(a) or (True, "wake fallback"),
    )

    woke = cd._route_queue_event(
        coord, canon, "feature_dev", "0001-x.yaml", verbose=False)
    assert woke is True
    assert calls, "0315: missing session_id must fall back to wake"


# ---------- lifecycle helper ----------


def test_lifecycle_helper_reads_schema(tmp_path: Path) -> None:
    _, canon = _project_with_lifecycle(
        tmp_path, role="DEVELOPER", lifecycle="driven", tool="claude")
    assert cd._lifecycle_for_role(canon, "DEVELOPER") == "driven"


def test_lifecycle_helper_none_when_absent(tmp_path: Path) -> None:
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(
        yaml.safe_dump({"roles": {"DEVELOPER": {}}}), encoding="utf-8")
    assert cd._lifecycle_for_role(canon, "DEVELOPER") is None
