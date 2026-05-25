"""Regression tests for task 0102: archive transitions now gated by
real preconditions (review_session_terminal_block,
feature_blocked_withdrawn_reason, stand_done_no_active_dependents,
stand_request_not_yet_claimed).

Pre-0102 these four transitions had ``requires: []`` — one role could
silently archive in-progress work, mid-claim requests, or stand_done
that other tasks still depend on.

Tests exercise each validator directly with a synthetic task dict (no
full CLI plumbing) AND verify the schema row now references the
expected key.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ---------- helpers ----------


def _schema():
    from greatminds.core.paths import find_canon_dir
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def _row_requires(from_q: str, to_q: str, by: str) -> list[str] | None:
    for t in _schema().get("transitions") or []:
        if t.get("from") == from_q and t.get("to") == to_q and t.get("by") == by:
            return list(t.get("requires") or [])
    return None


# ---------- 0102 (1)+(2): review_sessions → archive ----------


def test_review_sessions_archive_rows_now_require_terminal_block() -> None:
    """Both archive rows (by PLANNER and by EXPLORER) reference the
    new requires key."""
    for by in ("ARCHITECT-PLANNER", "EXPLORER"):
        reqs = _row_requires("review_sessions", "archive", by)
        assert reqs is not None, f"missing row review_sessions→archive by {by}"
        assert "review_session_terminal_block" in reqs, (
            f"row review_sessions→archive by {by} missing "
            f"review_session_terminal_block; got {reqs}"
        )


def test_review_session_terminal_validator_rejects_empty_session() -> None:
    from greatminds.cli.task import _check_review_session_terminal

    data = {"id": "0001-rs", "blocks": []}
    err = _check_review_session_terminal(data, "review_sessions", "archive")
    assert err is not None
    assert "session_iteration" in err
    assert "withdrawn" in err


def test_review_session_terminal_validator_accepts_session_iteration() -> None:
    from greatminds.cli.task import _check_review_session_terminal

    data = {"id": "0001-rs", "blocks": [
        {"kind": "session_iteration", "by": "EXPLORER",
         "at": "2026-05-25T00:00:00Z", "notes": "iter 1"},
    ]}
    assert _check_review_session_terminal(
        data, "review_sessions", "archive",
    ) is None


def test_review_session_terminal_validator_accepts_withdrawn_blocked() -> None:
    from greatminds.cli.task import _check_review_session_terminal

    data = {"id": "0001-rs", "blocks": [
        {"kind": "blocked", "by": "ARCHITECT-PLANNER",
         "at": "2026-05-25T00:00:00Z",
         "reason": "withdrawn: USER cancelled session"},
    ]}
    assert _check_review_session_terminal(
        data, "review_sessions", "archive",
    ) is None


def test_review_session_terminal_validator_rejects_non_withdrawn_blocked() -> None:
    """A blocked block with a non-withdrawn reason ('waiting on…') is
    NOT terminal — must still refuse archive."""
    from greatminds.cli.task import _check_review_session_terminal

    data = {"id": "0001-rs", "blocks": [
        {"kind": "blocked", "by": "ARCHITECT-PLANNER",
         "at": "2026-05-25T00:00:00Z",
         "reason": "waiting on STAND-KEEPER"},
    ]}
    err = _check_review_session_terminal(data, "review_sessions", "archive")
    assert err is not None


# ---------- 0102 (3): feature_blocked → archive ----------


def test_feature_blocked_archive_row_now_requires_withdrawn_reason() -> None:
    reqs = _row_requires("feature_blocked", "archive", "ARCHITECT-REVIEWER")
    assert reqs is not None
    assert "feature_blocked_withdrawn_reason" in reqs


def test_feature_blocked_withdrawn_validator_accepts_withdrawn() -> None:
    from greatminds.cli.task import _check_feature_blocked_withdrawn

    data = {"id": "0001-fb", "blocks": [
        {"kind": "blocked", "by": "ARCHITECT-PLANNER",
         "at": "2026-05-25T00:00:00Z",
         "reason": "withdrawn: duplicate of 0042"},
    ]}
    assert _check_feature_blocked_withdrawn(
        data, "feature_blocked", "archive",
    ) is None


def test_feature_blocked_withdrawn_validator_rejects_waiting_reason() -> None:
    """A normal feature_blocked task (waiting on a dep) must NOT be
    archivable by REVIEWER alone."""
    from greatminds.cli.task import _check_feature_blocked_withdrawn

    data = {"id": "0001-fb", "blocks": [
        {"kind": "blocked", "by": "DEVELOPER",
         "at": "2026-05-25T00:00:00Z",
         "reason": "waiting on verified/0103",
         "dependencies": ["verified/0103-x.yaml"],
         "resume_to": "feature_dev"},
    ]}
    err = _check_feature_blocked_withdrawn(
        data, "feature_blocked", "archive",
    )
    assert err is not None
    assert "withdrawn" in err.lower()


def test_feature_blocked_withdrawn_validator_rejects_no_blocked_block() -> None:
    from greatminds.cli.task import _check_feature_blocked_withdrawn

    data = {"id": "0001-fb", "blocks": []}
    err = _check_feature_blocked_withdrawn(
        data, "feature_blocked", "archive",
    )
    assert err is not None


# ---------- 0102 (4): stand_done → archive ----------


def test_stand_done_archive_row_now_requires_no_dependents() -> None:
    reqs = _row_requires("stand_done", "archive", "MAINTAINER")
    assert reqs is not None
    assert "stand_done_no_active_dependents" in reqs


def test_stand_done_validator_passes_when_no_blocked_tasks_depend(
    tmp_path: Path, monkeypatch,
) -> None:
    """No feature_blocked/ dir at all → returns None (vacuous pass)."""
    from greatminds.cli import task as task_mod

    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: tmp_path)

    data = {"id": "0050-stand-done-evidence"}
    assert task_mod._check_stand_done_no_active_dependents(
        data, "stand_done", "archive",
    ) is None


def test_stand_done_validator_rejects_when_feature_blocked_depends(
    tmp_path: Path, monkeypatch,
) -> None:
    from greatminds.cli import task as task_mod

    blocked_dir = tmp_path / "feature_blocked"
    blocked_dir.mkdir()
    (blocked_dir / "0091-foo.yaml").write_text(
        "id: 0091-foo\nblocks:\n"
        "- kind: blocked\n  by: DEVELOPER\n  at: '2026-05-25T00:00:00Z'\n"
        "  dependencies:\n"
        "  - stand_done/0050-stand-done-evidence.yaml\n"
        "  resume_to: feature_dev\n  reason: parked\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: tmp_path)

    data = {"id": "0050-stand-done-evidence"}
    err = task_mod._check_stand_done_no_active_dependents(
        data, "stand_done", "archive",
    )
    assert err is not None
    assert "0091-foo" in err
    assert "wake-check" in err


# ---------- 0102 (5): stand_requests → archive ----------


def test_stand_requests_archive_row_now_requires_not_yet_claimed() -> None:
    reqs = _row_requires("stand_requests", "archive", "ARCHITECT-PLANNER")
    assert reqs is not None
    assert "stand_request_not_yet_claimed" in reqs


def test_stand_request_validator_passes_when_no_stand_wip(
    tmp_path: Path, monkeypatch,
) -> None:
    from greatminds.cli import task as task_mod

    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: tmp_path)
    data = {"id": "0080-some-stand-request"}
    assert task_mod._check_stand_request_not_yet_claimed(
        data, "stand_requests", "archive",
    ) is None


def test_stand_request_validator_rejects_when_stand_wip_exists(
    tmp_path: Path, monkeypatch,
) -> None:
    from greatminds.cli import task as task_mod

    (tmp_path / "stand_wip").mkdir()
    (tmp_path / "stand_wip" / "0080-some-stand-request.yaml").write_text(
        "id: 0080-some-stand-request\n", encoding="utf-8",
    )
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: tmp_path)
    data = {"id": "0080-some-stand-request"}
    err = task_mod._check_stand_request_not_yet_claimed(
        data, "stand_requests", "archive",
    )
    assert err is not None
    assert "0080-some-stand-request" in err
    assert "STAND-KEEPER" in err


# ---------- All four validators registered ----------


def test_all_0102_validators_in_registry() -> None:
    """Each new requires key must be in SCHEMA_REQUIRES_VALIDATORS so
    enforce_schema_requires recognizes it (not an unknown-key error)."""
    from greatminds.cli.task import SCHEMA_REQUIRES_VALIDATORS
    for name in (
        "review_session_terminal_block",
        "feature_blocked_withdrawn_reason",
        "stand_done_no_active_dependents",
        "stand_request_not_yet_claimed",
    ):
        assert name in SCHEMA_REQUIRES_VALIDATORS, (
            f"0102 requires key {name!r} missing from registry"
        )
