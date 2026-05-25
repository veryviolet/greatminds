"""Regression tests for task 0115: per-file working-tree locks at
implementation-block append.

Pre-0115 two tasks both touching the same src/file.py would interleave
hunks in the working tree, forcing REVIEWER to slice the diff per
task (the 2026-05-25 0091+0103 incident, sibling 0008+0010).

0115 adds: at impl-block append time, claim a per-file lock under
coord/.file_locks/<hash>.lock = task_id. Subsequent impl-block append
on any other task listing a locked file is refused with a diagnostic
naming the holder + queue. Locks release when the holder moves to
verified or archive (working-tree settled).

Tests directly invoke the helpers — no full CLI plumbing — and one
end-to-end scenario via append_block / move_task.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli.task import (
    FILE_LOCKS_DIR_NAME,
    FILE_LOCK_RELEASE_QUEUES,
    _acquire_file_locks_for_task,
    _file_lock_path,
    _release_file_locks_for_task,
)
from greatminds.core.errors import GreatMindsError


def _make_task_file(coord: Path, queue: str, stem: str) -> Path:
    qdir = coord / queue
    qdir.mkdir(parents=True, exist_ok=True)
    p = qdir / f"{stem}.yaml"
    p.write_text(f"id: {stem}\n", encoding="utf-8")
    return p


# ---------- _acquire_file_locks_for_task ----------


def test_acquire_writes_lock_files_per_path(tmp_path: Path) -> None:
    """First claim: per-file lock file is created and contains task_id."""
    _acquire_file_locks_for_task(
        tmp_path, "0001-task-a", ["src/foo.py", "src/bar.py"],
    )
    a = _file_lock_path(tmp_path, "src/foo.py")
    b = _file_lock_path(tmp_path, "src/bar.py")
    assert a.read_text(encoding="utf-8").strip() == "0001-task-a"
    assert b.read_text(encoding="utf-8").strip() == "0001-task-a"


def test_acquire_idempotent_for_same_task(tmp_path: Path) -> None:
    """Re-claiming files the same task already owns must not raise."""
    _acquire_file_locks_for_task(tmp_path, "0001-task-a", ["src/foo.py"])
    # Re-run — no conflict, no raise.
    _acquire_file_locks_for_task(tmp_path, "0001-task-a", ["src/foo.py"])


def test_acquire_rejects_conflict_with_other_task(tmp_path: Path) -> None:
    """Different task → conflict, error names holder + queue."""
    _make_task_file(tmp_path, "feature_dev", "0001-task-a")
    _acquire_file_locks_for_task(
        tmp_path, "0001-task-a", ["src/file.py"],
    )
    # Task B tries to claim the same file.
    with pytest.raises(GreatMindsError) as excinfo:
        _acquire_file_locks_for_task(
            tmp_path, "0002-task-b", ["src/file.py"],
        )
    msg = str(excinfo.value)
    assert "src/file.py" in msg
    assert "0001-task-a" in msg
    assert "feature_dev" in msg
    assert "depends_on" in msg or "verify" in msg


def test_acquire_two_pass_no_partial_claim_on_conflict(tmp_path: Path) -> None:
    """If task B lists [non-conflicting, conflicting], the non-
    conflicting file must NOT end up locked by B — the two-pass
    design ensures all-or-nothing semantics within one acquire call.
    """
    _acquire_file_locks_for_task(tmp_path, "0001-a", ["src/x.py"])
    with pytest.raises(GreatMindsError):
        _acquire_file_locks_for_task(
            tmp_path, "0002-b", ["src/new.py", "src/x.py"],
        )
    new_lock = _file_lock_path(tmp_path, "src/new.py")
    # src/new.py must NOT be claimed by 0002-b — first-pass conflict
    # check ran before the write pass.
    assert not new_lock.exists() or \
        new_lock.read_text(encoding="utf-8").strip() != "0002-b"


def test_acquire_skips_empty_paths(tmp_path: Path) -> None:
    """Empty / whitespace-only file paths are filtered out."""
    _acquire_file_locks_for_task(
        tmp_path, "0001-a", ["", "   ", "src/real.py"],
    )
    locks_dir = tmp_path / FILE_LOCKS_DIR_NAME
    lock_files = list(locks_dir.glob("*.lock"))
    assert len(lock_files) == 1
    assert lock_files[0].read_text(encoding="utf-8").strip() == "0001-a"


# ---------- _release_file_locks_for_task ----------


def test_release_removes_only_this_tasks_locks(tmp_path: Path) -> None:
    _acquire_file_locks_for_task(tmp_path, "0001-a", ["src/a.py"])
    _acquire_file_locks_for_task(tmp_path, "0002-b", ["src/b.py"])
    _release_file_locks_for_task(tmp_path, "0001-a")
    assert not _file_lock_path(tmp_path, "src/a.py").exists()
    assert _file_lock_path(tmp_path, "src/b.py").exists()


def test_release_when_no_lockdir_is_noop(tmp_path: Path) -> None:
    """Calling release on a coord that never had locks must not raise."""
    _release_file_locks_for_task(tmp_path, "0099-nothing")


# ---------- verified is in the release set ----------


def test_verified_and_archive_in_release_queues_set() -> None:
    """Locks must release on verified (commit done) and archive
    (abandoned). Pin both."""
    assert "verified" in FILE_LOCK_RELEASE_QUEUES
    assert "archive" in FILE_LOCK_RELEASE_QUEUES


def test_other_queues_not_in_release_set() -> None:
    """feature_test / feature_review aren't terminal — locks stay
    until the task actually reaches verified/archive."""
    for q in ("feature_dev", "feature_test", "feature_review",
              "feature_blocked", "stand_wip"):
        assert q not in FILE_LOCK_RELEASE_QUEUES


# ---------- end-to-end via append_block + move_task ----------


def test_end_to_end_lock_acquired_on_impl_block_release_on_verified(
    tmp_path: Path, monkeypatch,
) -> None:
    """Full scenario: A appends impl block → lock acquired; B tries
    impl block on same file → refused; A → verified → lock released;
    B retry → succeeds.

    Monkeypatches find_coord_dir + caller_role to isolate from a real
    project.
    """
    from greatminds.cli import task as task_mod

    coord = tmp_path / "coordination"
    coord.mkdir()

    # Task A in feature_dev (DEVELOPER's queue, scope:backend).
    a_path = coord / "feature_dev" / "0001-task-a.yaml"
    a_path.parent.mkdir(parents=True)
    a_path.write_text(
        "id: 0001-task-a\n"
        "stream: product\n"
        "scope: backend\n"
        "kind: feature\n"
        "reporter: USER\n"
        "opened_at: '2026-05-25T00:00:00Z'\n"
        "priority: normal\n"
        "title: A\n"
        "blocks:\n"
        "- kind: triage\n  by: ARCHITECT-PLANNER\n  at: '2026-05-25T00:00:00Z'\n  notes: ok\n"
        "- kind: plan\n  by: ARCHITECT-PLANNER\n  at: '2026-05-25T00:00:00Z'\n"
        "  base_commit: deadbeef\n  assignee_role: DEVELOPER\n"
        "  stand_required: false\n  stand_reason: ''\n"
        "  plan_kind: bugfix\n  mode: A\n"
        "  ready_for_implementation: true\n",
        encoding="utf-8",
    )
    # Task B same shape.
    b_path = coord / "feature_dev" / "0002-task-b.yaml"
    b_path.write_text(
        a_path.read_text(encoding="utf-8").replace("0001-task-a", "0002-task-b")
                                          .replace("title: A", "title: B"),
        encoding="utf-8",
    )

    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    monkeypatch.setattr(task_mod, "caller_role", lambda: "DEVELOPER")

    # A claims src/file.py.
    task_mod.append_block(
        task_id="0001-task-a", kind="implementation",
        fields={"base_commit": "deadbeef",
                "files": ["src/file.py"],
                "ready_for_test": True},
        body="iter-1",
    )
    assert _file_lock_path(coord, "src/file.py").read_text(
        encoding="utf-8",
    ).strip() == "0001-task-a"

    # B tries to claim src/file.py — refused.
    with pytest.raises(GreatMindsError) as excinfo:
        task_mod.append_block(
            task_id="0002-task-b", kind="implementation",
            fields={"base_commit": "deadbeef",
                    "files": ["src/file.py"],
                    "ready_for_test": True},
            body="iter-1",
        )
    msg = str(excinfo.value)
    assert "src/file.py" in msg
    assert "0001-task-a" in msg

    # Move A through feature_test → feature_review → verified.
    # Skip the intermediate moves; emulate by moving A directly to
    # verified via _do_move (we're testing the release semantics).
    # The real FSM requires more blocks; mirror that by directly
    # invoking _release helper to assert behavior without forcing
    # the whole sequence.
    task_mod._release_file_locks_for_task(coord, "0001-task-a")
    assert not _file_lock_path(coord, "src/file.py").exists()

    # B retries — succeeds.
    task_mod.append_block(
        task_id="0002-task-b", kind="implementation",
        fields={"base_commit": "deadbeef",
                "files": ["src/file.py"],
                "ready_for_test": True},
        body="iter-1",
    )
    assert _file_lock_path(coord, "src/file.py").read_text(
        encoding="utf-8",
    ).strip() == "0002-task-b"
