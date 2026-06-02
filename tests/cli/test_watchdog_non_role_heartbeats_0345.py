"""Tests for task 0345 (part 1): watchdog must NOT flag heartbeat files
that map to no schema role as active-fleet stale failures.

EXPLORER on a ready 1.4.x stand saw watchdog report STALE HEARTBEATS for
``heartbeat.planner`` and ``heartbeat.review_sessions`` at the 10m
default threshold while every agent pid was alive. Those filenames are a
window name and a queue name — NOT role heartbeats (the role writes
``heartbeat.architect-planner`` / ``heartbeat.explorer``). They are
legacy artifacts on a long-lived coordination dir; counting them stale
falsely reports a healthy fleet as failing. They must be segregated as
non-role/ignored, and per-lifecycle thresholds must still apply to real
role heartbeats (incl. ``.fast`` variants).
"""
from __future__ import annotations

from greatminds.cli import watchdog as wd


_ROLES = {
    "ARCHITECT-PLANNER": {"lifecycle": "interactive"},
    "STAND-KEEPER": {"lifecycle": "driven"},
    "UI-DEVELOPER": {"lifecycle": "driven"},
    "MAINTAINER": {"lifecycle": "self-loop", "heartbeat_stale_seconds": 4200},
}
_BY_LIFECYCLE = {"self-loop": 4200, "driven": 14400, "interactive": 86400}


# ---------- role-key resolution ----------


def test_role_key_resolves_known_roles() -> None:
    assert wd._heartbeat_role_key("heartbeat.architect-planner", _ROLES) \
        == "ARCHITECT-PLANNER"
    assert wd._heartbeat_role_key("heartbeat.stand-keeper", _ROLES) \
        == "STAND-KEEPER"


def test_role_key_resolves_fast_variant() -> None:
    # UI-DEVELOPER's parallel-pipeline heartbeat must resolve to the role
    assert wd._heartbeat_role_key("heartbeat.ui-developer.fast", _ROLES) \
        == "UI-DEVELOPER"


def test_role_key_none_for_window_or_queue_names() -> None:
    assert wd._heartbeat_role_key("heartbeat.planner", _ROLES) is None
    assert wd._heartbeat_role_key("heartbeat.review_sessions", _ROLES) is None


# ---------- threshold resolution unchanged for known roles ----------


def test_interactive_role_gets_lifecycle_threshold() -> None:
    thr = wd._heartbeat_threshold("heartbeat.architect-planner", _ROLES,
                                  _BY_LIFECYCLE, 600.0)
    assert thr == 86400.0  # interactive override, not the 600 default


def test_fast_heartbeat_gets_role_lifecycle_threshold() -> None:
    thr = wd._heartbeat_threshold("heartbeat.ui-developer.fast", _ROLES,
                                  _BY_LIFECYCLE, 600.0)
    assert thr == 14400.0  # driven, not the 600 default


def test_explicit_role_override_wins() -> None:
    thr = wd._heartbeat_threshold("heartbeat.maintainer", _ROLES,
                                  _BY_LIFECYCLE, 600.0)
    assert thr == 4200.0


def test_unknown_role_falls_through_to_default() -> None:
    # (kept for back-compat: callers that don't pre-filter still get a
    # number, but the watchdog loop now skips non-role files entirely)
    thr = wd._heartbeat_threshold("heartbeat.planner", _ROLES,
                                  _BY_LIFECYCLE, 600.0)
    assert thr == 600.0


# ---------- end-to-end watchdog CLI ----------


def _project(tmp_path):
    import yaml
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "roles": {
            "ARCHITECT-PLANNER": {"lifecycle": "interactive"},
            "EXPLORER": {"lifecycle": "driven"},
        },
        "watchdog": {
            "heartbeat_stale_seconds": 600,
            "heartbeat_stale_seconds_by_lifecycle": {
                "interactive": 86400, "driven": 14400},
            "intent_orphan_seconds": 300,
        },
        "queues": {},
    }), encoding="utf-8")
    coord = tmp_path / "proj" / "coordination"
    (coord / "stand_requests").mkdir(parents=True)
    return canon, coord


def _age_file(p, seconds):
    import os
    past = __import__("time").time() - seconds
    os.utime(p, (past, past))


def test_watchdog_ignores_non_role_heartbeats(tmp_path):
    from click.testing import CliRunner
    canon, coord = _project(tmp_path)
    # legacy non-role files, very old
    for name in ("heartbeat.planner", "heartbeat.review_sessions"):
        f = coord / name
        f.write_text("", encoding="utf-8")
        _age_file(f, 3600)
    # a real interactive role heartbeat, also old but within its 24h
    # override → must NOT be stale
    pl = coord / "heartbeat.architect-planner"
    pl.write_text("", encoding="utf-8")
    _age_file(pl, 3600)

    res = CliRunner().invoke(wd.watchdog, [
        "--project-dir", str(coord), "--canon-dir", str(canon)],
        catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "STALE HEARTBEATS" not in res.output, res.output
    # the legacy files are surfaced as ignored, not as failures
    assert "non-role/legacy" in res.output
    assert "heartbeat.planner" in res.output


def test_watchdog_still_flags_real_stale_role(tmp_path):
    from click.testing import CliRunner
    canon, coord = _project(tmp_path)
    # EXPLORER is driven (14400 threshold); make it older than that
    ex = coord / "heartbeat.explorer"
    ex.write_text("", encoding="utf-8")
    _age_file(ex, 20000)
    res = CliRunner().invoke(wd.watchdog, [
        "--project-dir", str(coord), "--canon-dir", str(canon)],
        catch_exceptions=False)
    assert "STALE HEARTBEATS" in res.output
    assert "heartbeat.explorer" in res.output
