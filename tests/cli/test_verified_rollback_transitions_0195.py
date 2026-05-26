"""Tests for task 0195: verified → {archive, feature_review} rollback
transitions.

Pre-0195 the schema had NO transition out of ``verified/``. When a
task was reverted at the code level (e.g. 0192 reverted 2026-05-26
via commit 9d12466), the FSM record was stuck in verified/ forever
even though the code no longer carried the work.

0195 adds two paths gated on a ``rollback`` block with non-empty
``reason``:
- verified → archive       (withdraw — work reverted)
- verified → feature_review (revisit — work needs amendment)
"""
from __future__ import annotations

import pytest
import yaml

from greatminds.cli import task as task_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema additions ----------


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def test_schema_has_verified_to_archive_transition() -> None:
    """0195 schema pin: ``transitions:`` includes verified → archive
    by ARCHITECT-REVIEWER with rollback_block_with_reason."""
    rows = _schema()["transitions"]
    matches = [
        r for r in rows
        if r.get("from") == "verified" and r.get("to") == "archive"
    ]
    assert matches, "0195: missing transition verified → archive"
    row = matches[0]
    assert row["by"] == "ARCHITECT-REVIEWER"
    assert "rollback_block_with_reason" in row.get("requires", [])


def test_schema_has_verified_to_feature_review_transition() -> None:
    """0195: verified → feature_review (revisit) similarly gated."""
    rows = _schema()["transitions"]
    matches = [
        r for r in rows
        if r.get("from") == "verified" and r.get("to") == "feature_review"
    ]
    assert matches
    row = matches[0]
    assert row["by"] == "ARCHITECT-REVIEWER"
    assert "rollback_block_with_reason" in row.get("requires", [])


def test_schema_streams_product_allows_rollback_kind() -> None:
    """0195: product stream's allowed_block_kinds includes 'rollback'."""
    streams = _schema()["streams"]
    assert "rollback" in streams["product"]["allowed_block_kinds"]


def test_schema_block_kinds_rollback_authored_by_reviewer() -> None:
    """0195: only ARCHITECT-REVIEWER may produce a rollback block."""
    bk = _schema()["block_kinds"]
    assert "rollback" in bk
    assert bk["rollback"]["authored_by"] == ["ARCHITECT-REVIEWER"]


def test_schema_queue_accepts_blocks_verified_allows_rollback() -> None:
    """0195: verified queue accepts the rollback block (was terminal
    pre-0195 with no accepted blocks)."""
    q = _schema()["queue_accepts_blocks"]
    assert "rollback" in q["verified"]


# ---------- in-process constants reflect schema (0174 + 0195) ----------


def test_module_constants_reflect_rollback() -> None:
    """0174 + 0195 wiring: cli/task.py constants load from schema, so
    the rollback additions must appear in the loaded sets without any
    code change beyond schema edits."""
    assert "rollback" in task_mod.STREAM_BLOCK_KINDS["product"]
    assert task_mod.BLOCK_KIND_ROLES["rollback"] == {"ARCHITECT-REVIEWER"}
    assert "rollback" in task_mod.QUEUE_BLOCK_KINDS["verified"]


# ---------- validate_block: rollback branch ----------


def test_validate_block_rollback_rejects_missing_reason() -> None:
    """0195 contract: rollback block with no ``reason`` field is
    rejected at append-block time."""
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod.validate_block("product", {
            "kind": "rollback",
            "by": "ARCHITECT-REVIEWER",
            "at": "2026-05-26T00:00:00Z",
        })
    assert "reason" in str(exc.value).lower()


def test_validate_block_rollback_rejects_empty_reason() -> None:
    """Whitespace-only reason → reject. Empty reason kills the
    FSM-record value: an operator can't tell WHY this was rolled back."""
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod.validate_block("product", {
            "kind": "rollback",
            "by": "ARCHITECT-REVIEWER",
            "at": "2026-05-26T00:00:00Z",
            "reason": "   ",
        })
    assert "reason" in str(exc.value).lower()


def test_validate_block_rollback_accepts_non_empty_reason() -> None:
    """Happy path: non-empty reason → validates."""
    task_mod.validate_block("product", {
        "kind": "rollback",
        "by": "ARCHITECT-REVIEWER",
        "at": "2026-05-26T00:00:00Z",
        "reason": "0192 reverted per USER directive; superseded by 0193",
    })


# ---------- _check_rollback_block_with_reason ----------


def test_check_rejects_no_rollback_block() -> None:
    """0195: verified → archive without any rollback block in the
    task data → reject."""
    data = {"blocks": [{"kind": "plan"}, {"kind": "review",
                                            "outcome": "approved"}]}
    msg = task_mod._check_rollback_block_with_reason(
        data, "verified", "archive",
    )
    assert msg is not None
    assert "rollback_block_with_reason" in msg


def test_check_rejects_empty_reason() -> None:
    data = {"blocks": [{"kind": "rollback", "reason": ""}]}
    msg = task_mod._check_rollback_block_with_reason(
        data, "verified", "archive",
    )
    assert msg is not None


def test_check_accepts_valid_rollback_block() -> None:
    data = {"blocks": [{"kind": "rollback",
                        "reason": "withdrawn 2026-05-26"}]}
    msg = task_mod._check_rollback_block_with_reason(
        data, "verified", "archive",
    )
    assert msg is None


def test_check_uses_latest_rollback_when_multiple() -> None:
    """Latest-wins: a task can accumulate multiple rollback blocks
    (rollback → revisit → revisit-rollback). The latest decides."""
    data = {
        "blocks": [
            {"kind": "rollback", "reason": "first revert"},
            # Implementation iteration, etc.
            {"kind": "implementation"},
            {"kind": "rollback", "reason": "second revert"},
        ]
    }
    msg = task_mod._check_rollback_block_with_reason(
        data, "verified", "archive",
    )
    assert msg is None


def test_check_registry_wired() -> None:
    """0195 wiring pin: the validator is registered in
    SCHEMA_REQUIRES_VALIDATORS."""
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS["rollback_block_with_reason"]
    assert fn is task_mod._check_rollback_block_with_reason


# ---------- require_target_readiness pre-schema gate ----------


def test_require_target_readiness_allows_verified_to_feature_review() -> None:
    """0195 iter-2 (REVIEWER-flagged regression): the hardcoded gate
    in require_target_readiness for to_q=="feature_review" had a
    catch-all ``else: raise`` branch that rejected ALL from_q outside
    {feature_test, feature_docs_review, feature_blocked}. Without the
    explicit ``elif from_q == "verified": return``, the schema's
    rollback_block_with_reason validator never gets a chance to run
    — every verified→feature_review mv would bounce at the pre-
    schema gate.

    Pin the fix: passing data with NO rollback block should NOT
    raise from require_target_readiness — the schema validator
    raises later (tested elsewhere)."""
    data = {"blocks": []}  # no rollback block; schema would reject
    # require_target_readiness should NOT raise — the readiness
    # check is satisfied by the from_q="verified" allow.
    task_mod.require_target_readiness(data, "verified", "feature_review")
    # archive path was already allowed by the catch-all READY_FLAG_PER_TARGET
    # default (no rule for archive); verify it doesn't regress either.
    task_mod.require_target_readiness(data, "verified", "archive")
