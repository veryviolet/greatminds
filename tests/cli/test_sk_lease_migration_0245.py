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


# Role-doc lease-API prose pins removed: the per-role prose docs are
# gone (system prompt is the static bootstrap.md). The lease/release
# workflow + information-asymmetry are pinned in schema (event_triggers
# / forbidden_actions, test_schema_role_contracts_0288) and the stand
# resource model (schema.stand).
