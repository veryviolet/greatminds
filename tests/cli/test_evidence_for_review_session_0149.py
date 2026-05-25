"""Tests for task 0149: ``greatminds task new --evidence-for`` must accept
review-session ids.

Pre-fix ``task_exists_in_active`` only scanned the product-pipeline
queues. EXPLORER's review-session tasks live in ``review_sessions/``,
so ``stand request --evidence-for 0007 ...`` (where 0007 is the
EXPLORER session) was rejected with ``evidence-for ids not in any
active queue``. Fix adds ``review_sessions`` to the active-queue
allow-list. Bare-seq and full-id resolution were already supported by
the existing prefix-match in ``task_exists_in_active``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli.stand import (
    EVIDENCE_FOR_ACTIVE_QUEUES,
    task_exists_in_active,
)


def _make_task_file(coord: Path, queue: str, stem: str) -> Path:
    qd = coord / queue
    qd.mkdir(parents=True, exist_ok=True)
    p = qd / f"{stem}.yaml"
    p.write_text(f"id: {stem}\n", encoding="utf-8")
    return p


# ---------- review_sessions in the allow-list ----------


def test_review_sessions_in_active_queues() -> None:
    """The 0149 fix point. Pre-fix the tuple had only product-pipeline
    queues; review_sessions joined to match the actual schema (review
    sessions are an active stream)."""
    assert "review_sessions" in EVIDENCE_FOR_ACTIVE_QUEUES


def test_product_queues_still_in_active_queues() -> None:
    """Negative pin: the iter-1 product-pipeline membership must not
    regress when adding review_sessions."""
    for q in (
        "feature_inbox",
        "feature_plan",
        "feature_dev",
        "feature_ui_dev",
        "feature_docs",
        "feature_test",
        "feature_docs_review",
        "feature_review",
        "feature_blocked",
    ):
        assert q in EVIDENCE_FOR_ACTIVE_QUEUES


# ---------- task_exists_in_active over review_sessions ----------


def test_review_session_full_id_resolves(tmp_path: Path) -> None:
    """0149 contract: a full review-session id (``0007-explorer-...``)
    resolves through the existing prefix-match."""
    _make_task_file(tmp_path, "review_sessions",
                    "0007-explorer-canon-9-pretrust-walkthrough")
    assert task_exists_in_active(
        tmp_path, "0007-explorer-canon-9-pretrust-walkthrough",
    )


def test_review_session_bare_seq_prefix_resolves(tmp_path: Path) -> None:
    """Bare seq (``0007``) matches by prefix — same path users use for
    product tasks. The plan body specifically calls this out: both
    bare-seq and full-id should resolve."""
    _make_task_file(tmp_path, "review_sessions",
                    "0007-explorer-canon-9-pretrust-walkthrough")
    assert task_exists_in_active(tmp_path, "0007")


def test_review_session_id_in_archive_does_NOT_resolve(tmp_path: Path) -> None:
    """archive is terminal, not active. A review_session id that has
    been archived must still reject as 'not in any active queue' —
    you can't use stale evidence."""
    _make_task_file(tmp_path, "archive",
                    "0007-explorer-canon-9-pretrust-walkthrough")
    assert not task_exists_in_active(tmp_path, "0007")


def test_review_session_id_unknown_rejects(tmp_path: Path) -> None:
    """No matching file in any active queue → False (validator will
    then refuse the stand-request)."""
    (tmp_path / "review_sessions").mkdir(parents=True)
    assert not task_exists_in_active(tmp_path, "9999")


# ---------- co-existence with product queues ----------


def test_resolves_product_task_alongside_review_session(tmp_path: Path) -> None:
    """A stand can reference BOTH a product task and a review_session
    in its --evidence-for list. The validator iterates each id
    independently; nothing in 0149 changes the per-id semantics."""
    _make_task_file(tmp_path, "feature_dev", "0100-some-feature")
    _make_task_file(tmp_path, "review_sessions",
                    "0007-explorer-session")
    assert task_exists_in_active(tmp_path, "0100")
    assert task_exists_in_active(tmp_path, "0007")
