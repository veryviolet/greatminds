"""Tests for GitHub issue #20 (task 0371): notify_from_journal must not
re-emit wakes for a *historical* transition forever.

Pre-fix, coordd's notify_from_journal re-wrote a fresh
``wake-<wallclock>-<task>-<queue>.md`` for a historical journal
transition every tick (the filename keyed on wall-clock now, and there
was no check that the task still lived in the destination queue). A task
that had moved on to feature_blocked / archive — or an empty destination
queue — kept waking the destination owner indefinitely, burning driven
no-op turns and firing false ``hung-<role>`` escalations.

Fix (two guards):
  (a) current-location check — wake the destination owner only if the
      task currently RESIDES in the destination queue;
  (b) per-transition dedupe — the wake filename keys on the transition
      timestamp (journal ``t``), so replaying the same transition yields
      the same filename and ``path.exists()`` suppresses the duplicate.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli import notify_from_journal as nfj_mod


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    coord = project / "coordination"
    coord.mkdir(parents=True)
    canon = tmp_path / "canon"
    canon.mkdir()
    schema_yaml = {
        "queues": {
            "feature_inbox":   {"owner": "ARCHITECT-PLANNER", "writers": []},
            "feature_plan":    {"owner": "ARCHITECT-PLANNER", "writers": []},
            "feature_dev":     {"owner": "DEVELOPER", "writers": []},
            "feature_blocked": {"owner": "ARCHITECT-REVIEWER", "writers": []},
            "feature_test":    {"owner": "TESTER", "writers": []},
        },
        "roles": {
            "ARCHITECT-PLANNER":  {"claims_from": ["feature_inbox", "feature_plan"]},
            "DEVELOPER":          {"claims_from": ["feature_dev"]},
            "TESTER":             {"claims_from": ["feature_test"]},
            "ARCHITECT-REVIEWER": {"claims_from": ["feature_review"]},
        },
    }
    (canon / "schema.yaml").write_text(yaml.safe_dump(schema_yaml), encoding="utf-8")
    return project, canon


def _write_journal(coord: Path, lines: list[dict]) -> None:
    (coord / "journal.ndjson").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8",
    )


def _place_task(coord: Path, queue: str, task_id: str) -> None:
    qdir = coord / queue
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / f"{task_id}.yaml").write_text(f"id: {task_id}\n", encoding="utf-8")


def _move_task(coord: Path, src: str, dst: str, task_id: str) -> None:
    (coord / src / f"{task_id}.yaml").unlink()
    _place_task(coord, dst, task_id)


def _run(project: Path, canon: Path, *, once: bool = False):
    args = ["--project-dir", str(project), "--canon-dir", str(canon)]
    if once:
        args.append("--once")
    return CliRunner().invoke(nfj_mod.notify_journal, args, catch_exceptions=False)


def _wakes(coord: Path, role: str) -> list[str]:
    inbox = coord / "inbox" / role.lower()
    if not inbox.is_dir():
        return []
    return sorted(f.name for f in inbox.iterdir() if f.name.startswith("wake-"))


# ---- current-location check -------------------------------------------------


def test_task_moved_onward_produces_no_wake(tmp_path: Path) -> None:
    """The reproducer: transition feature_plan->feature_dev happened, but
    the task has since moved to feature_blocked. Replaying must NOT wake
    DEVELOPER — feature_dev no longer holds the task."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_blocked", "0004-some-task")  # already moved on
    _write_journal(coord, [{
        "t": "2026-06-05T10:25:00Z", "actor": "ARCHITECT-PLANNER",
        "task": "0004-some-task", "from": "feature_plan", "to": "feature_dev",
        "reason": "route scope:backend", "intent_id": "a",
    }])
    _run(project, canon, once=True)
    assert _wakes(coord, "DEVELOPER") == []


def test_empty_destination_queue_produces_no_wake(tmp_path: Path) -> None:
    """feature_dev is empty (task archived). No DEVELOPER wake."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    (coord / "feature_dev").mkdir()  # empty
    _write_journal(coord, [{
        "t": "2026-06-05T10:28:00Z", "actor": "ARCHITECT-PLANNER",
        "task": "0005-other", "from": "feature_plan", "to": "feature_dev",
        "reason": "route", "intent_id": "b",
    }])
    _run(project, canon, once=True)
    assert _wakes(coord, "DEVELOPER") == []


def test_legitimate_new_transition_still_wakes_once(tmp_path: Path) -> None:
    """A task that DOES reside in the destination queue wakes its owner
    exactly once."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_dev", "0006-live")
    _write_journal(coord, [{
        "t": "2026-06-05T11:00:00Z", "actor": "ARCHITECT-PLANNER",
        "task": "0006-live", "from": "feature_plan", "to": "feature_dev",
        "reason": "route", "intent_id": "c",
    }])
    _run(project, canon, once=True)
    assert len(_wakes(coord, "DEVELOPER")) == 1


def test_bare_id_in_journal_matches_full_id_file(tmp_path: Path) -> None:
    """Real coordd journals store the bare id (``0006``); the queue file
    is ``0006-slug.yaml``. The residence check must still match."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_dev", "0006-live")
    _write_journal(coord, [{
        "t": "2026-06-05T11:00:00Z", "actor": "ARCHITECT-PLANNER",
        "task": "0006", "from": "feature_plan", "to": "feature_dev",
        "reason": "route", "intent_id": "c2",
    }])
    _run(project, canon, once=True)
    assert len(_wakes(coord, "DEVELOPER")) == 1


# ---- per-transition dedupe / monotonic cursor -------------------------------


def test_repeated_ticks_emit_at_most_one_wake(tmp_path: Path) -> None:
    """Many coordd ticks over the same journal (task still resident) must
    not accumulate duplicate wakes — the offset cursor skips already-read
    lines, and the transition-keyed filename dedupes even on replay."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_dev", "0007-resident")
    _write_journal(coord, [{
        "t": "2026-06-05T12:00:00Z", "actor": "ARCHITECT-PLANNER",
        "task": "0007-resident", "from": "feature_plan", "to": "feature_dev",
        "reason": "route", "intent_id": "d",
    }])
    for _ in range(5):
        _run(project, canon)  # default: offset-tracked incremental
    assert len(_wakes(coord, "DEVELOPER")) == 1


def test_once_backfill_does_not_duplicate_same_transition(tmp_path: Path) -> None:
    """Repeated ``--once`` backfills of the same transition (no offset
    advance) must still produce a single wake — the filename keys on the
    transition timestamp, not wall-clock."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_dev", "0008-x")
    _write_journal(coord, [{
        "t": "2026-06-05T13:00:00Z", "actor": "ARCHITECT-PLANNER",
        "task": "0008-x", "from": "feature_plan", "to": "feature_dev",
        "reason": "route", "intent_id": "e",
    }])
    _run(project, canon, once=True)
    _run(project, canon, once=True)
    assert len(_wakes(coord, "DEVELOPER")) == 1


def test_backfill_mixed_history_only_wakes_resident(tmp_path: Path) -> None:
    """A backfill over a long journal: only the task still resident in
    feature_dev produces a wake; historical transitions whose tasks moved
    on are suppressed."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_blocked", "0004-moved")   # moved on
    # 0005 archived entirely (no file anywhere)
    _place_task(coord, "feature_dev", "0009-here")        # still resident
    _write_journal(coord, [
        {"t": "2026-06-05T10:25:00Z", "actor": "ARCHITECT-PLANNER",
         "task": "0004-moved", "from": "feature_plan", "to": "feature_dev",
         "reason": "r", "intent_id": "f1"},
        {"t": "2026-06-05T10:28:00Z", "actor": "ARCHITECT-PLANNER",
         "task": "0005-gone", "from": "feature_plan", "to": "feature_dev",
         "reason": "r", "intent_id": "f2"},
        {"t": "2026-06-05T10:40:00Z", "actor": "ARCHITECT-PLANNER",
         "task": "0009-here", "from": "feature_plan", "to": "feature_dev",
         "reason": "r", "intent_id": "f3"},
    ])
    _run(project, canon, once=True)
    wakes = _wakes(coord, "DEVELOPER")
    assert len(wakes) == 1
    assert "0009-here" in wakes[0]
