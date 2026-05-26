"""Tests for task 0246 (0242d / Phase 5 of 0242): gate_check
rewrite — read lease evidence from the product task's tests block
instead of scanning ``coordination/stand_done/*.yaml``.

Pre-0246 gate_check scanned ``coordination/stand_done/`` and matched
``evidence_for: [<task-id>]`` entries. With the lease redesign
(0243-0245), the FSM transports stand-result evidence directly on
the tests block via ``stand_evidence`` carrying ``lease_id`` +
``result`` + ``commit``. 0246 makes gate_check read that path
first; the stand_done scan stays as backwards-compat fallback for
pre-0246 tasks (0247 removes the scan entirely).
"""
from __future__ import annotations

import pytest

from greatminds.cli import gate_check as gc_mod


# ---------- extract_lease_evidence_from_tests ----------


def test_extract_returns_none_without_tests_block() -> None:
    """No tests block → None (caller falls back to stand_done
    scan)."""
    merged = {"plan": {"stand_required": True}}
    assert gc_mod.extract_lease_evidence_from_tests(merged) is None


def test_extract_returns_none_without_stand_evidence() -> None:
    """Tests block present but no stand_evidence → None."""
    merged = {"tests": {"test_result": "pass", "base_commit": "abc"}}
    assert gc_mod.extract_lease_evidence_from_tests(merged) is None


def test_extract_returns_none_without_lease_id() -> None:
    """Stand_evidence present but no lease_id field → None.
    Backwards-compat: pre-0246 tests blocks with the OLD
    stand_evidence shape (no lease_id) fall through to the
    stand_done scan, not into the new lease comparison path."""
    merged = {"tests": {"stand_evidence": {
        "reproduction_steps": "x", "observed_with_fix": "y",
    }}}
    assert gc_mod.extract_lease_evidence_from_tests(merged) is None


def test_extract_returns_lease_evidence_dict() -> None:
    """Happy path: tests.stand_evidence with lease_id + result +
    commit → dict in the shape gate_check's comparison loop
    expects."""
    merged = {"tests": {
        "test_result": "pass",
        "gate_check_commit": "abc1234",
        "stand_evidence": {
            "lease_id": "lease-uuid-abc",
            "commit": "abc1234",
            "result": "pass",
            "ready_at": "2026-05-26T10:00:00Z",
            "released_at": "2026-05-26T11:00:00Z",
            "observed_with_fix": "container UP",
            "tester_observations": "curl /api returned [1,2,3]",
        },
    }}
    ev = gc_mod.extract_lease_evidence_from_tests(merged)
    assert ev is not None
    assert ev["lease_id"] == "lease-uuid-abc"
    assert ev["commit"] == "abc1234"
    assert ev["result"] == "pass"  # test_result wins over ev.result
    assert ev["ready_at"] == "2026-05-26T10:00:00Z"
    assert ev["tester_observations"] == "curl /api returned [1,2,3]"


def test_extract_prefers_test_result_over_evidence_result() -> None:
    """If TESTER's test_result disagrees with stand_evidence.result
    (shouldn't happen but defensive), test_result wins — TESTER is
    the source-of-truth on the verdict."""
    merged = {"tests": {
        "test_result": "fail",
        "stand_evidence": {
            "lease_id": "x",
            "result": "pass",   # disagrees
        },
    }}
    ev = gc_mod.extract_lease_evidence_from_tests(merged)
    assert ev["result"] == "fail"


def test_extract_falls_back_to_gate_check_commit_when_evidence_missing_commit() -> None:
    """If stand_evidence lacks commit but tests has
    gate_check_commit, use that (legacy field carries the same
    info)."""
    merged = {"tests": {
        "gate_check_commit": "fallback-sha",
        "stand_evidence": {"lease_id": "x"},
    }}
    ev = gc_mod.extract_lease_evidence_from_tests(merged)
    assert ev["commit"] == "fallback-sha"


def test_extract_captures_worktree_fingerprint() -> None:
    """0229 + 0246: worktree fingerprint stays in the lease
    evidence shape so the existing fingerprint-drift check fires
    on the new path too."""
    merged = {"tests": {"stand_evidence": {
        "lease_id": "x",
        "worktree_fingerprint": "abcdef0123",
    }}}
    ev = gc_mod.extract_lease_evidence_from_tests(merged)
    assert ev["worktree_fingerprint"] == "abcdef0123"


# ---------- gate_check pass/fail with lease evidence ----------


def _decide(merged, stand_dones=None):
    """Mimic the gate_check pass/fail decision for unit testing.
    Mirrors the inline logic in cli/gate_check.py."""
    lease_ev = gc_mod.extract_lease_evidence_from_tests(merged)
    if lease_ev is not None:
        candidates = [(
            type("P", (), {"name": "tests.stand_evidence"})(),
            lease_ev,
        )]
    else:
        candidates = stand_dones or []
    task_commit = gc_mod.get_task_commit(merged)
    task_fingerprint = gc_mod.get_task_worktree_fingerprint(merged)
    fail_reasons = []
    pass_any = False
    for _path, sr in candidates:
        result = sr.get("result")
        sr_commit = sr.get("commit")
        sr_fp = sr.get("worktree_fingerprint")
        if result not in ("pass", "ok"):
            fail_reasons.append(f"result={result!r}")
            continue
        if task_commit and sr_commit and not str(sr_commit).startswith(
            str(task_commit)
        ) and not str(task_commit).startswith(str(sr_commit)):
            fail_reasons.append("commit mismatch")
            continue
        if task_fingerprint and isinstance(sr_fp, str) and sr_fp:
            if task_fingerprint != sr_fp:
                fail_reasons.append("worktree_fingerprint mismatch")
                continue
        pass_any = True
        break
    return pass_any, fail_reasons


def test_gate_check_passes_when_lease_evidence_matches() -> None:
    """Happy path: tests.stand_evidence has matching commit,
    result=pass → gate_check pass."""
    merged = {
        "implementation": {"base_commit": "abc1234"},
        "tests": {
            "test_result": "pass",
            "stand_evidence": {
                "lease_id": "x",
                "commit": "abc1234",
            },
        },
    }
    pass_any, _ = _decide(merged)
    assert pass_any is True


def test_gate_check_fails_when_lease_commit_drifts() -> None:
    """Commit-drift via lease evidence path. The lease was deployed
    at a different commit than impl.base_commit names."""
    merged = {
        "implementation": {"base_commit": "task-sha"},
        "tests": {
            "test_result": "pass",
            "stand_evidence": {
                "lease_id": "x",
                "commit": "lease-sha",
            },
        },
    }
    pass_any, reasons = _decide(merged)
    assert pass_any is False
    assert any("commit mismatch" in r for r in reasons)


def test_gate_check_fails_when_lease_result_is_fail() -> None:
    """Lease released with result=fail → gate fails."""
    merged = {
        "implementation": {"base_commit": "abc"},
        "tests": {
            "test_result": "fail",
            "stand_evidence": {"lease_id": "x", "commit": "abc"},
        },
    }
    pass_any, reasons = _decide(merged)
    assert pass_any is False
    assert any("result=" in r for r in reasons)


def test_gate_check_falls_back_to_stand_done_when_no_lease() -> None:
    """0246 backwards-compat: pre-0246 task with no lease evidence
    on tests block → gate_check falls back to stand_done scan."""
    merged = {
        "implementation": {"base_commit": "abc"},
        "tests": {"test_result": "pass"},  # no stand_evidence.lease_id
    }
    stand_dones = [(
        type("P", (), {"name": "stand_done/0099.yaml"})(),
        {"result": "ok", "commit": "abc"},
    )]
    pass_any, _ = _decide(merged, stand_dones=stand_dones)
    assert pass_any is True


def test_gate_check_lease_fingerprint_drift_fails() -> None:
    """0229 + 0246: lease evidence carries a worktree_fingerprint
    that drifts from the impl's → fail."""
    merged = {
        "implementation": {
            "base_commit": "abc",
            "worktree_fingerprint": "iter1-fp",
        },
        "tests": {
            "test_result": "pass",
            "stand_evidence": {
                "lease_id": "x",
                "commit": "abc",
                "worktree_fingerprint": "iter2-fp",
            },
        },
    }
    pass_any, reasons = _decide(merged)
    assert pass_any is False
    assert any("fingerprint" in r for r in reasons)
