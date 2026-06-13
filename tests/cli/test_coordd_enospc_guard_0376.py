"""Tests for task 0376: ENOSPC / low-disk resilience for driven turns.

A full root disk silently broke driven turns — codex/claude refreshed an
auth token but could not persist auth.json (ENOSPC), so the next turn
reused a consumed refresh token and failed with refresh_token_reused /
token_expired (looks like an auth bug, not a disk bug). 0376 adds:

  1. A disk preflight that refuses to spawn a driven turn below threshold
     and surfaces an explicit ENOSPC blocker (never a silent zero-work turn).
  2. Failure-detail enrichment: auth/persistence failure text gets the live
     disk status appended so operators can tell auth-vs-ENOSPC apart.
  3. A retention cap on coordd's own unbounded .turns/ logs.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from greatminds.cli import coordd as cd


# ---------- disk preflight ----------


def _fake_usage(free_mb: float, total_mb: float = 100_000.0):
    """A shutil.disk_usage-compatible namedtuple-ish object in bytes."""
    Usage = shutil._ntuple_diskusage  # the real return type
    total = int(total_mb * 1024 * 1024)
    free = int(free_mb * 1024 * 1024)
    used = total - free
    return Usage(total=total, used=used, free=free)


def test_disk_preflight_ok_when_plenty_free(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=50_000))
    ok, diag = cd._disk_preflight(tmp_path)
    assert ok is True
    assert diag == ""


def test_disk_preflight_blocks_below_absolute_mb(monkeypatch, tmp_path):
    # 100 MB free, threshold default 512 MB → block.
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=100, total_mb=100_000))
    ok, diag = cd._disk_preflight(tmp_path)
    assert ok is False
    assert "ENOSPC" in diag
    assert "NOT spawned" in diag


def test_disk_preflight_blocks_below_percent(monkeypatch, tmp_path):
    # 5000 MB free but on a 1 TB disk → 0.49% < 1.0% threshold → block,
    # even though absolute MB is comfortably above 512.
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda p: _fake_usage(free_mb=5000, total_mb=1_000_000))
    ok, diag = cd._disk_preflight(tmp_path)
    assert ok is False
    assert "%" in diag


def test_disk_preflight_fails_open_on_oserror(monkeypatch, tmp_path):
    def _boom(p):
        raise OSError("cannot stat")
    monkeypatch.setattr(shutil, "disk_usage", _boom)
    ok, diag = cd._disk_preflight(tmp_path)
    assert ok is True  # never block a turn on an unknowable disk


# ---------- preflight gates _maybe_drive_driven_role ----------


def _driven_schema(monkeypatch):
    """Make role look like a migrated driven claude role."""
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda canon, role: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda doc, role: "driven")


def test_maybe_drive_blocks_spawn_when_disk_low(monkeypatch, tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".turns").mkdir(parents=True)
    (coord / ".locks").mkdir(parents=True)
    _driven_schema(monkeypatch)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=10, total_mb=100_000))
    spawned: list = []
    monkeypatch.setattr(cd, "_spawn_driven_turn",
                        lambda *a, **k: spawned.append(a) or (True, "spawned"))
    notes: list = []
    monkeypatch.setattr(cd, "_note_low_disk_blocker",
                        lambda *a, **k: notes.append(a))

    result = cd._maybe_drive_driven_role(
        coord, tmp_path, {}, ("dev", "claude"), "DEVELOPER", verbose=False)

    assert result is False
    assert spawned == []          # turn NOT spawned
    assert len(notes) == 1        # blocker surfaced


def test_low_disk_blocker_writes_operator_visible_status(tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".turns").mkdir(parents=True)
    (coord / ".locks").mkdir(parents=True)

    cd._note_low_disk_blocker(
        coord, "developer", "LOW DISK / ENOSPC risk", verbose=False)

    status = json.loads(
        cd._driven_retry_path(coord, "developer").read_text())
    assert status["klass"] == "low_disk"
    assert status["escalated"] is True
    assert "ENOSPC" in status["detail"]


def test_maybe_drive_spawns_when_disk_ok(monkeypatch, tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".turns").mkdir(parents=True)
    (coord / ".locks").mkdir(parents=True)
    (coord / ".registry").mkdir(parents=True, exist_ok=True)
    _driven_schema(monkeypatch)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=50_000))
    monkeypatch.setattr(cd, "read_registry", lambda *a, **k: {})
    monkeypatch.setattr(cd, "_driven_bootstrap_path", lambda *a, **k: None)
    spawned: list = []
    monkeypatch.setattr(
        cd, "_spawn_driven_turn",
        lambda *a, **k: spawned.append(a) or (True, "spawned"))

    result = cd._maybe_drive_driven_role(
        coord, tmp_path, {}, ("dev", "claude"), "DEVELOPER", verbose=False)

    assert result is True
    assert len(spawned) == 1      # healthy-disk path NOT regressed


# ---------- failure-detail enrichment ----------


def test_enrich_appends_disk_status_on_auth_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=10, total_mb=100_000))
    out = cd._enrich_failure_detail(
        "turn failed: refresh_token_reused", tmp_path)
    assert "disk: free=" in out
    assert "LIKELY ENOSPC-CAUSED AUTH CORRUPTION" in out


def test_enrich_noop_without_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=10))
    out = cd._enrich_failure_detail("some unrelated error", tmp_path)
    assert out == "some unrelated error"


def test_enrich_marker_but_disk_healthy_omits_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _fake_usage(free_mb=50_000))
    out = cd._enrich_failure_detail("token_expired", tmp_path)
    assert "disk: free=" in out          # status still informative
    assert "LIKELY ENOSPC" not in out    # but no false-cause hint


# ---------- .turns retention ----------


def test_prune_turn_logs_keeps_recent_per_role(tmp_path):
    d = tmp_path / "coordination" / ".turns"
    d.mkdir(parents=True)
    coord = tmp_path / "coordination"
    # 5 logs for a hyphenated role + 2 for another.
    for ts in range(5):
        (d / f"architect-reviewer-2026060{ts}T000000Z.log").write_text("x")
    for ts in range(2):
        (d / f"developer-2026060{ts}T000000Z.log").write_text("x")

    removed = cd._prune_turn_logs(coord, keep_per_role=2)

    assert removed == 3  # reviewer 5→2 (drop 3); developer 2→2 (drop 0)
    remaining = sorted(p.name for p in d.glob("*.log"))
    # newest two reviewer logs kept (ts 3,4) + both developer logs.
    assert "architect-reviewer-20260604T000000Z.log" in remaining
    assert "architect-reviewer-20260603T000000Z.log" in remaining
    assert "architect-reviewer-20260600T000000Z.log" not in remaining
    assert len([r for r in remaining if r.startswith("developer-")]) == 2


def test_prune_turn_logs_noop_when_dir_missing(tmp_path):
    assert cd._prune_turn_logs(tmp_path / "coordination") == 0
