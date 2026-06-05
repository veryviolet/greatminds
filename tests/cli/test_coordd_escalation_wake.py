"""Escalation wakes a SLEEPING MAINTAINER, never a working one.

Autonomy backstop (Phase A): when coordd escalates (a driven turn failed
repeatedly, or a deploy keeps failing), MAINTAINER should act now — not on
its next hourly self-loop. But a MAINTAINER mid-turn must not be interrupted.
The wake is gated on heartbeat freshness.
"""
from __future__ import annotations

import time
from pathlib import Path

from greatminds.cli import coordd as cd


def test_wake_skips_when_maintainer_fresh(tmp_path: Path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    # Fresh heartbeat → MAINTAINER is actively working.
    (coord / "heartbeat.maintainer").write_text("x", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(cd, "push_to_role",
                        lambda *a, **k: calls.append(a))
    cd._wake_maintainer_if_asleep(coord)
    assert calls == [], "must NOT nudge a working MAINTAINER"


def test_wake_nudges_when_maintainer_stale(tmp_path: Path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    hb = coord / "heartbeat.maintainer"
    hb.write_text("x", encoding="utf-8")
    # Age the heartbeat well past the fresh guard → MAINTAINER idle/asleep.
    old = time.time() - (cd.PUSH_FRESH_GUARD_SEC + 600)
    import os
    os.utime(hb, (old, old))
    calls: list = []
    monkeypatch.setattr(cd, "push_to_role",
                        lambda *a, **k: calls.append(a))
    cd._wake_maintainer_if_asleep(coord)
    assert len(calls) == 1
    assert calls[0][1] == "MAINTAINER"


def test_wake_nudges_when_no_heartbeat(tmp_path: Path, monkeypatch) -> None:
    # No heartbeat file at all → treat as idle, wake it.
    coord = tmp_path / "coordination"
    coord.mkdir()
    calls: list = []
    monkeypatch.setattr(cd, "push_to_role",
                        lambda *a, **k: calls.append(a))
    cd._wake_maintainer_if_asleep(coord)
    assert len(calls) == 1
