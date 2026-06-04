"""Tests for task 0319 (0311 Phase 2e): migrate the remaining claude
worker roles to the driven lifecycle in ONE batch — DEVELOPER,
UI-DEVELOPER, TESTER, STAND-KEEPER.

The driven mechanism was proven by READER (0318) on avatar; it is
generic — coordd runs each turn via ``claude --resume -p`` when a
role's schema lifecycle == 'driven' AND its coord.yaml window
mode == 'driven'. Only the per-role config differs, so the four
remaining claude workers migrate together (one avatar validation,
not four cycles).

NOT touched: PLANNER (interactive), MAINTAINER (self-loop safety
net), and the codex roles ARCHITECT-REVIEWER / TECHNICAL-WRITER /
EXPLORER (Phase 3).

Notable behavioural shift: STAND-KEEPER's ``.stand`` state events
(``schema.queues['.stand']`` owner STAND-KEEPER) now drive a turn
via ``claude --resume -p`` instead of a press_enter wake. Full
behavioural proof for the batch runs on the representative role
(DEVELOPER) on a live avatar — TESTER's GATE (stand_required); SK
is confirmed at the config + argv-driver level (0319 plan GATE
step 3).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import launch as launch_mod
from greatminds.cli import coordd as cd
from greatminds.core.paths import find_canon_dir


# Roles migrated to driven in this batch.
MIGRATED = ["DEVELOPER", "UI-DEVELOPER", "TESTER"]
# Roles that MUST stay non-driven: only the two paned, resident seats.
# Every worker — including the codex ones (ARCHITECT-REVIEWER, EXPLORER,
# TECHNICAL-WRITER) — is driven.
UNTOUCHED = ["ARCHITECT-PLANNER", "MAINTAINER"]


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def _coord_template() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(
            encoding="utf-8")
    ) or {}


# ---------- canon: migrated roles are driven everywhere ----------


@pytest.mark.parametrize("role", MIGRATED)
def test_schema_lifecycle_driven(role: str) -> None:
    assert _schema()["roles"][role].get("lifecycle") == "driven", (
        f"0319: {role} schema lifecycle must be 'driven'"
    )


@pytest.mark.parametrize("role", MIGRATED)
def test_coord_template_window_mode_driven(role: str) -> None:
    """0319: each migrated role's coord.yaml template window must be
    mode=driven so launch leaves it idle + coordd drives it."""
    win = next(
        (w for w in (_coord_template().get("windows") or [])
         if isinstance(w, dict) and w.get("role") == role),
        None,
    )
    assert win is not None, f"0319: no coord.yaml window for {role}"
    assert win.get("mode") == "driven", (
        f"0319: {role} window mode must be 'driven' (got "
        f"{win.get('mode')!r})"
    )


def test_reader_and_batch_all_driven_in_template() -> None:
    """0318 (READER) + 0319 (batch): all five migrated claude workers
    are mode=driven in the template."""
    by_role = {w.get("role"): w for w in
               (_coord_template().get("windows") or [])
               if isinstance(w, dict)}
    for role in ["READER", *MIGRATED]:
        assert by_role.get(role, {}).get("mode") == "driven", (
            f"0319: {role} must be mode=driven"
        )


# ---------- untouched roles stay non-driven ----------


@pytest.mark.parametrize("role", UNTOUCHED)
def test_untouched_roles_not_driven(role: str) -> None:
    """0319 must NOT migrate PLANNER (interactive), MAINTAINER
    (self-loop), or the codex roles (Phase 3)."""
    by_role = {w.get("role"): w for w in
               (_coord_template().get("windows") or [])
               if isinstance(w, dict)}
    win = by_role.get(role)
    if win is not None:
        assert win.get("mode") != "driven", (
            f"0319: {role} must stay non-driven this phase (got "
            f"{win.get('mode')!r})"
        )


# ---------- launch leaves a driven pane idle ----------


def _env_setup():
    return launch_mod.gm_env.EnvSetup(
        env_type=None, activation="", source="(test)")


def _fake_run_collector(calls: list):
    import subprocess as _sp

    def _run(args, **kw):
        calls.append(list(args))
        return _sp.CompletedProcess(
            args=args,
            returncode=1 if "has-session" in args else 0,
            stdout="", stderr="")
    return _run


@pytest.mark.parametrize("name,role", [
    ("dev", "DEVELOPER"), ("ui", "UI-DEVELOPER"),
    ("tester", "TESTER"), ("stand", "STAND-KEEPER"),
])
def test_launch_creates_no_window_for_driven_role(
    tmp_path: Path, monkeypatch, name: str, role: str,
):
    """A mode=driven role gets NO tmux pane — coordd runs each of its
    turns as a managed subprocess. launch creates a window only for
    the paned roles and skips the driven one entirely."""
    calls: list = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run", _fake_run_collector(calls))
    cfg = {
        "session": "test",
        "windows": [
            {"name": "maintainer", "role": "MAINTAINER", "tool": "claude",
             "mode": "loop"},
            {"name": name, "role": role, "tool": "claude",
             "mode": "driven"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    created = set()
    for c in calls:
        if "-n" in c:
            created.add(c[c.index("-n") + 1])
    assert "maintainer" in created, "paned role must get a window"
    assert name not in created, (
        f"driven {role} must NOT get a tmux window"
    )


# ---------- coordd drives the migrated roles ----------


def _project(tmp_path: Path, *, role: str, queue: str,
             session_id: str = "sess-x") -> tuple[Path, Path]:
    project = tmp_path / "project"
    coord = project / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    win_name = {"DEVELOPER": "dev", "UI-DEVELOPER": "ui",
                "TESTER": "tester", "STAND-KEEPER": "stand"}[role]
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": win_name, "role": role, "tool": "claude",
             "mode": "driven"},
        ],
    }), encoding="utf-8")
    canon = project / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {queue: {"owner": role}},
        "roles": {role: {"lifecycle": "driven"}},
        "event_wake": {"by_tool": {"claude": "tmux_send_keys"}},
    }), encoding="utf-8")
    (coord / cd.REGISTRY_DIR / f"{role.lower()}.json").write_text(
        '{"role": "%s", "tool": "claude", "pid": 1, '
        '"session_id": "%s"}' % (role, session_id),
        encoding="utf-8")
    return coord, canon


def test_coordd_drives_developer_when_window_driven(
    tmp_path: Path, monkeypatch,
):
    """End-to-end: a feature_dev landing for the now-driven DEVELOPER
    makes coordd spawn a driven turn (claude --resume), not a
    press_enter wake."""
    coord, canon = _project(
        tmp_path, role="DEVELOPER", queue="feature_dev",
        session_id="sess-dev")

    captured: list = []

    def _fake_spawn(coord_, role_lower, session_id, pane, session_name,
                    bf, verbose, *, reg=None, force_fresh=False, **kw):
        captured.append((role_lower, session_id, force_fresh))
        return (True, "test")

    monkeypatch.setattr(cd, "_spawn_driven_turn", _fake_spawn)
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: pytest.fail(
            "driven DEVELOPER must NOT use press_enter wake"),
    )

    woke = cd._route_queue_event(
        coord, canon, "feature_dev", "0001-x.yaml", verbose=False)
    assert woke is True
    assert len(captured) == 1
    # Existing session → --resume continuation (not force-fresh).
    assert captured[0] == ("developer", "sess-dev", False)


def test_coordd_deploys_stand_on_stand_event(
    tmp_path: Path, monkeypatch,
):
    """1.6.0: a ``.stand`` change to `preparing` runs the COORDD deploy
    engine (deploy_lease) directly. STAND-KEEPER is retired — no driven
    turn, no press_enter."""
    coord, canon = _project(
        tmp_path, role="STAND-KEEPER", queue=".stand",
        session_id="sess-sk")
    (coord / ".stand").mkdir(parents=True, exist_ok=True)
    import yaml as _y
    (coord / ".stand" / "state.yaml").write_text(_y.safe_dump({
        "state": "preparing", "queue": [], "history": [],
        "active_lease": {"lease_id": "L1", "profile": "full-deploy",
                         "worktree": str(coord.parent / "wt"),
                         "holder_role": "TESTER", "task": "0001"}}),
        encoding="utf-8")

    import threading
    done = threading.Event()
    monkeypatch.setattr("greatminds.cli.stand.deploy_lease",
                        lambda c, *, lease_id=None, **k: (done.set(),
                                                          (0, "ok"))[-1])
    monkeypatch.setattr(cd, "_spawn_driven_turn",
                        lambda *a, **kw: pytest.fail(
                            "1.6.0: .stand deploys via coordd, not an SK turn"))
    cd._DEPLOYING_LEASES.discard("L1")

    woke = cd._route_queue_event(
        coord, canon, ".stand", "state.yaml", verbose=False)
    assert woke is True
    assert done.wait(timeout=3), "coordd must run deploy_lease"
