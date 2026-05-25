"""Tests for task 0169: coordd inotify watcher.

Pre-0169 the main loop blocked on ``time.sleep(interval)``; reaction
latency was bounded below by ``interval`` (default 1.0s, minimum
0.2s). 0169 adds an inotify_simple watcher on coordination's inbox
and active queue directories. The main loop's wait becomes
``inotify.read(timeout=interval*1000)`` — blocks up to interval but
returns early on any file event. Reaction latency drops to whatever
inotify delivers (typically <1ms on Linux).

Linux-only — on non-Linux hosts the optional import fails and coordd
falls back to plain polling.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import coordd as coordd_mod


def _make_coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    coord.mkdir()
    for sub in ("inbox", "feature_dev", "stand_requests"):
        (coord / sub).mkdir()
    return coord


# ---------- inotify dir list ----------


def test_inotify_queue_dirs_covers_inbox_and_active_queues() -> None:
    """0169 contract: the watch list includes the inbox AND every
    active queue an agent might claim from. Missing one of these
    would re-introduce poll-bound latency for tasks landing in that
    queue."""
    queues = set(coordd_mod.INOTIFY_QUEUE_DIRS)
    assert "inbox" in queues
    # Product pipeline.
    for q in ("feature_inbox", "feature_plan", "feature_dev",
              "feature_ui_dev", "feature_docs", "feature_test",
              "feature_docs_review", "feature_review",
              "feature_blocked", "verified"):
        assert q in queues, f"0169: queue dir {q!r} missing from watch list"
    # Stand pipeline.
    for q in ("stand_requests", "stand_wip", "stand_done"):
        assert q in queues, f"0169: queue dir {q!r} missing from watch list"
    # Review sessions.
    assert "review_sessions" in queues


# ---------- watcher construction ----------


def test_make_inotify_watcher_returns_object_on_linux(tmp_path: Path) -> None:
    """Happy path: on Linux with inotify_simple installed, the
    factory returns a watcher object (not None). The test runs on
    Linux per MAINTAINER.md operational scope."""
    pytest.importorskip("inotify_simple")
    coord = _make_coord(tmp_path)
    watcher = coordd_mod._make_inotify_watcher(coord, verbose=False)
    assert watcher is not None, (
        "0169: factory must return a watcher on Linux with inotify_simple"
    )


def test_make_inotify_watcher_returns_none_when_dep_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Graceful degradation: if ``inotify_simple`` import fails (non-
    Linux host, unusual install), the factory returns None and the
    main loop falls back to plain polling."""
    coord = _make_coord(tmp_path)

    # Patch the inner class to raise ImportError on construction.
    def fake_init(self, coord, verbose=False):
        raise ImportError("inotify_simple not available (test stub)")
    monkeypatch.setattr(coordd_mod._InotifyWatcher, "__init__", fake_init)

    watcher = coordd_mod._make_inotify_watcher(coord, verbose=False)
    assert watcher is None


def test_make_inotify_watcher_returns_none_on_oserror(
    tmp_path: Path, monkeypatch,
) -> None:
    """Defensive: kernel inotify FD exhaustion (rare but seen on
    overloaded hosts) raises OSError. Factory must catch and degrade
    rather than crash coordd startup."""
    coord = _make_coord(tmp_path)

    def fake_init(self, coord, verbose=False):
        raise OSError("watch budget exhausted (test stub)")
    monkeypatch.setattr(coordd_mod._InotifyWatcher, "__init__", fake_init)

    watcher = coordd_mod._make_inotify_watcher(coord, verbose=False)
    assert watcher is None


# ---------- watcher behavior ----------


def test_watcher_read_returns_within_timeout_on_no_event(tmp_path: Path) -> None:
    """0169: when no event fires, ``read_or_timeout(timeout_s)`` must
    return within ``timeout_s`` (with some slop). Without this, coordd
    would hang forever between events; the polling tick wouldn't
    fire and stale-kick / dead-pid checks would stop."""
    pytest.importorskip("inotify_simple")
    import time
    coord = _make_coord(tmp_path)
    watcher = coordd_mod._make_inotify_watcher(coord, verbose=False)
    assert watcher is not None
    t0 = time.monotonic()
    events = watcher.read_or_timeout(0.1)
    elapsed = time.monotonic() - t0
    assert events == []
    # Slop budget: inotify_simple's read can take up to ~2x the
    # timeout in extreme cases (CI under load); generous bound.
    assert elapsed < 1.0, (
        f"0169: read_or_timeout took {elapsed:.2f}s, expected near 0.1s"
    )


def test_watcher_wakes_early_on_new_inbox_file(tmp_path: Path) -> None:
    """0169 core contract: when a file lands in coord/inbox/<role>/
    while the watcher is blocked in ``read_or_timeout``, the call
    returns early with a non-empty event list. Pre-0169 the main
    loop would have slept the full interval before noticing."""
    pytest.importorskip("inotify_simple")
    import threading
    import time
    coord = _make_coord(tmp_path)
    # Seed a role inbox dir so the watcher adds it on init.
    role_dir = coord / "inbox" / "developer"
    role_dir.mkdir()
    watcher = coordd_mod._make_inotify_watcher(coord, verbose=False)
    assert watcher is not None

    def producer():
        time.sleep(0.05)  # let read_or_timeout start first
        (role_dir / "wake-0001-test.md").write_text("x", encoding="utf-8")

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    t0 = time.monotonic()
    events = watcher.read_or_timeout(2.0)
    elapsed = time.monotonic() - t0
    t.join()

    assert events, (
        "0169: watcher must return events when a file lands in the "
        "watched inbox dir"
    )
    assert elapsed < 1.0, (
        f"0169: watcher should return early on event, took {elapsed:.2f}s"
    )


def test_watcher_handles_missing_subdir_gracefully(tmp_path: Path) -> None:
    """Defensive: a coord/ dir that's missing some of the queue
    subdirs (fresh setup before any tasks landed) must not crash
    the watcher. Each missing subdir is silently skipped."""
    pytest.importorskip("inotify_simple")
    coord = tmp_path / "coordination"
    coord.mkdir()
    # ONLY inbox dir; others missing.
    (coord / "inbox").mkdir()

    watcher = coordd_mod._make_inotify_watcher(coord, verbose=False)
    assert watcher is not None
