"""Tests for task 0318 (0311 Phase 2d): migrate READER to the
driven lifecycle (first role, low-risk).

READER is already lifecycle=driven in schema (0312). 0318 completes
the migration: the coord.yaml template sets the reader window to
mode=driven, launch leaves a driven pane idle (no persistent
agent), command_START reframes READER as driven (no self-pacing),
and coordd's driven dispatch force-spawns a FRESH session on the
first event (no session_id yet). Other claude workers migrate
later, one at a time.

The live avatar GATE (driven spawn, idle-bash, run-lock, recovery)
is TESTER's job — stand_required.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import launch as launch_mod
from greatminds.cli import coordd as cd
from greatminds.core.paths import find_canon_dir


# ---------- canon: READER is driven everywhere ----------


def test_schema_reader_lifecycle_driven() -> None:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    assert (doc["roles"]["READER"].get("lifecycle")) == "driven"


def test_coord_template_reader_mode_driven() -> None:
    """0318: the coord.yaml template reader window must be
    mode=driven so launch leaves it idle + coordd drives it."""
    doc = yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(
            encoding="utf-8")
    ) or {}
    reader = next(
        (w for w in (doc.get("windows") or [])
         if isinstance(w, dict) and w.get("role") == "READER"),
        None,
    )
    assert reader is not None
    assert reader.get("mode") == "driven", (
        f"0318: reader window mode must be 'driven' (got "
        f"{reader.get('mode')!r})"
    )


def test_command_start_reader_launch_is_driven() -> None:
    doc = yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8")
    ) or {}
    reader = (doc.get("roles") or {}).get("READER") or {}
    assert reader.get("launch") == "driven"
    body = reader.get("body") or ""
    low = body.lower()
    assert "driven" in low
    # No self-pacing /loop framing — the body tells READER not to
    # self-pace (wording may wrap across lines, so match loosely).
    assert "self-pace" in low


def test_other_claude_workers_not_migrated_yet() -> None:
    """0318 migrates ONLY READER. The coord template's other claude
    workers (tester) must stay mode=loop — one-at-a-time migration."""
    doc = yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(
            encoding="utf-8")
    ) or {}
    by_role = {w.get("role"): w for w in (doc.get("windows") or [])
               if isinstance(w, dict)}
    # TESTER is claude in the template — must NOT be driven yet.
    tester = by_role.get("TESTER")
    if tester is not None:
        assert tester.get("mode") != "driven", (
            "0318: only READER migrates this phase; TESTER stays loop"
        )


# ---------- launch leaves driven pane idle ----------


def _env_setup():
    return launch_mod.gm_env.EnvSetup(
        env_type=None, activation="", source="(test)")


def test_launch_leaves_driven_pane_idle(tmp_path: Path, monkeypatch):
    """0318: a mode=driven role window must NOT receive a
    start-agent send-keys — the pane stays idle bash for coordd to
    drive."""
    calls: list = []
    import subprocess as _sp
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or _sp.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )
    cfg = {
        "session": "test",
        "windows": [
            {"name": "reader", "role": "READER", "tool": "claude",
             "mode": "driven"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    send_keys = [c for c in calls
                 if c and c[0] == "tmux" and c[1] == "send-keys"]
    for c in send_keys:
        for arg in c:
            if isinstance(arg, str):
                assert "greatminds start-agent" not in arg, (
                    "0318: driven pane must NOT receive a "
                    f"start-agent command. Got: {arg}"
                )


def test_launch_non_driven_role_still_starts(tmp_path: Path, monkeypatch):
    """Regression net: a mode=loop role still gets its start-agent
    command (0308 behavior unchanged for non-driven roles)."""
    calls: list = []
    import subprocess as _sp
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or _sp.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )
    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    send_keys = [c for c in calls
                 if c and c[0] == "tmux" and c[1] == "send-keys"]
    assert any(
        isinstance(a, str) and "greatminds start-agent" in a
        for c in send_keys for a in c
    ), "0318: non-driven role must still get its start-agent command"


# ---------- coordd force-fresh on missing session_id ----------


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    return coord


def test_driven_first_turn_forces_fresh_session(tmp_path: Path):
    """0318: no session_id yet (first event after launch) → spawn a
    FRESH session (claude -p, no --resume) so the driven role can
    bootstrap. Pre-0318 this fell back to a useless wake."""
    coord = _coord(tmp_path)
    spawned: list = []
    ok, diag = cd._spawn_driven_turn(
        coord, "reader", "", "reader", "test-session",
        None, verbose=False, reg=None, force_fresh=True,
        spawn=lambda argv: spawned.append(argv),
    )
    assert ok is True
    assert spawned, "0318: force_fresh must spawn a turn"
    assert "--resume" not in spawned[0], (
        "0318: force-fresh first turn must NOT use --resume "
        "(no session yet)"
    )
    assert spawned[0][:2] == ["claude", "-p"]


def test_driven_route_force_fresh_when_no_session(
    tmp_path: Path, monkeypatch,
):
    """End-to-end: an event for a driven READER with no session_id
    in the registry → coordd spawns a fresh turn (not a wake)."""
    project = tmp_path / "project"
    coord = project / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": "reader", "role": "READER", "tool": "claude",
             "mode": "driven"},
        ],
    }), encoding="utf-8")
    canon = project / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {"feature_docs_review": {"owner": "READER"}},
        "roles": {"READER": {"lifecycle": "driven"}},
        "event_wake": {"by_tool": {"claude": "tmux_send_keys"}},
    }), encoding="utf-8")
    # Registry WITHOUT session_id.
    (coord / cd.REGISTRY_DIR / "reader.json").write_text(
        '{"role": "READER", "tool": "claude", "pid": 1}',
        encoding="utf-8")

    spawned: list = []
    monkeypatch.setattr(
        cd, "_tmux_send_keys_driven",
        lambda session, pane, argv: spawned.append(argv),
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: pytest.fail(
            "0318: driven role with no session must spawn fresh, "
            "NOT fall back to wake"),
    )

    woke = cd._route_queue_event(
        coord, canon, "feature_docs_review", "0001-x.yaml",
        verbose=False)
    assert woke is True
    assert spawned, "0318: must spawn a fresh driven turn"
    assert "--resume" not in spawned[0]
