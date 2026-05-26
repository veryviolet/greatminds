"""Tests for task 0245 (0242c, Phase 3+4 of 0242): SK + TESTER /
EXPLORER migration to the new lease API.

Phase 3 (SK): coordd's inotify watcher includes
``coordination/.stand/`` so state-file transitions wake the daemon
sub-second. The actual SK deploy playbook lives in PROJECT.md
(project-specific) — greatminds canon just exposes the lease/release
CLI + the state-file event.

Phase 4 (TESTER / EXPLORER): role-doc migration to the lease API.
The CLI surface itself was shipped in 0244; this phase rewrites
the workflow docs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import coordd as coordd_mod
from greatminds.core.paths import find_canon_dir


# ---------- coordd inotify includes .stand ----------


def test_inotify_watches_stand_state_dir() -> None:
    """0245 contract: coordd's INOTIFY_QUEUE_DIRS includes the
    ``.stand`` directory so state.yaml transitions fire wakes
    sub-second instead of waiting for the next poll tick."""
    assert ".stand" in coordd_mod.INOTIFY_QUEUE_DIRS


def test_inotify_watcher_registers_stand_when_dir_exists(
    tmp_path: Path,
) -> None:
    """0245 wiring pin: _InotifyWatcher's wd→queue mapping records
    .stand when the directory exists on the watched project."""
    pytest.importorskip("inotify_simple")
    coord = tmp_path / "coord"
    (coord / ".stand").mkdir(parents=True)
    w = coordd_mod._InotifyWatcher(coord, verbose=False)
    assert ".stand" in set(w._wd_to_queue.values())


# ---------- role-doc pins (0244 → 0245 migration) ----------


def test_stand_keeper_doc_references_lease_api() -> None:
    """0245 prose pin: STAND-KEEPER.md cites the new
    `greatminds stand ready/down/up` workflow."""
    text = (find_canon_dir() / "roles" / "STAND-KEEPER.md").read_text(
        encoding="utf-8",
    )
    assert "greatminds stand ready" in text
    assert "greatminds stand down" in text
    assert "greatminds stand up" in text
    assert "deprecated" in text.lower() or "post-0245" in text.lower()


def test_tester_doc_references_lease_workflow() -> None:
    """TESTER.md cites the new lease/release path + holder
    information asymmetry."""
    text = (find_canon_dir() / "roles" / "TESTER.md").read_text(
        encoding="utf-8",
    )
    assert "greatminds stand lease" in text
    assert "greatminds stand release" in text
    # Information-asymmetry mention (no prose channel).
    assert "information asymmetry" in text.lower() or \
           "rubber-stamp" in text.lower()


def test_explorer_doc_references_lease_workflow() -> None:
    """EXPLORER.md walks scenarios using lease/release for
    review-session stand state."""
    text = (find_canon_dir() / "roles" / "EXPLORER.md").read_text(
        encoding="utf-8",
    )
    assert "greatminds stand lease" in text
    assert "greatminds stand release" in text


def test_no_prose_channel_in_lease_request_documented() -> None:
    """Cross-doc: TESTER doc explicitly warns against putting
    acceptance / probe steps in lease requests (PLANNER §7
    amendment)."""
    text = (find_canon_dir() / "roles" / "TESTER.md").read_text(
        encoding="utf-8",
    )
    assert "structured only" in text.lower() or "no prose" in text.lower()
