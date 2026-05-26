"""Tests for task 0268: ``_evaluate_gate_check`` (cli/task.py) must
read lease evidence from ``tests.stand_evidence`` before falling back
to the legacy ``stand_done/`` scan.

Bug context: 0247 (1.3.0 BREAKING) removed the stand_done queue. The
parallel CLI helper ``greatminds gate-check`` was migrated to
``extract_lease_evidence_from_tests`` (0246) and works against the
new lease shape — but the in-process ``_evaluate_gate_check`` used by
``require_target_readiness`` on every ``task mv feature_test →
feature_review`` was missed. It still called ``find_stand_evidence``,
which now always returns ``[]`` on fresh fleets, so every legitimate
mv failed with ``gate_check_pass_if_stand_required: gate_check
returns 'missing'``.

0268 adds a lease-evidence-first path (mirroring the CLI) with a
fallback to the legacy scan for pre-1.3.0 fleets that still carry
stand_done/ files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import task as task_mod
from greatminds.cli import gate_check as gc_mod


def _plan(stand_required: bool = True, base_commit: str = "deadbeef") -> dict:
    return {"kind": "plan",
            "stand_required": stand_required,
            "base_commit": base_commit}


def _impl(base_commit: str = "deadbeef", files: list[str] | None = None) -> dict:
    return {"kind": "implementation",
            "base_commit": base_commit,
            "files": files or ["src/x.py"],
            "ready_for_test": True}


def _tests_with_lease(
    lease_id: str = "lease-deadbeef",
    commit: str = "deadbeef",
    test_result: str = "pass",
    gate_check_result: str = "pass",
) -> dict:
    return {
        "kind": "tests",
        "base_commit": commit,
        "test_files": ["tests/cli/x.py"],
        "test_command": "pytest tests/cli/x.py",
        "test_result": test_result,
        "gate_check_result": gate_check_result,
        "gate_check_at": "2026-05-27T00:00:00Z",
        "gate_check_commit": commit,
        "ready_for_review": True,
        "stand_evidence": {
            "lease_id": lease_id,
            "result": test_result,
            "commit": commit,
            "ready_at": "2026-05-26T23:00:00Z",
            "released_at": "2026-05-26T23:30:00Z",
        },
    }


# ---------- lease evidence path (post-1.3.0 happy path) ----------


def test_evaluate_gate_check_passes_via_lease_evidence(monkeypatch) -> None:
    """0268 contract: a task with ``tests.stand_evidence.lease_id`` set
    + matching commit + test_result=pass returns 'pass', WITHOUT
    needing a ``stand_done/<id>.yaml`` file."""
    data = {
        "id": "0999-fake-task",
        "blocks": [_plan(), _impl(), _tests_with_lease()],
    }
    # Make sure find_stand_evidence is NEVER consulted on the lease path.
    sentinel: list[bool] = []
    monkeypatch.setattr(
        gc_mod, "find_stand_evidence",
        lambda *_a, **_k: sentinel.append(True) or [],
    )
    assert task_mod._evaluate_gate_check(data) == "pass"
    assert sentinel == [], (
        "0268: when lease evidence is present, _evaluate_gate_check "
        "must not fall back to find_stand_evidence"
    )


def test_evaluate_gate_check_lease_evidence_with_test_result_fail() -> None:
    """test_result=fail on the lease side → 'fail'."""
    data = {
        "id": "0999-fake-task",
        "blocks": [_plan(), _impl(),
                   _tests_with_lease(test_result="fail",
                                     gate_check_result="fail")],
    }
    assert task_mod._evaluate_gate_check(data) == "fail"


def test_evaluate_gate_check_lease_commit_mismatch_returns_fail() -> None:
    """task base_commit != stand_evidence.commit → 'fail' (drift)."""
    data = {
        "id": "0999-fake-task",
        "blocks": [
            _plan(base_commit="aaaaaaaa"),
            _impl(base_commit="aaaaaaaa"),
            _tests_with_lease(commit="bbbbbbbb"),
        ],
    }
    assert task_mod._evaluate_gate_check(data) == "fail"


# ---------- legacy fall-back (pre-1.3.0 tasks with stand_done file) ----------


def test_evaluate_gate_check_falls_back_to_stand_done_scan(monkeypatch) -> None:
    """Pre-1.3.0 task: no ``tests.stand_evidence`` but a legacy
    ``stand_done/<id>.yaml`` is present. The fall-back path must reach
    ``find_stand_evidence`` and produce 'pass' when its result is ok."""
    data = {
        "id": "0888-legacy-task",
        "blocks": [
            _plan(base_commit="cafef00d"),
            _impl(base_commit="cafef00d"),
            {"kind": "tests",
             "base_commit": "cafef00d",
             "test_files": ["tests/cli/y.py"],
             "test_command": "pytest tests/cli/y.py",
             "test_result": "pass",
             "gate_check_result": "pass",
             "gate_check_at": "2026-05-27T00:00:00Z",
             "gate_check_commit": "cafef00d",
             "ready_for_review": True},
        ],
    }

    class _FakePath:
        name = "stand_done/0888.yaml"

    legacy_evidence = {"result": "pass", "commit": "cafef00d"}
    monkeypatch.setattr(
        gc_mod, "find_stand_evidence",
        lambda *_a, **_k: [(_FakePath(), legacy_evidence)],
    )
    assert task_mod._evaluate_gate_check(data) == "pass"


def test_evaluate_gate_check_missing_when_both_paths_empty(monkeypatch) -> None:
    """Fresh fleet, no lease evidence, no legacy file → 'missing'."""
    data = {
        "id": "0777-no-evidence",
        "blocks": [_plan(), _impl(),
                   {"kind": "tests",
                    "base_commit": "deadbeef",
                    "test_files": ["tests/cli/z.py"],
                    "test_command": "pytest tests/cli/z.py",
                    "test_result": "pass",
                    "gate_check_result": "pass",
                    "gate_check_at": "2026-05-27T00:00:00Z",
                    "gate_check_commit": "deadbeef",
                    "ready_for_review": True}],
    }
    monkeypatch.setattr(gc_mod, "find_stand_evidence",
                        lambda *_a, **_k: [])
    assert task_mod._evaluate_gate_check(data) == "missing"


# ---------- integration with _check_gate_for_stand_required ----------


def test_check_gate_validator_passes_with_lease_evidence(monkeypatch) -> None:
    """The validator wrapper consumed by ``require_target_readiness``
    must return None (no error) when the lease evidence shape is
    present on the tests block — i.e. ``task mv feature_test →
    feature_review`` succeeds for the post-1.3.0 happy path."""
    data = {
        "id": "0999-mv-probe",
        "blocks": [_plan(), _impl(), _tests_with_lease()],
    }
    monkeypatch.setattr(gc_mod, "find_stand_evidence",
                        lambda *_a, **_k: [])
    assert task_mod._check_gate_for_stand_required(
        data, "feature_test", "feature_review",
    ) is None
