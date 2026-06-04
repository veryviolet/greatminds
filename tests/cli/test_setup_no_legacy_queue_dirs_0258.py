"""Tests for task 0258: ``greatminds setup`` must not (re-)create
the legacy ``stand_requests`` / ``stand_wip`` / ``stand_done``
directories on a fresh project.

0247 (1.3.0 BREAKING) removed the queue model in favour of the
lease-based singleton stand. The setup.QUEUES tuple already dropped
those three names; 0258 pins the regression-net so a future addition
fails this test.
"""
from __future__ import annotations

from pathlib import Path

import yaml

import greatminds
from greatminds.cli import setup as setup_mod


LEGACY_STAND_DIRS = ("stand_requests", "stand_wip", "stand_done")


def test_setup_queues_match_schema_task_queues() -> None:
    """setup.QUEUES must EXACTLY equal the task-holding queues declared in
    schema.queues (kind active/parking/terminal). The state-kind ``.stand``
    is excluded (it is a watch path, not a created queue dir).

    This pins the created-queue list against schema drift in both
    directions. ``feature_live`` was added to the schema with
    LIVE-DEVELOPER in 1.5.0 but never added to QUEUES, so every fresh
    ``greatminds setup`` from 1.5.0–1.5.4 silently lacked the queue
    (caught only when re-bootstrapping a real fleet). Fixed in 1.5.5."""
    schema = yaml.safe_load(
        (Path(greatminds.__file__).parent / "data" / "schema.yaml")
        .read_text(encoding="utf-8")
    )
    task_queues = {
        name for name, meta in (schema.get("queues") or {}).items()
        if isinstance(meta, dict)
        and meta.get("kind") in ("active", "parking", "terminal")
    }
    assert set(setup_mod.QUEUES) == task_queues, (
        "setup.QUEUES drifted from schema task queues — "
        f"missing from QUEUES: {sorted(task_queues - set(setup_mod.QUEUES))}; "
        f"extra in QUEUES: {sorted(set(setup_mod.QUEUES) - task_queues)}"
    )


def test_setup_queues_excludes_legacy_stand_dirs() -> None:
    """0258: the module-level QUEUES tuple must not enumerate any of
    the removed stand_* queues. Adding one back would let
    ``greatminds setup`` recreate them in fresh fleets — undoing the
    0247 BREAKING."""
    for q in LEGACY_STAND_DIRS:
        assert q not in setup_mod.QUEUES, (
            f"0258: setup.QUEUES must not include {q!r} "
            f"(0247 removed the stand queue model)"
        )


def test_setup_queues_keeps_lease_neighbours() -> None:
    """Sanity: the queues that LIVE next to the removed stand_* dirs
    (review_sessions, user_feedback, the feature pipeline) are still
    present. Otherwise fresh setups would be broken in a worse way."""
    for q in ("feature_inbox", "feature_plan", "feature_dev",
              "feature_test", "feature_review", "verified", "archive",
              "review_sessions", "user_feedback"):
        assert q in setup_mod.QUEUES, (
            f"0258: setup.QUEUES missing {q!r} — regression "
            "(0258 only removes stand_* dirs)"
        )
