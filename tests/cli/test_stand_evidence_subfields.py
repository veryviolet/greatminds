"""Regression tests for task 0091 item 3: tests.stand_evidence subfields.

When a task has plan.stand_required: true, the tests block appended to
it must include stand_evidence as a mapping with three required
subfields: reproduction_steps, observed_without_fix, observed_with_fix.
Mirrors COORDINATE.md §9 and the schema.tests_block_validation entry.

Tests directly invoke require_block_cross_state to avoid full CLI
plumbing.
"""
from __future__ import annotations

import pytest

from greatminds.cli.task import require_block_cross_state
from greatminds.core.errors import GreatMindsError


def _task_with_plan(stand_required: bool) -> dict:
    return {
        "stream": "product",
        "blocks": [
            {"kind": "plan",
             "by": "ARCHITECT-PLANNER",
             "at": "2026-05-25T00:00:00Z",
             "stand_required": stand_required,
             "base_commit": "deadbeef",
             "stand_reason": "see body" if stand_required else "",
             "assignee_role": "DEVELOPER",
             "ready_for_implementation": True,
             "plan_kind": "full",
             "mode": "A"},
        ],
    }


def _tests_block(stand_evidence=None) -> dict:
    block: dict = {
        "kind": "tests",
        "by": "TESTER",
        "at": "2026-05-25T00:00:00Z",
        "base_commit": "deadbeef",
        "test_files": ["tests/cli/test_x.py"],
        "test_command": ".venv/bin/pytest tests/cli/test_x.py",
        "test_result": "pass",
        "gate_check_result": "pass",
        "gate_check_at": "2026-05-25T00:00:00Z",
        "gate_check_commit": "deadbeef",
        "ready_for_review": True,
    }
    if stand_evidence is not None:
        block["stand_evidence"] = stand_evidence
    return block


def test_tests_block_accepts_complete_stand_evidence_mapping():
    """Happy path: all schema-required subfields set non-empty.
    0301 added lease_id/result/commit alongside the legacy 3
    prose fields — fixture extended."""
    data = _task_with_plan(stand_required=True)
    block = _tests_block(stand_evidence={
        "reproduction_steps": "ssh avatar; cmd1; cmd2",
        "observed_without_fix": "heartbeat stuck at +420s",
        "observed_with_fix": "heartbeat advanced within 10s",
        # 0301: schema also requires lease_id / result / commit.
        "lease_id": "lease-uuid",
        "result": "pass",
        "commit": "deadbeef",
    })
    # Must not raise.
    require_block_cross_state(block, data)


def test_tests_block_rejects_missing_stand_evidence_entirely():
    """No stand_evidence at all → refused (canon §9 + 0091 item 3)."""
    data = _task_with_plan(stand_required=True)
    block = _tests_block()  # no stand_evidence
    with pytest.raises(GreatMindsError) as excinfo:
        require_block_cross_state(block, data)
    msg = str(excinfo.value)
    assert "stand_evidence" in msg
    assert "0091 item 3" in msg or "tests_block_validation" in msg


def test_tests_block_rejects_scalar_string_stand_evidence():
    """Legacy 'stand_done/0083-…yaml' scalar form is no longer enough."""
    data = _task_with_plan(stand_required=True)
    block = _tests_block(stand_evidence="stand_done/0083-foo.yaml")
    with pytest.raises(GreatMindsError) as excinfo:
        require_block_cross_state(block, data)
    assert "mapping" in str(excinfo.value)


def test_tests_block_rejects_partial_subfields():
    """Missing one of the three subfields → refused, message names it."""
    data = _task_with_plan(stand_required=True)
    block = _tests_block(stand_evidence={
        "reproduction_steps": "step",
        "observed_without_fix": "before",
        # missing observed_with_fix
    })
    with pytest.raises(GreatMindsError) as excinfo:
        require_block_cross_state(block, data)
    msg = str(excinfo.value)
    assert "observed_with_fix" in msg


def test_tests_block_rejects_empty_string_subfield():
    """Subfield present but empty/whitespace → counted as missing."""
    data = _task_with_plan(stand_required=True)
    block = _tests_block(stand_evidence={
        "reproduction_steps": "step",
        "observed_without_fix": "",
        "observed_with_fix": "  ",
    })
    with pytest.raises(GreatMindsError) as excinfo:
        require_block_cross_state(block, data)
    msg = str(excinfo.value)
    assert "observed_without_fix" in msg or "observed_with_fix" in msg


def test_tests_block_skips_validation_when_stand_not_required():
    """plan.stand_required: false → tests block doesn't need stand_evidence."""
    data = _task_with_plan(stand_required=False)
    block = _tests_block()  # no stand_evidence
    # Must not raise — gate doesn't apply.
    require_block_cross_state(block, data)


def test_coerce_value_parses_mapping_for_stand_evidence():
    """REVIEWER 0091 iter blocker: ``--field stand_evidence='{a: 1, b: 2}'``
    must coerce to a dict so the mapping validator can pass through
    the CLI. Without this the CLI couldn't produce a valid tests
    block — stand_done/0110 only succeeded via direct Python API.
    """
    from greatminds.cli.task import coerce_value

    parsed = coerce_value(
        "stand_evidence",
        "{reproduction_steps: ssh; cmd, "
        "observed_without_fix: before, "
        "observed_with_fix: after}",
    )
    assert isinstance(parsed, dict)
    assert parsed["reproduction_steps"] == "ssh; cmd"
    assert parsed["observed_without_fix"] == "before"
    assert parsed["observed_with_fix"] == "after"


def test_coerce_value_passes_mapping_to_tests_block_via_cli():
    """End-to-end through coerce_value: a mapping field value lands as
    a dict in the block, which then satisfies require_block_cross_state.
    """
    from greatminds.cli.task import coerce_value

    block = _tests_block(stand_evidence=coerce_value(
        "stand_evidence",
        "{reproduction_steps: x, observed_without_fix: y, "
        "observed_with_fix: z, "
        # 0301: schema-required lease metadata fields.
        "lease_id: lease-uuid, result: pass, commit: deadbeef}",
    ))
    data = _task_with_plan(stand_required=True)
    # Must not raise.
    require_block_cross_state(block, data)


def test_coerce_value_keeps_non_mapping_strings_as_strings():
    """A normal string value (no leading `{`) must stay a string —
    no false-positive mapping interpretation."""
    from greatminds.cli.task import coerce_value

    assert coerce_value("notes", "hello world") == "hello world"
    assert coerce_value("notes", "true") is True  # bool path still works
    assert coerce_value("notes", "42") == 42  # int path still works


def test_coerce_value_malformed_mapping_falls_through_to_string():
    """If the value starts with `{` but isn't valid YAML mapping syntax,
    we fall back to the string path (rather than swallowing the input).
    """
    from greatminds.cli.task import coerce_value

    # Looks-like-a-mapping but with broken syntax → not a dict, stays a string.
    result = coerce_value("notes", "{not valid yaml mapping syntax")
    assert isinstance(result, str)
    assert "not valid yaml" in result
