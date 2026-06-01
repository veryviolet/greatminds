"""Tests for task 0301: align schema.tests_block_validation.
stand_evidence.required_subfields with what gate_check actually
demands (lease_id + result + commit on top of the 3 prose
fields).

Pre-0301 schema listed only 3 fields; gate_check returned
``missing`` for every well-formed lease release because
``extract_lease_evidence_from_tests`` short-circuits on missing
lease_id. Schema is now normative; the validator reads from it.
"""
from __future__ import annotations

import yaml

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


def _schema_required_subfields() -> list[str]:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return (((doc.get("tests_block_validation") or {})
             .get("stand_evidence") or {})
            .get("required_subfields") or [])


# ---------- schema source-of-truth ----------


def test_schema_required_subfields_includes_lease_id() -> None:
    """0301 pin: schema must list ``lease_id`` so the contract
    matches gate_check.extract_lease_evidence_from_tests's actual
    early-return condition."""
    fields = _schema_required_subfields()
    assert "lease_id" in fields, (
        f"0301: schema.tests_block_validation.stand_evidence."
        f"required_subfields must include 'lease_id' (got {fields!r})"
    )


def test_schema_required_subfields_includes_lease_metadata() -> None:
    """0301: schema lists the lease metadata fields gate_check
    consumes — ``result`` and ``commit`` (both have fallbacks
    but are part of the documented contract)."""
    fields = _schema_required_subfields()
    assert "result" in fields
    assert "commit" in fields


def test_schema_keeps_legacy_prose_subfields() -> None:
    """Regression net: 0091's three free-form prose fields must
    survive the 0301 expansion. They're orthogonal to the lease
    metadata + still required by TESTER's contract."""
    fields = _schema_required_subfields()
    for legacy in ("reproduction_steps",
                    "observed_without_fix",
                    "observed_with_fix"):
        assert legacy in fields


# ---------- validator reads from schema ----------


def _plan() -> dict:
    return {"kind": "plan",
            "stand_required": True,
            "base_commit": "deadbeef"}


def _tests_block(**ev_overrides) -> dict:
    ev = {
        "reproduction_steps": "step1",
        "observed_without_fix": "no fix",
        "observed_with_fix": "with fix",
        "lease_id": "lease-uuid",
        "result": "pass",
        "commit": "deadbeef",
    }
    ev.update(ev_overrides)
    return {
        "kind": "tests",
        "stand_evidence": ev,
    }


def test_validator_accepts_complete_stand_evidence() -> None:
    """Happy path: all 6 schema-listed subfields populated → no
    raise."""
    data = {"blocks": [_plan()]}
    # Should NOT raise.
    task_mod.require_block_cross_state(_tests_block(), data)


def test_validator_rejects_missing_lease_id() -> None:
    """0301 contract: a tests block without ``lease_id`` is now
    rejected at append-block time (not waiting for the mv gate)."""
    data = {"blocks": [_plan()]}
    block = _tests_block(lease_id="")
    import pytest as _pytest
    with _pytest.raises(GreatMindsError) as exc:
        task_mod.require_block_cross_state(block, data)
    assert "lease_id" in str(exc.value)


def test_validator_rejects_missing_legacy_prose_subfield() -> None:
    """Regression net: the 0091 prose subfields are still
    enforced."""
    data = {"blocks": [_plan()]}
    block = _tests_block(reproduction_steps="")
    import pytest as _pytest
    with _pytest.raises(GreatMindsError) as exc:
        task_mod.require_block_cross_state(block, data)
    assert "reproduction_steps" in str(exc.value)


def test_validator_skips_for_non_stand_required_task() -> None:
    """No raise when plan.stand_required is False — the schema
    contract is conditional (``required_when: plan.stand_required``).
    """
    plan = _plan()
    plan["stand_required"] = False
    data = {"blocks": [plan]}
    block = _tests_block(lease_id="")  # missing — but plan opts out
    # Must not raise.
    task_mod.require_block_cross_state(block, data)


def test_validator_uses_schema_list_not_hardcoded() -> None:
    """0301: the validator's required fields match the schema's
    list. Adding a new field to schema must propagate to the
    validator without code changes."""
    schema_fields = set(_schema_required_subfields())
    # Build a tests block missing ONE of the schema-listed fields
    # at a time; assert the validator complains about that field.
    import pytest as _pytest
    for field in schema_fields:
        block = _tests_block(**{field: ""})
        with _pytest.raises(GreatMindsError) as exc:
            task_mod.require_block_cross_state(
                block, {"blocks": [_plan()]})
        assert field in str(exc.value), (
            f"0301: validator must surface the missing schema field "
            f"{field!r} (got: {exc.value})"
        )
