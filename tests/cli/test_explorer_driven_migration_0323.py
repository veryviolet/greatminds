"""Tests for task 0323 (0311 Phase 3c): migrate EXPLORER (codex) to
the driven lifecycle — the first CODEX worker migrated.

EXPLORER is already lifecycle=driven in schema (0312). 0323 completes
the migration: the coord.yaml template sets the explorer window to
mode=driven, launch leaves a driven pane idle, command_START reframes
EXPLORER as driven (no self-pacing /loop). The codex stdio-per-turn
driver (0321) is generic — it fires when schema lifecycle==driven AND
tool==codex AND the coord.yaml window mode==driven — so flipping the
window mode activates it. TECHNICAL-WRITER (codex) migrates later;
PLANNER / MAINTAINER / claude workers are untouched.

The live avatar GATE (codex stdio-per-turn spawn, turn/completed,
idle-bash, run-lock) is TESTER's job — stand_required.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from greatminds.cli import launch as launch_mod
from greatminds.cli import coordd as cd
from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def _coord_template() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(
            encoding="utf-8")
    ) or {}


def _command_start() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8")
    ) or {}


# ---------- canon: EXPLORER is driven everywhere ----------


def test_schema_explorer_lifecycle_driven() -> None:
    assert _schema()["roles"]["EXPLORER"].get("lifecycle") == "driven"


def test_coord_template_explorer_mode_driven() -> None:
    win = next(
        (w for w in (_coord_template().get("windows") or [])
         if isinstance(w, dict) and w.get("role") == "EXPLORER"),
        None,
    )
    assert win is not None
    assert win.get("tool") == "codex"
    assert win.get("mode") == "driven", (
        f"0323: explorer window mode must be 'driven' (got "
        f"{win.get('mode')!r})"
    )


def test_command_start_explorer_launch_is_driven() -> None:
    spec = (_command_start().get("roles") or {}).get("EXPLORER") or {}
    assert spec.get("launch") == "driven"
    body = spec.get("body") or ""
    low = body.lower()
    assert "driven" in low
    assert "self-pace" in low
    assert not body.lstrip().startswith("/loop "), (
        "0323: driven EXPLORER body must not open with a /loop opener"
    )


def test_other_codex_workers_not_migrated_yet() -> None:
    """0323 migrates ONLY EXPLORER among codex roles. TECHNICAL-WRITER
    (codex) must stay non-driven this phase."""
    by_role = {w.get("role"): w for w in
               (_coord_template().get("windows") or [])
               if isinstance(w, dict)}
    writer = by_role.get("TECHNICAL-WRITER")
    if writer is not None:
        assert writer.get("mode") != "driven", (
            "0323: only EXPLORER migrates this phase; "
            "TECHNICAL-WRITER stays non-driven"
        )


# ---------- launch leaves the driven codex pane idle ----------


def _env_setup():
    return launch_mod.gm_env.EnvSetup(
        env_type=None, activation="", source="(test)")


def test_launch_leaves_driven_explorer_pane_idle(
    tmp_path: Path, monkeypatch,
):
    """0323: a mode=driven codex window must NOT receive a start-agent
    send-keys — the pane stays idle bash for coordd to drive."""
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
            {"name": "explorer", "role": "EXPLORER", "tool": "codex",
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
                    "0323: driven codex pane must NOT receive a "
                    f"start-agent command. Got: {arg}"
                )


# ---------- coordd drives the migrated EXPLORER via codex stdio ----------


def test_coordd_drives_explorer_codex_when_window_driven(
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end: a queue landing owned by the now-driven codex
    EXPLORER reaches the codex stdio-per-turn driver
    (``_spawn_driven_codex_turn``), NOT the SIGINT wake."""
    project = tmp_path / "project"
    coord = project / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "fleet",
        "project_dir": str(project),
        "windows": [
            {"name": "explorer", "role": "EXPLORER", "tool": "codex",
             "mode": "driven"},
        ],
    }), encoding="utf-8")
    canon = project / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {"review_inbox": {"owner": "EXPLORER"}},
        "roles": {"EXPLORER": {"lifecycle": "driven"}},
        "event_wake": {"by_tool": {
            "claude": "tmux_send_keys",
            "codex": "sigint_deepest_descendant"}},
    }), encoding="utf-8")
    (coord / cd.REGISTRY_DIR / "explorer.json").write_text(
        json.dumps({"role": "EXPLORER", "tool": "codex", "pid": 1}),
        encoding="utf-8")

    calls: list = []
    monkeypatch.setattr(
        cd, "_spawn_driven_codex_turn",
        lambda *a, **kw: calls.append((a, kw)) or (True, "ok"),
    )
    monkeypatch.setattr(
        cd, "sigint_sleeping_descendant",
        lambda *a, **kw: pytest.fail(
            "0323: driven codex EXPLORER must NOT use SIGINT wake"),
    )

    woke = cd._route_queue_event(
        coord, canon, "review_inbox", "0001-x.yaml", verbose=False)
    assert woke is True
    assert len(calls) == 1
    assert calls[0][0][1] == "explorer"
