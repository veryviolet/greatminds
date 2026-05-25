"""Tests for tasks 0170 + 0171: real validator enforcement on
stand_wip → stand_done (0170) and feature_review → verified (0171).

Pre-0170/0171 the schema rows had:
  - ``stand_wip → stand_done    requires: [stand_result_block, ...]``
  - ``feature_review → verified requires: [review_block_approved, ...]``

But ``stand_result_block`` and ``review_block_approved`` were both
``_noop_existing`` placeholders. STAND-KEEPER could mv to stand_done
without ever appending a stand_result block; ARCHITECT-REVIEWER could
mv to verified without a review block (or with outcome != approved).

0170 + 0171 wire real validator functions in. These tests exercise
them directly with synthetic task dicts.
"""
from __future__ import annotations

import pytest

from greatminds.cli import task as task_mod


# ---------- 0170: stand_result_block ----------


def test_stand_result_block_rejects_when_no_stand_result_block() -> None:
    """0170 contract: a stand_wip task with NO stand_result block must
    be rejected at mv-to-stand_done time. Pre-0170 STAND-KEEPER could
    silently mv → orphan task in stand_done with no result, evidence,
    or commit."""
    data = {
        "id": "0999-test",
        "stream": "stand",
        "blocks": [
            {"kind": "triage", "by": "ARCHITECT-PLANNER",
             "at": "2026-05-25T00:00:00Z"},
        ],
    }
    msg = task_mod._check_stand_result_block(data, "stand_wip", "stand_done")
    assert msg is not None
    assert "stand_result_block" in msg
    # Error message points at the recovery action.
    assert "stand_result" in msg
    assert "append-block" in msg.lower() or "append-block" in msg


def test_stand_result_block_accepts_when_stand_result_present() -> None:
    """A stand_wip task with at least one stand_result block (any
    result value) passes."""
    data = {
        "id": "0999-test",
        "stream": "stand",
        "blocks": [
            {"kind": "stand_result", "by": "STAND-KEEPER",
             "at": "2026-05-25T00:00:00Z", "result": "ok",
             "stand_status": "READY", "commit": "abcd1234",
             "profile": "full-deploy"},
        ],
    }
    msg = task_mod._check_stand_result_block(data, "stand_wip", "stand_done")
    assert msg is None


def test_stand_result_block_accepts_any_result_value() -> None:
    """The validator's job is presence-check; ``result`` value
    (ok|partial|fail) is enforced by ``validate_block`` schema. A
    fail result still satisfies stand_result_block — it's still a
    legitimate stand outcome the operator wants archived."""
    for result in ("ok", "partial", "fail"):
        data = {
            "id": "0999-test",
            "blocks": [
                {"kind": "stand_result", "result": result},
            ],
        }
        assert task_mod._check_stand_result_block(
            data, "stand_wip", "stand_done"
        ) is None


def test_stand_result_block_registry_wired() -> None:
    """0170 wiring pin: SCHEMA_REQUIRES_VALIDATORS no longer maps
    ``stand_result_block`` to ``_noop_existing``. A regression here
    would re-open the validator hole."""
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS["stand_result_block"]
    assert fn is task_mod._check_stand_result_block, (
        "0170: stand_result_block validator must be wired to "
        "_check_stand_result_block, not _noop_existing"
    )


# ---------- 0171: review_block_approved ----------


def test_review_block_approved_rejects_when_no_review_block() -> None:
    """0171 contract: feature_review → verified rejects when the task
    has no review block at all. Pre-0171 ARCHITECT-REVIEWER could mv
    with zero approval evidence."""
    data = {
        "id": "0999-test",
        "blocks": [
            {"kind": "plan", "by": "ARCHITECT-PLANNER"},
            {"kind": "implementation", "by": "DEVELOPER"},
            {"kind": "tests", "by": "TESTER"},
        ],
    }
    msg = task_mod._check_review_block_approved(
        data, "feature_review", "verified",
    )
    assert msg is not None
    assert "review_block_approved" in msg
    assert "outcome=approved" in msg


def test_review_block_approved_rejects_when_latest_is_changes_requested() -> None:
    """The latest review block decides. A changes_requested review
    must NOT verify — that's the bounceback case which goes to a
    per-scope queue via a different transition."""
    data = {
        "id": "0999-test",
        "blocks": [
            {"kind": "review", "outcome": "changes_requested",
             "by": "ARCHITECT-REVIEWER"},
        ],
    }
    msg = task_mod._check_review_block_approved(
        data, "feature_review", "verified",
    )
    assert msg is not None
    assert "changes_requested" in msg


def test_review_block_approved_accepts_when_latest_is_approved() -> None:
    """Happy path: latest review block has outcome=approved → mv
    allowed."""
    data = {
        "id": "0999-test",
        "blocks": [
            {"kind": "review", "outcome": "approved",
             "by": "ARCHITECT-REVIEWER"},
        ],
    }
    msg = task_mod._check_review_block_approved(
        data, "feature_review", "verified",
    )
    assert msg is None


def test_review_block_approved_uses_LATEST_review_not_any() -> None:
    """Tasks ping-pong feature_review ↔ feature_dev across iterations.
    A task might have an older approved block from iter-1 plus a
    newer changes_requested block from iter-2's review. The LATEST
    one (changes_requested) decides — must reject the verify.

    Without latest-wins semantics, a malicious or buggy caller could
    bypass the gate by reusing an old approval."""
    data = {
        "id": "0999-test",
        "blocks": [
            {"kind": "review", "outcome": "approved",
             "by": "ARCHITECT-REVIEWER"},  # iter-1 approval
            {"kind": "implementation", "by": "DEVELOPER"},  # iter-2 fix
            {"kind": "tests", "by": "TESTER"},
            {"kind": "review", "outcome": "changes_requested",
             "by": "ARCHITECT-REVIEWER"},  # iter-2 review
        ],
    }
    msg = task_mod._check_review_block_approved(
        data, "feature_review", "verified",
    )
    assert msg is not None
    assert "changes_requested" in msg


def test_review_block_approved_registry_wired() -> None:
    """0171 wiring pin: SCHEMA_REQUIRES_VALIDATORS no longer maps
    ``review_block_approved`` to ``_noop_existing``."""
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS["review_block_approved"]
    assert fn is task_mod._check_review_block_approved, (
        "0171: review_block_approved validator must be wired to "
        "_check_review_block_approved, not _noop_existing"
    )
