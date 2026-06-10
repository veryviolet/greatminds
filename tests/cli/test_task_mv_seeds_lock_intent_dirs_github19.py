"""Regression tests for GitHub issue #19 / task 0370:
``greatminds task mv`` must never crash with FileNotFoundError when the
``coordination/.locks/`` or ``coordination/intent/`` directories do not
exist yet (a fresh queue / upgraded-from-<1.6 project that has never had
a task moved before).

Reported failure (nginarea, 1.6.6): a ``feature_dev → feature_blocked``
move raised FileNotFoundError because the lock/intent write paths
assumed their parent dir already existed; the operator had to ``mkdir``
the paths by hand to recover.

At HEAD the layout is FLAT (``.locks/<id>.lock``, ``intent/<id>-...``)
and both write helpers already ``mkdir(parents=True, exist_ok=True)``
before opening the file, so the FileNotFoundError class is eliminated.
These tests PIN that resilience so a future refactor (e.g. reintroducing
per-queue subdirs, or "simplifying" the mkdir away) cannot silently
bring the crash back. They also pin the defense-in-depth requirement
that ``greatminds setup`` seeds every queue declared in the schema.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from greatminds.cli import task as task_mod
from greatminds.cli.setup import QUEUES
from greatminds.cli.task import intent_write, task_file_lock
from greatminds.core.paths import find_canon_dir


# ---------------------------------------------------------------------------
# Patch (1): the write helpers create their own parent dir on demand.
# ---------------------------------------------------------------------------


def test_intent_write_creates_missing_intent_dir(tmp_path: Path) -> None:
    """intent_write must create coordination/intent/ if it is absent
    (never assume a prior move seeded it)."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    assert not (coord / "intent").exists()

    p = intent_write(coord, "DEVELOPER", "0001-x",
                     "feature_dev", "feature_blocked", "parking")

    assert (coord / "intent").is_dir()
    assert p.is_file()


def test_task_file_lock_creates_missing_locks_dir(tmp_path: Path) -> None:
    """task_file_lock must create coordination/.locks/ if it is absent."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    assert not (coord / ".locks").exists()

    with task_file_lock(coord, "0001-x"):
        assert (coord / ".locks").is_dir()
        assert (coord / ".locks" / "0001-x.lock").is_file()


# ---------------------------------------------------------------------------
# End-to-end: the exact move from the bug report (feature_dev →
# feature_blocked) succeeds with BOTH .locks/ and intent/ absent.
# ---------------------------------------------------------------------------


def _make_blocked_ready_dev_task(coord: Path) -> Path:
    """A feature_dev task carrying triage + plan + a blocked block, ready
    to be parked into feature_blocked by its current owner (DEVELOPER)."""
    p = coord / "feature_dev" / "0001-task-a.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "id: 0001-task-a\n"
        "stream: product\n"
        "scope: backend\n"
        "kind: feature\n"
        "reporter: USER\n"
        "opened_at: '2026-06-05T00:00:00Z'\n"
        "priority: normal\n"
        "title: A\n"
        "blocks:\n"
        "- kind: triage\n  by: ARCHITECT-PLANNER\n"
        "  at: '2026-06-05T00:00:00Z'\n  notes: ok\n"
        "- kind: plan\n  by: ARCHITECT-PLANNER\n"
        "  at: '2026-06-05T00:00:00Z'\n"
        "  base_commit: deadbeef\n  assignee_role: DEVELOPER\n"
        "  stand_required: false\n  stand_reason: ''\n"
        "  plan_kind: bugfix\n  mode: A\n"
        "  ready_for_implementation: true\n"
        "- kind: blocked\n  blocked_by: DEVELOPER\n"
        "  blocked_at: '2026-06-05T00:00:00Z'\n"
        "  reason: waiting on upstream\n"
        "  dependencies:\n  - feature_dev/0002-dep.md\n"
        "  resume_to: feature_dev\n",
        encoding="utf-8",
    )
    return p


def test_mv_does_not_crash_when_lock_and_intent_dirs_absent(
    tmp_path: Path, monkeypatch,
) -> None:
    """The feature_dev → feature_blocked move from the #19 report must
    succeed even though neither .locks/ nor intent/ exists yet."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    _make_blocked_ready_dev_task(coord)
    # Simulate a previously-untouched project: no lock/intent dirs.
    assert not (coord / ".locks").exists()
    assert not (coord / "intent").exists()

    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    monkeypatch.setattr(task_mod, "caller_role", lambda: "DEVELOPER")

    from_q = task_mod.move_task(
        task_id="0001-task-a", to_queue="feature_blocked",
        reason="park",
    )

    assert from_q == "feature_dev"
    assert (coord / "feature_blocked" / "0001-task-a.yaml").is_file()
    assert not (coord / "feature_dev" / "0001-task-a.yaml").exists()
    # The helpers seeded their dirs as a side effect of the move.
    assert (coord / ".locks").is_dir()
    assert (coord / "intent").is_dir()


# ---------------------------------------------------------------------------
# Patch (2) / defense-in-depth: setup seeds EVERY schema queue, so a
# fresh / migrated project has all queue dirs present from the start.
# ---------------------------------------------------------------------------


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def test_setup_seeds_every_schema_queue() -> None:
    """The QUEUES list setup.py creates must cover every product queue
    declared in schema.queues — a new queue added to the schema without
    being added here would not be seeded, reviving the #19 class of
    'first move into an unseeded queue' failures."""
    schema_queues = {
        name for name in (_schema().get("queues") or {})
        # .stand is a state watch-path, not a claim queue / on-disk
        # task dir that setup seeds with a .gitkeep.
        if not name.startswith(".")
    }
    missing = schema_queues - set(QUEUES)
    assert not missing, (
        f"setup.QUEUES does not seed schema queues {sorted(missing)}; "
        "add them so a fresh/migrated project has every queue dir "
        "(GitHub #19 defense-in-depth)."
    )
