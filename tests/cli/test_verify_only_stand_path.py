"""Tests for the no-code stand/playbook verification path (plan.verify_only).

A plan marked ``verify_only: true`` routes feature_plan → feature_test
directly (no implementer step): TESTER leases a stand, runs the
playbook/probe, records stand evidence. Mirrors the audit_only path.
Covers the helper, the require-validator, the readiness bypass, and that
the canon schema actually declares the transition.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import greatminds
from greatminds.cli import task as t
from greatminds.core.errors import GreatMindsError


def _task(*, verify_only=None, ready=True):
    plan = {"kind": "plan", "by": "ARCHITECT-PLANNER",
            "at": "2026-06-04T00:00:00Z", "ready_for_implementation": ready}
    if verify_only is not None:
        plan["verify_only"] = verify_only
    return {"id": "0001-x", "blocks": [plan]}


# ---------- helper ----------


def test_is_verify_only():
    assert t.is_verify_only(_task(verify_only=True)) is True
    assert t.is_verify_only(_task(verify_only=False)) is False
    assert t.is_verify_only(_task()) is False  # absent → False


# ---------- require-validator ----------


def test_validator_passes_when_verify_only_true():
    assert t._check_plan_verify_only(
        _task(verify_only=True), "feature_plan", "feature_test") is None


def test_validator_fails_without_verify_only():
    msg = t._check_plan_verify_only(_task(), "feature_plan", "feature_test")
    assert msg is not None and "verify_only" in msg


def test_validator_registered():
    assert "plan.verify_only" in t.SCHEMA_REQUIRES_VALIDATORS
    assert t.SCHEMA_REQUIRES_VALIDATORS["plan.verify_only"] is t._check_plan_verify_only


# ---------- require_target_readiness bypass ----------


def test_readiness_ok_for_verify_only_ready_plan():
    # verify_only + ready_for_implementation → no raise (implementer bypass).
    t.require_target_readiness(_task(verify_only=True, ready=True),
                               "feature_plan", "feature_test")


def test_readiness_rejects_plan_without_verify_only():
    with pytest.raises(GreatMindsError) as e:
        t.require_target_readiness(_task(verify_only=None),
                                   "feature_plan", "feature_test")
    assert "verify_only" in str(e.value)


def test_readiness_requires_ready_for_implementation():
    with pytest.raises(GreatMindsError) as e:
        t.require_target_readiness(_task(verify_only=True, ready=False),
                                   "feature_plan", "feature_test")
    assert "ready_for_implementation" in str(e.value)


def test_implementer_to_feature_test_still_needs_implementation():
    """feature_dev → feature_test must STILL go through the implementation
    gate — the verify_only bypass is feature_plan-only."""
    data = {"id": "0002", "blocks": [
        {"kind": "implementation", "by": "DEVELOPER",
         "at": "2026-06-04T00:00:00Z", "ready_for_test": False}]}
    with pytest.raises(GreatMindsError):
        t.require_target_readiness(data, "feature_dev", "feature_test")


# ---------- canon schema declares the transition ----------


def test_canon_schema_has_verify_only_transition():
    schema = yaml.safe_load(
        (Path(greatminds.__file__).parent / "data" / "schema.yaml")
        .read_text(encoding="utf-8"))
    rows = [r for r in schema["transitions"]
            if r.get("from") == "feature_plan" and r.get("to") == "feature_test"]
    assert len(rows) == 1, "expected exactly one feature_plan → feature_test row"
    row = rows[0]
    assert row["by"] == "ARCHITECT-PLANNER"
    assert "plan.verify_only" in row["requires"]
