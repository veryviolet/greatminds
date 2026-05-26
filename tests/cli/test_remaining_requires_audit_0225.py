"""Tests for task 0225: audit + close the remaining
``_noop_existing`` schema-requires entries (follow-up to 0222).

Per stand_done/0224 there were 11 entries still pointing at
``_noop_existing``. 0225 classifies each:

- **Real prerequisites** (now real validators):
  - tests_block_fail_or_partial
  - review_block_changes_requested
  - all_dependencies_exist_per_wake_check
  - evidence_for_if_related_product_task

- **Documentary** (kept as ``_noop_existing`` with explicit comment
  explaining where enforcement actually lives; the mv-time gate is
  legitimately empty-semantic):
  - plan_block, scope_backend, scope_ui, scope_docs, plan.audit_only,
    implementation_block, tests_block,
    blocked_block_with_dependencies_and_resume_to

This file tests the four new validators + pins the documentary-only
allowlist so future audits classify them correctly.
"""
from __future__ import annotations

import pytest

from greatminds.cli import task as task_mod


# ---------- _check_tests_block_fail_or_partial ----------


def test_tests_handback_rejects_missing_block() -> None:
    """feature_test → feature_dev without any tests block → reject."""
    msg = task_mod._check_tests_block_fail_or_partial(
        {"blocks": []}, "feature_test", "feature_dev",
    )
    assert msg is not None
    assert "tests_block_fail_or_partial" in msg


def test_tests_handback_rejects_pass_outcome() -> None:
    """test_result=pass → reject (forward path is feature_test →
    feature_review, not feature_dev)."""
    data = {"blocks": [{"kind": "tests", "test_result": "pass"}]}
    msg = task_mod._check_tests_block_fail_or_partial(
        data, "feature_test", "feature_dev",
    )
    assert msg is not None
    assert "'pass'" in msg


def test_tests_handback_accepts_fail() -> None:
    data = {"blocks": [{"kind": "tests", "test_result": "fail"}]}
    assert task_mod._check_tests_block_fail_or_partial(
        data, "feature_test", "feature_dev",
    ) is None


def test_tests_handback_accepts_partial() -> None:
    data = {"blocks": [{"kind": "tests", "test_result": "partial"}]}
    assert task_mod._check_tests_block_fail_or_partial(
        data, "feature_test", "feature_ui_dev",
    ) is None


# ---------- _check_review_block_changes_requested ----------


def test_review_handback_rejects_missing_block() -> None:
    msg = task_mod._check_review_block_changes_requested(
        {"blocks": []}, "feature_review", "feature_dev",
    )
    assert msg is not None


def test_review_handback_rejects_approved_outcome() -> None:
    data = {"blocks": [{"kind": "review", "outcome": "approved"}]}
    msg = task_mod._check_review_block_changes_requested(
        data, "feature_review", "feature_dev",
    )
    assert msg is not None
    assert "'approved'" in msg


def test_review_handback_accepts_changes_requested() -> None:
    data = {"blocks": [
        {"kind": "review", "outcome": "changes_requested"},
    ]}
    assert task_mod._check_review_block_changes_requested(
        data, "feature_review", "feature_dev",
    ) is None


# ---------- _check_all_dependencies_exist ----------


def test_all_dependencies_exist_rejects_missing_blocked_block(
    monkeypatch, tmp_path,
) -> None:
    """No blocked block → reject (task in feature_blocked SHOULD
    have one)."""
    monkeypatch.setattr(task_mod, "find_coord_dir",
                        lambda: tmp_path)
    msg = task_mod._check_all_dependencies_exist(
        {"blocks": []}, "feature_blocked", "feature_dev",
    )
    assert msg is not None


def test_all_dependencies_exist_rejects_when_dep_missing(
    monkeypatch, tmp_path,
) -> None:
    """Dependency file doesn't exist at named path → reject with
    named missing files."""
    coord = tmp_path / "coord"
    coord.mkdir()
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    data = {"blocks": [{
        "kind": "blocked",
        "dependencies": ["verified/0099-some-task.yaml"],
    }]}
    msg = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "feature_dev",
    )
    assert msg is not None
    assert "0099-some-task.yaml" in msg


def test_all_dependencies_exist_accepts_when_all_present(
    monkeypatch, tmp_path,
) -> None:
    coord = tmp_path / "coord"
    (coord / "verified").mkdir(parents=True)
    (coord / "verified" / "0099-task.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    data = {"blocks": [{
        "kind": "blocked",
        "dependencies": ["verified/0099-task.yaml"],
    }]}
    assert task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "feature_dev",
    ) is None


# ---------- _check_evidence_for_if_related_product_task ----------


def test_evidence_for_accepts_when_no_related_product_task() -> None:
    """No related_product_task declared → constraint vacuous → accept.
    Most infra-only stand requests fall in this branch."""
    data = {"blocks": [{
        "kind": "stand_result", "result": "ok",
    }]}
    assert task_mod._check_evidence_for_if_related_product_task(
        data, "stand_wip", "stand_done",
    ) is None


def test_evidence_for_rejects_when_related_but_no_evidence_for() -> None:
    """stand_result names related_product_task, but the request's
    evidence_for is empty → reject (evidence chain broken)."""
    data = {
        "evidence_for": [],
        "blocks": [{
            "kind": "stand_result",
            "result": "ok",
            "related_product_task": "0099-feature-task",
        }],
    }
    msg = task_mod._check_evidence_for_if_related_product_task(
        data, "stand_wip", "stand_done",
    )
    assert msg is not None
    assert "0099-feature-task" in msg


def test_evidence_for_accepts_when_related_and_evidence_for_present() -> None:
    data = {
        "evidence_for": ["0099-feature-task"],
        "blocks": [{
            "kind": "stand_result",
            "related_product_task": "0099-feature-task",
        }],
    }
    assert task_mod._check_evidence_for_if_related_product_task(
        data, "stand_wip", "stand_done",
    ) is None


def test_evidence_for_accepts_when_no_stand_result_block() -> None:
    """If the stand_result block is missing entirely, the
    stand_result_block validator handles it. This validator is
    vacuously OK."""
    assert task_mod._check_evidence_for_if_related_product_task(
        {"blocks": []}, "stand_wip", "stand_done",
    ) is None


# ---------- registry pins ----------


def test_tests_block_fail_or_partial_wired() -> None:
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS[
        "tests_block_fail_or_partial"
    ]
    assert fn is task_mod._check_tests_block_fail_or_partial


def test_review_block_changes_requested_wired() -> None:
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS[
        "review_block_changes_requested"
    ]
    assert fn is task_mod._check_review_block_changes_requested


def test_all_dependencies_exist_wired() -> None:
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS[
        "all_dependencies_exist_per_wake_check"
    ]
    assert fn is task_mod._check_all_dependencies_exist


def test_evidence_for_if_related_wired() -> None:
    fn = task_mod.SCHEMA_REQUIRES_VALIDATORS[
        "evidence_for_if_related_product_task"
    ]
    assert fn is task_mod._check_evidence_for_if_related_product_task


# ---------- documentary noop allowlist ----------

# 0225 audit: the entries below stay mapped to ``_noop_existing``
# DELIBERATELY because their schema-row prerequisites are enforced
# elsewhere (require_scope_match_on_routing, require_target_readiness,
# validate_block per-kind). Future audits should classify them as
# documentary, not as missed validators.
_DOCUMENTARY_NOOP_NAMES = frozenset([
    "plan_block",
    "scope_backend", "scope_ui", "scope_docs",
    "plan.audit_only",
    "implementation_block",
    "tests_block",
    "blocked_block_with_dependencies_and_resume_to",
])


def test_documentary_noops_stay_documentary() -> None:
    """Pin: each documentary entry still maps to _noop_existing.
    The validator function exists (registry entry isn't missing),
    but is intentionally a no-op. Future audits read this allowlist
    + the inline comments instead of misclassifying as a hole."""
    for name in _DOCUMENTARY_NOOP_NAMES:
        fn = task_mod.SCHEMA_REQUIRES_VALIDATORS.get(name)
        assert fn is task_mod._noop_existing, (
            f"0225: {name} expected to be documentary _noop_existing; "
            f"got {fn!r}. If you're promoting it to a real validator, "
            f"remove it from _DOCUMENTARY_NOOP_NAMES in this test."
        )


def test_no_undocumented_noops_remain() -> None:
    """0225 closure pin: every entry pointing at _noop_existing
    must be in the documentary allowlist. A new schema-requires
    name landing as _noop_existing without explicit classification
    repeats the 0170/0171/0222 hole class."""
    undocumented = []
    for name, fn in task_mod.SCHEMA_REQUIRES_VALIDATORS.items():
        if fn is task_mod._noop_existing and name not in _DOCUMENTARY_NOOP_NAMES:
            undocumented.append(name)
    assert not undocumented, (
        f"0225: {len(undocumented)} requires-name(s) silently noop'd: "
        f"{undocumented}. Either implement a real validator + register "
        f"it, or add the name to _DOCUMENTARY_NOOP_NAMES with a "
        f"comment in task.py explaining where enforcement lives."
    )
