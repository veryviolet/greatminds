"""Regression tests for task 0103: schema.yaml transitions[].requires
is load-bearing.

Before 0103 the `requires:` list was documentary-only — the
``greatminds task`` CLI never parsed it. New keys added to schema
silently no-op'd, which is exactly the FSM-hole class that 0102 / 0105
follow-ups need to close further holes.

These tests pin:
  - SCHEMA_REQUIRES_VALIDATORS exists and covers every name actually
    used by transitions in schema.yaml.
  - An unknown require name raises a clear error (so future schema
    edits cannot accidentally introduce a documentary key).
  - gate_check_pass_if_stand_required no longer trusts the
    tests.gate_check_result field — it re-evaluates the gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _schema_requires_names() -> set[str]:
    """Return every name that appears in any transitions[].requires
    list across the canonical schema.yaml."""
    from greatminds.core.paths import find_canon_dir
    data = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    out: set[str] = set()
    for t in data.get("transitions") or []:
        for r in (t.get("requires") or []):
            if isinstance(r, str):
                out.add(r)
    return out


def test_every_schema_requires_name_has_a_validator() -> None:
    """SCHEMA_REQUIRES_VALIDATORS must cover every name the schema uses.
    An unmapped name would raise at mv time — better to catch at
    test time so editors of schema.yaml see the gap immediately."""
    from greatminds.cli.task import SCHEMA_REQUIRES_VALIDATORS
    schema_names = _schema_requires_names()
    registered = set(SCHEMA_REQUIRES_VALIDATORS.keys())
    missing = schema_names - registered
    assert missing == set(), (
        f"schema.yaml references requires-keys with no registered "
        f"validator: {sorted(missing)}. Add entries to "
        f"SCHEMA_REQUIRES_VALIDATORS in cli/task.py."
    )


def test_enforce_schema_requires_rejects_unknown_name() -> None:
    """If a (somehow) unknown require name appeared in a schema row,
    the validator must raise rather than silently no-op. Tested by
    directly invoking the function with a fake row + monkeypatched
    transitions_for."""
    from greatminds.cli import task as task_mod
    from greatminds.core.errors import GreatMindsError

    fake_row = {"from": "feature_test", "to": "feature_review",
                "by": "TESTER", "requires": ["this_key_is_not_registered"]}

    def fake_transitions_for(from_q: str, to_q: str):
        return [fake_row]

    orig = task_mod.transitions_for
    task_mod.transitions_for = fake_transitions_for
    try:
        with pytest.raises(GreatMindsError) as excinfo:
            task_mod.enforce_schema_requires(
                {}, "TESTER", "feature_test", "feature_review",
            )
        assert "this_key_is_not_registered" in str(excinfo.value)
        assert "no registered validator" in str(excinfo.value)
    finally:
        task_mod.transitions_for = orig


def test_enforce_schema_requires_runs_for_current_owner_rows() -> None:
    """REVIEWER iter-N+1 (0103): row matching must mirror can_role_move
    authorization, including ``by: current_owner``. The canonical
    any_active_queue → feature_blocked row uses current_owner; if
    enforce_schema_requires skips it, an unknown requires key there
    would land as documentary — the exact hole 0103 closes.

    Build a fake current_owner row with an unregistered requires key,
    confirm enforce_schema_requires raises (because the owner of
    feature_dev is DEVELOPER, so DEVELOPER → current_owner row is
    authorized and its requires must be checked).
    """
    from greatminds.cli import task as task_mod
    from greatminds.core.errors import GreatMindsError

    fake_row = {"from": "any_active_queue", "to": "feature_blocked",
                "by": "current_owner",
                "requires": ["unregistered_current_owner_key"]}

    def fake_transitions_for(from_q: str, to_q: str):
        return [fake_row]

    orig = task_mod.transitions_for
    task_mod.transitions_for = fake_transitions_for
    try:
        with pytest.raises(GreatMindsError) as excinfo:
            task_mod.enforce_schema_requires(
                {}, "DEVELOPER", "feature_dev", "feature_blocked",
            )
        msg = str(excinfo.value)
        assert "unregistered_current_owner_key" in msg
        assert "no registered validator" in msg
    finally:
        task_mod.transitions_for = orig


def test_enforce_schema_requires_skips_current_owner_row_for_non_owner() -> None:
    """current_owner row authorizes only owner/writers. A non-owner
    role calling enforce on a (from, to) where the only matching row
    is current_owner should NOT trip the unknown-key error — the row
    doesn't authorize them, so can_role_move would have already
    rejected the move.
    """
    from greatminds.cli import task as task_mod

    fake_row = {"from": "any_active_queue", "to": "feature_blocked",
                "by": "current_owner",
                "requires": ["unregistered_current_owner_key"]}

    def fake_transitions_for(from_q: str, to_q: str):
        return [fake_row]

    orig = task_mod.transitions_for
    task_mod.transitions_for = fake_transitions_for
    try:
        # TESTER does not own feature_dev. Must NOT raise — no
        # authorizing row for this role, so no requires to enforce.
        task_mod.enforce_schema_requires(
            {}, "TESTER", "feature_dev", "feature_blocked",
        )
    finally:
        task_mod.transitions_for = orig


def test_gate_check_validator_returns_none_when_stand_not_required() -> None:
    """The validator is a no-op for stand_required: false plans."""
    from greatminds.cli.task import _check_gate_for_stand_required
    data = {"blocks": [
        {"kind": "plan", "stand_required": False},
    ]}
    assert _check_gate_for_stand_required(data, "feature_test", "feature_review") is None


def test_gate_check_validator_fails_without_evidence(tmp_path: Path, monkeypatch) -> None:
    """When stand_required is true and no stand_done evidence exists,
    the validator must return an error message (NOT None). This is
    the bug 0103 fixes: TESTER cannot write pass into the tests block
    to bypass — the validator actually runs gate-check."""
    from greatminds.cli import task as task_mod
    from greatminds.cli import gate_check as gc_mod

    data = {"id": "0999-fake-task",
            "blocks": [{"kind": "plan",
                        "stand_required": True,
                        "base_commit": "deadbeef"}]}

    # No candidates → gate-check returns "missing".
    monkeypatch.setattr(gc_mod, "find_stand_evidence", lambda *_a, **_k: [])

    err = task_mod._check_gate_for_stand_required(
        data, "feature_test", "feature_review",
    )
    assert err is not None
    assert "gate_check_pass_if_stand_required" in err
    assert "missing" in err
