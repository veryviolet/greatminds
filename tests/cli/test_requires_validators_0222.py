"""Tests for task 0222: schema-declared requires validators are
real (not _noop_existing) for triage and reader-review gates.

EXPLORER's stand_done/0205 ran each documented FSM transition with
deliberately-failing prerequisites. Four bounced through despite
schema declaring requires:
- T1  user_feedback → feature_inbox     requires [triage_block]
- T2  user_feedback → archive           requires [triage_block]
- T20 feature_docs_review → feature_review   requires [reader_block_pass]
- T21 feature_docs_review → feature_docs     requires [reader_block_fail_or_partial]

Root cause: SCHEMA_REQUIRES_VALIDATORS mapped those names to
_noop_existing. 0222 replaces with real validators + tests below
pin the new contract.
"""
from __future__ import annotations

import pytest

from greatminds.cli import task as task_mod


# ---------- _check_triage_block ----------


def test_triage_block_rejects_missing_block() -> None:
    """T1/T2 pre-0222: blocks=[] accepted → mv succeeded. Post-0222
    the validator rejects."""
    data = {"id": "0099-x", "blocks": []}
    msg = task_mod._check_triage_block(
        data, "user_feedback", "feature_inbox",
    )
    assert msg is not None
    assert "triage_block" in msg
    assert "append-block triage" in msg


def test_triage_block_rejects_empty_notes() -> None:
    """A triage block present but with empty notes leaves the FSM
    record meaningless (PLANNER didn't actually triage). Reject."""
    data = {"blocks": [
        {"kind": "triage", "by": "ARCHITECT-PLANNER", "notes": ""},
    ]}
    msg = task_mod._check_triage_block(
        data, "user_feedback", "feature_inbox",
    )
    assert msg is not None
    assert "notes" in msg.lower() or "body" in msg.lower()


def test_triage_block_accepts_with_notes() -> None:
    """Happy path: triage block with non-empty notes → no error."""
    data = {"blocks": [
        {"kind": "triage", "by": "ARCHITECT-PLANNER",
         "notes": "triaged into feature_inbox per scope:backend"},
    ]}
    assert task_mod._check_triage_block(
        data, "user_feedback", "feature_inbox",
    ) is None


def test_triage_block_accepts_body_field_as_alternative() -> None:
    """Some triage blocks store the routing rationale under ``body``
    instead of ``notes``. Both fields satisfy the validator."""
    data = {"blocks": [
        {"kind": "triage", "by": "ARCHITECT-PLANNER",
         "body": "duplicate of 0099; route to archive"},
    ]}
    assert task_mod._check_triage_block(
        data, "user_feedback", "archive",
    ) is None


def test_triage_block_uses_latest_when_multiple() -> None:
    """Multiple triage blocks → latest decides. A re-triage with
    empty notes overwrites an earlier valid one (rejection)."""
    data = {"blocks": [
        {"kind": "triage", "by": "ARCHITECT-PLANNER",
         "notes": "first triage"},
        {"kind": "triage", "by": "ARCHITECT-PLANNER", "notes": ""},
    ]}
    msg = task_mod._check_triage_block(
        data, "user_feedback", "feature_inbox",
    )
    assert msg is not None


# ---------- _check_reader_block_pass ----------


def test_reader_block_pass_rejects_missing_block() -> None:
    """T20 pre-0222: no reader_review block but mv accepted. Now
    rejected."""
    data = {"blocks": []}
    msg = task_mod._check_reader_block_pass(
        data, "feature_docs_review", "feature_review",
    )
    assert msg is not None
    assert "reader_block_pass" in msg


def test_reader_block_pass_rejects_fail_outcome() -> None:
    """outcome=fail → reject (forward path is for pass/approved only)."""
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "fail"},
    ]}
    msg = task_mod._check_reader_block_pass(
        data, "feature_docs_review", "feature_review",
    )
    assert msg is not None
    assert "'fail'" in msg


def test_reader_block_pass_accepts_pass_outcome() -> None:
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "pass"},
    ]}
    assert task_mod._check_reader_block_pass(
        data, "feature_docs_review", "feature_review",
    ) is None


def test_reader_block_pass_accepts_approved_outcome() -> None:
    """Some readers historically use 'approved' as a synonym; the
    validator accepts both."""
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "approved"},
    ]}
    assert task_mod._check_reader_block_pass(
        data, "feature_docs_review", "feature_review",
    ) is None


def test_reader_block_pass_latest_wins() -> None:
    """An earlier pass followed by a fresh fail → reject (latest)."""
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "pass"},
        {"kind": "reader_review", "outcome": "fail"},
    ]}
    msg = task_mod._check_reader_block_pass(
        data, "feature_docs_review", "feature_review",
    )
    assert msg is not None


# ---------- _check_reader_block_fail_or_partial ----------


def test_reader_block_fail_rejects_missing_block() -> None:
    """T21 pre-0222: no reader_review but hand-back mv accepted."""
    data = {"blocks": []}
    msg = task_mod._check_reader_block_fail_or_partial(
        data, "feature_docs_review", "feature_docs",
    )
    assert msg is not None


def test_reader_block_fail_rejects_pass_outcome() -> None:
    """outcome=pass → reject (hand-back path is for negative
    verdicts only)."""
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "pass"},
    ]}
    msg = task_mod._check_reader_block_fail_or_partial(
        data, "feature_docs_review", "feature_docs",
    )
    assert msg is not None
    assert "'pass'" in msg


def test_reader_block_fail_accepts_fail() -> None:
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "fail"},
    ]}
    assert task_mod._check_reader_block_fail_or_partial(
        data, "feature_docs_review", "feature_docs",
    ) is None


def test_reader_block_fail_accepts_partial() -> None:
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "partial"},
    ]}
    assert task_mod._check_reader_block_fail_or_partial(
        data, "feature_docs_review", "feature_docs",
    ) is None


def test_reader_block_fail_accepts_changes_requested() -> None:
    """Some readers use review's 'changes_requested' verb; accept."""
    data = {"blocks": [
        {"kind": "reader_review", "outcome": "changes_requested"},
    ]}
    assert task_mod._check_reader_block_fail_or_partial(
        data, "feature_docs_review", "feature_docs",
    ) is None


# ---------- registry wiring pins ----------


def test_triage_block_registry_wired() -> None:
    """0222 wiring pin: SCHEMA_REQUIRES_VALIDATORS['triage_block']
    is the real validator, not _noop_existing."""
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS["triage_block"]
    assert fn is task_mod._check_triage_block, (
        "0222: triage_block must point at _check_triage_block, not "
        "_noop_existing"
    )


def test_reader_block_pass_registry_wired() -> None:
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS["reader_block_pass"]
    assert fn is task_mod._check_reader_block_pass


def test_reader_block_fail_or_partial_registry_wired() -> None:
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS[
        "reader_block_fail_or_partial"
    ]
    assert fn is task_mod._check_reader_block_fail_or_partial
