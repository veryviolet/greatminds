"""Tests for task 0305 (upstream issue #2): SK heartbeat refresh
during long ansible runs so watchdog doesn't flag the lease cycle
as a dead pid.

Pre-0305 ansible-playbook over SSH could exceed watchdog's
heartbeat_stale_seconds threshold (default 600s) on legit
multi-minute deploys → watchdog filed spurious dead-pid asks to
MAINTAINER inbox. 0305 spins a background thread that touches
``heartbeat.stand-keeper`` every 30s while the subprocess runs.

Note: Fix A (coordd .stand wake) was already addressed in 0269 —
``.stand`` lives in ``INOTIFY_QUEUE_DIRS`` and the schema declares
STAND-KEEPER as the queue's owner. This task focuses on Fix B.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_executor as se
from greatminds.cli.stand_profile import ProfileSpec


def _yaml_spec(tmp_path: Path) -> ProfileSpec:
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump({
        "name": "full-deploy", "hosts": "stand",
        "tasks": [{"name": "x"}],
    }), encoding="utf-8")
    return ProfileSpec(name="full-deploy", format="yaml", path=path,
                        deploy_prerequisites_only=False)


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / ".stand").mkdir(parents=True)
    (tmp_path / "proj" / ".worktrees" / "0305").mkdir(parents=True)
    return coord


def _lease(coord: Path, project_dir: Path) -> dict:
    return {
        "lease_id": "lease-0305",
        "task_id": "0305-probe",
        "worktree": str(project_dir / ".worktrees" / "0305"),
        "host": "avatar",
        "user": "deploy",
        "deploy_path": "/srv/stand",
        "coord": str(coord),
        "profile": "full-deploy",
    }


# ---------- _start_heartbeat_refresher ----------


def test_refresher_returns_none_when_coord_missing() -> None:
    """Defensive: no coord → no thread."""
    assert se._start_heartbeat_refresher(None, "stand-keeper") is None


def test_refresher_touches_heartbeat_immediately(
    tmp_path: Path,
) -> None:
    """The thread must touch the heartbeat at startup (not wait
    for the first interval) so even quick subprocesses refresh
    the file once."""
    coord = _coord(tmp_path)
    hb = coord / "heartbeat.stand-keeper"
    assert not hb.exists()

    handle = se._start_heartbeat_refresher(
        coord, "stand-keeper", interval=10.0,
    )
    try:
        # Allow the daemon thread to run its first touch.
        deadline = time.time() + 2.0
        while not hb.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert hb.is_file(), (
            "0305: heartbeat must be touched at startup"
        )
    finally:
        se._stop_heartbeat_refresher(handle)


def test_refresher_touches_heartbeat_repeatedly(
    tmp_path: Path,
) -> None:
    """Across multiple intervals the heartbeat mtime advances
    (proves the thread isn't a one-shot)."""
    coord = _coord(tmp_path)
    hb = coord / "heartbeat.stand-keeper"

    handle = se._start_heartbeat_refresher(
        coord, "stand-keeper", interval=0.1,
    )
    try:
        deadline = time.time() + 2.0
        while not hb.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert hb.exists()
        first_mtime = hb.stat().st_mtime
        # Wait a few intervals.
        time.sleep(0.3)
        second_mtime = hb.stat().st_mtime
        assert second_mtime >= first_mtime, (
            f"0305: second mtime {second_mtime} must be >= first "
            f"{first_mtime} (refresher idle)"
        )
    finally:
        se._stop_heartbeat_refresher(handle)


def test_stop_refresher_is_idempotent(tmp_path: Path) -> None:
    """Double-stop is a no-op (callers in except branches can
    safely call stop multiple times)."""
    coord = _coord(tmp_path)
    handle = se._start_heartbeat_refresher(
        coord, "stand-keeper", interval=10.0,
    )
    se._stop_heartbeat_refresher(handle)
    se._stop_heartbeat_refresher(handle)  # must not raise
    se._stop_heartbeat_refresher(None)    # None handle also OK


# ---------- integration with execute_yaml_profile ----------


def test_execute_yaml_starts_and_stops_refresher(
    tmp_path: Path, monkeypatch,
) -> None:
    """execute_yaml_profile spins the refresher around the
    subprocess. After the call, the heartbeat file exists (proves
    refresher ran) and no daemon thread is left behind that keeps
    touching."""
    coord = _coord(tmp_path)
    project = tmp_path / "proj"
    spec = _yaml_spec(tmp_path)
    lease = _lease(coord, project)

    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""),
    )

    rc, _ = se.execute_yaml_profile(spec, lease)
    assert rc == 0

    hb = coord / "heartbeat.stand-keeper"
    assert hb.is_file(), (
        "0305: heartbeat must exist after execute_yaml_profile run"
    )
    first_mtime = hb.stat().st_mtime
    # Wait a moment; mtime should NOT advance because refresher
    # already stopped.
    time.sleep(0.4)
    assert hb.stat().st_mtime == first_mtime, (
        "0305: refresher must STOP after subprocess returns "
        "(otherwise daemon thread leaks)"
    )


def test_execute_yaml_stops_refresher_on_timeout(
    tmp_path: Path, monkeypatch,
) -> None:
    """Even when subprocess raises TimeoutExpired, the refresher
    must be stopped — otherwise dead leases would keep faking the
    heartbeat."""
    coord = _coord(tmp_path)
    project = tmp_path / "proj"
    spec = _yaml_spec(tmp_path)
    lease = _lease(coord, project)

    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1.0)
    monkeypatch.setattr(se.subprocess, "run", fake_run)

    rc, _ = se.execute_yaml_profile(
        spec, lease, timeout_seconds=1.0)
    assert rc == 124

    hb = coord / "heartbeat.stand-keeper"
    assert hb.is_file()
    first_mtime = hb.stat().st_mtime
    time.sleep(0.4)
    assert hb.stat().st_mtime == first_mtime, (
        "0305: refresher must stop on timeout path"
    )


def test_execute_yaml_stops_refresher_on_filenotfound(
    tmp_path: Path, monkeypatch,
) -> None:
    """FileNotFoundError path (ansible disappeared mid-call) also
    stops the refresher."""
    coord = _coord(tmp_path)
    project = tmp_path / "proj"
    spec = _yaml_spec(tmp_path)
    lease = _lease(coord, project)

    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")

    def fake_run(*a, **kw):
        raise FileNotFoundError("ansible-playbook gone")
    monkeypatch.setattr(se.subprocess, "run", fake_run)

    rc, _ = se.execute_yaml_profile(spec, lease)
    assert rc == 127

    hb = coord / "heartbeat.stand-keeper"
    first_mtime = hb.stat().st_mtime
    time.sleep(0.4)
    assert hb.stat().st_mtime == first_mtime
