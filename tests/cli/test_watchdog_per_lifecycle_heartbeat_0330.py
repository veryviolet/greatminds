"""Tests for task 0330 (0311 Phase 5): per-role / per-lifecycle
watchdog heartbeat stale threshold.

Bug (EXPLORER-found, code-confirmed): the watchdog used a single
global ``heartbeat_stale_seconds=600`` for every role. A heartbeat is
touched only while a role runs a tick; non-continuous roles
legitimately idle far longer than 10min between ticks (driven panes
are idle bash between events; self-loop wakes on a ~1h timer;
interactive is human-paced). So ``greatminds watchdog`` flagged every
alive-but-idle role stale (maintainer/tester/writer/explorer) while
"agent pids: all alive".

Fix (PLANNER scope-correction 16:41 — covers ALL non-continuous
lifecycles, not just self-loop): resolve the threshold per heartbeat
file — ``roles.<ROLE>.heartbeat_stale_seconds`` (explicit) →
``watchdog.heartbeat_stale_seconds_by_lifecycle[<lifecycle>]`` → global
default. self-loop=4200 (1h cadence+margin), driven=14400 (max-idle
between events), interactive=86400 (human-paced); the tight 600s
default applies only to continuous-signal roles. The authoritative
liveness check for event-driven roles is the dead-pid scan, NOT
heartbeat age.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli import watchdog as wd
from greatminds.core.paths import find_canon_dir


# ---------- _heartbeat_threshold unit ----------


ROLES = {
    "MAINTAINER": {"lifecycle": "self-loop"},
    "TESTER": {"lifecycle": "driven"},
    "ARCHITECT-PLANNER": {"lifecycle": "interactive"},
    "STAND-KEEPER": {"lifecycle": "driven",
                     "heartbeat_stale_seconds": 1234},
}
BY_LIFECYCLE = {"self-loop": 4200, "driven": 14400, "interactive": 86400}


def test_threshold_self_loop_uses_lifecycle() -> None:
    assert wd._heartbeat_threshold(
        "heartbeat.maintainer", ROLES, BY_LIFECYCLE, 600) == 4200


def test_threshold_driven_uses_lifecycle_not_global() -> None:
    """The core scope-correction: a driven role gets the driven
    (max-idle) threshold, NOT the tight 600s global."""
    assert wd._heartbeat_threshold(
        "heartbeat.tester", ROLES, BY_LIFECYCLE, 600) == 14400


def test_threshold_interactive_uses_lifecycle() -> None:
    assert wd._heartbeat_threshold(
        "heartbeat.architect-planner", ROLES, BY_LIFECYCLE, 600) == 86400


def test_threshold_explicit_per_role_override_wins() -> None:
    assert wd._heartbeat_threshold(
        "heartbeat.stand-keeper", ROLES, BY_LIFECYCLE, 600) == 1234


def test_threshold_unknown_role_falls_back_to_default() -> None:
    assert wd._heartbeat_threshold(
        "heartbeat.nobody", ROLES, BY_LIFECYCLE, 600) == 600


def test_threshold_hyphenated_role_matches_case_insensitively() -> None:
    assert wd._heartbeat_threshold(
        "heartbeat.MAINTAINER", ROLES, BY_LIFECYCLE, 600) == 4200


# ---------- watchdog command end-to-end ----------


def _canon(tmp_path: Path) -> Path:
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "roles": {
            "MAINTAINER": {"lifecycle": "self-loop"},
            "TESTER": {"lifecycle": "driven"},
            "ARCHITECT-PLANNER": {"lifecycle": "interactive"},
            # A real role with NO lifecycle entry → continuous-signal:
            # keeps the tight 600s default (used by the continuous-signal
            # test below). 0345: only KNOWN-role heartbeats are eligible
            # to be flagged stale; non-role files are ignored.
            "UI-DEVELOPER": {},
        },
        "watchdog": {
            "heartbeat_stale_seconds": 600,
            "heartbeat_stale_seconds_by_lifecycle": {
                "self-loop": 4200, "driven": 14400, "interactive": 86400},
        },
        "queues": {},
    }), encoding="utf-8")
    return canon


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / "stand_requests").mkdir(parents=True)
    return project


def _touch_age(path: Path, age_seconds: float) -> None:
    path.write_text("x", encoding="utf-8")
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))


def _run(project: Path, canon: Path):
    return CliRunner().invoke(wd.watchdog, [
        "--project-dir", str(project), "--canon-dir", str(canon),
    ], catch_exceptions=False)


def _stale_section(output: str) -> str:
    parts = output.split("STALE HEARTBEATS", 1)
    return parts[1] if len(parts) > 1 else ""


def test_idle_but_alive_roles_not_flagged_within_window(tmp_path: Path):
    """The bug's exact false positives: maintainer 32m (self-loop),
    tester 45m + explorer 19m (driven), planner 60m (interactive) —
    all WITHIN their windows → none flagged stale."""
    canon = _canon(tmp_path)
    project = _project(tmp_path)
    coord = project / "coordination"
    _touch_age(coord / "heartbeat.maintainer", 32 * 60)
    _touch_age(coord / "heartbeat.tester", 45 * 60)
    _touch_age(coord / "heartbeat.architect-planner", 60 * 60)

    result = _run(project, canon)
    assert result.exit_code == 0, result.output
    section = _stale_section(result.output)
    for name in ("heartbeat.maintainer", "heartbeat.tester",
                 "heartbeat.architect-planner"):
        assert name not in section, (
            f"0330: idle-but-alive {name} must NOT be flagged stale")


def test_each_lifecycle_flagged_past_its_own_window(tmp_path: Path):
    """Past each lifecycle window the role IS flagged (the override
    widens, not disables, staleness)."""
    canon = _canon(tmp_path)
    project = _project(tmp_path)
    coord = project / "coordination"
    _touch_age(coord / "heartbeat.maintainer", 4800)        # >4200
    _touch_age(coord / "heartbeat.tester", 15000)           # >14400
    # interactive planner fresh enough (well under 24h) → not flagged
    _touch_age(coord / "heartbeat.architect-planner", 3600)

    result = _run(project, canon)
    assert result.exit_code == 0, result.output
    section = _stale_section(result.output)
    assert "heartbeat.maintainer" in section
    assert "heartbeat.tester" in section
    assert "heartbeat.architect-planner" not in section


def test_continuous_signal_role_keeps_tight_default(tmp_path: Path):
    """A KNOWN role with no lifecycle entry (continuous-signal) keeps the
    tight 600s default and is flagged at 11min.

    0345: the heartbeat must map to a real schema role — UI-DEVELOPER has
    no lifecycle entry in the fixture, so it keeps the default. (A
    non-role filename like the old ``heartbeat.ui-fast`` is now ignored
    as legacy, not flagged — see test_watchdog_non_role_heartbeats_0345.)
    """
    canon = _canon(tmp_path)
    project = _project(tmp_path)
    coord = project / "coordination"
    _touch_age(coord / "heartbeat.ui-developer", 660)  # 11 min, default thr

    result = _run(project, canon)
    assert result.exit_code == 0, result.output
    assert "heartbeat.ui-developer" in _stale_section(result.output)


def test_dead_pid_still_flagged_regardless_of_heartbeat(tmp_path: Path):
    """PLANNER requirement: a truly dead pid is STILL flagged — the
    authoritative driven-liveness signal is the dead-pid scan, separate
    from (widened) heartbeat staleness."""
    canon = _canon(tmp_path)
    project = _project(tmp_path)
    coord = project / "coordination"
    reg = coord / ".agent_registry"
    reg.mkdir(parents=True)
    # A fresh heartbeat (would NOT be stale) but a dead pid.
    _touch_age(coord / "heartbeat.explorer", 5)
    (reg / "explorer.json").write_text(
        '{"role": "EXPLORER", "pid": 2147480000}', encoding="utf-8")

    result = _run(project, canon)
    assert result.exit_code == 0, result.output
    assert "DEAD AGENT PIDS" in result.output
    assert "explorer" in result.output.split("DEAD AGENT PIDS", 1)[1]


# ---------- canon fidelity ----------


def test_canon_schema_declares_per_lifecycle_thresholds() -> None:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    by_lc = ((doc.get("watchdog") or {})
             .get("heartbeat_stale_seconds_by_lifecycle") or {})
    assert by_lc.get("self-loop", 0) >= 3600
    assert by_lc.get("driven", 0) > 600, (
        "0330: driven lifecycle must NOT keep the tight 600s")
    assert by_lc.get("interactive", 0) >= by_lc.get("driven", 0)
    assert (doc.get("roles", {}).get("MAINTAINER", {}).get("lifecycle")
            == "self-loop")
