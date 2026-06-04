"""Regression tests for `transitions_for` / `can_role_move`.

Bug context: schema.yaml may legitimately carry multiple `transitions[]`
rows with the same `(from, to)` and different `by:` roles — e.g.
`review_sessions → archive` permits both ARCHITECT-PLANNER and EXPLORER.
Previously `transition_for` returned the first match only, blocking
whichever role appeared second.
"""
from __future__ import annotations

import pytest

from greatminds.cli import task as task_mod


@pytest.fixture(autouse=True)
def _reset_schema_cache():
    """Each test installs its own schema; reset module-level cache."""
    task_mod._schema_cache = None
    yield
    task_mod._schema_cache = None


def _install_schema(monkeypatch, transitions: list[dict],
                    queues: dict[str, dict] | None = None) -> None:
    queues = queues or {
        "review_sessions": {"owner": "EXPLORER", "writers": ["ARCHITECT-PLANNER", "EXPLORER"]},
        "feature_dev":     {"owner": "DEVELOPER", "writers": ["DEVELOPER"]},
        "feature_blocked": {"owner": "ARCHITECT-REVIEWER",
                            "writers": ["DEVELOPER", "UI-DEVELOPER", "TECHNICAL-WRITER",
                                        "TESTER", "READER", "ARCHITECT-REVIEWER"]},
        "archive":         {"owner": "ARCHITECT-PLANNER", "writers": ["ARCHITECT-PLANNER"]},
    }
    monkeypatch.setattr(task_mod, "_schema_cache", {
        "version": 1,
        "queues": queues,
        "transitions": transitions,
    })


# ---------------------------------------------------------------------------
# transitions_for: multi-row + wildcard resolution
# ---------------------------------------------------------------------------


def test_transitions_for_returns_all_matching_rows(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "review_sessions", "to": "archive", "by": "ARCHITECT-PLANNER"},
        {"from": "review_sessions", "to": "archive", "by": "EXPLORER"},
    ])
    matches = task_mod.transitions_for("review_sessions", "archive")
    assert len(matches) == 2
    assert {m["by"] for m in matches} == {"ARCHITECT-PLANNER", "EXPLORER"}


def test_transitions_for_resolves_any_active_queue_wildcard(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "any_active_queue", "to": "feature_blocked", "by": "current_owner"},
    ])
    matches = task_mod.transitions_for("feature_dev", "feature_blocked")
    assert len(matches) == 1
    assert matches[0]["from"] == "any_active_queue"


def test_transitions_for_resolves_any_resume_to_queue_wildcard(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "feature_blocked", "to": "any_resume_to_queue",
         "by": "ARCHITECT-REVIEWER"},
    ])
    matches = task_mod.transitions_for("feature_blocked", "feature_dev")
    assert len(matches) == 1
    assert matches[0]["by"] == "ARCHITECT-REVIEWER"


def test_resume_wildcard_excludes_terminal_archive(monkeypatch):
    """Regression: ``any_resume_to_queue`` must NOT match the terminal
    ``archive`` queue. With both the exact withdraw row and the resume
    wildcard present (as in real schema), ``feature_blocked → archive``
    must match ONLY the withdraw row — otherwise the resume path's
    wake-check (all_dependencies_exist_per_wake_check) fires on a
    withdrawn task whose sentinel dependency never resolves, making
    archive impossible."""
    _install_schema(monkeypatch, [
        {"from": "feature_blocked", "to": "archive", "by": "ARCHITECT-REVIEWER",
         "requires": ["feature_blocked_withdrawn_reason"]},
        {"from": "feature_blocked", "to": "any_resume_to_queue",
         "by": "ARCHITECT-REVIEWER",
         "requires": ["all_dependencies_exist_per_wake_check"]},
    ], queues={
        "feature_blocked": {"owner": "ARCHITECT-REVIEWER",
                            "writers": ["ARCHITECT-REVIEWER"], "kind": "parking"},
        "feature_dev": {"owner": "DEVELOPER", "writers": ["DEVELOPER"],
                        "kind": "active"},
        "archive": {"owner": "ARCHITECT-PLANNER",
                    "writers": ["ARCHITECT-PLANNER", "ARCHITECT-REVIEWER"],
                    "kind": "terminal"},
        "verified": {"owner": "ARCHITECT-REVIEWER",
                     "writers": ["ARCHITECT-REVIEWER"], "kind": "terminal"},
    })
    # archive (terminal): only the exact withdraw row, no wake-check.
    arch = task_mod.transitions_for("feature_blocked", "archive")
    assert len(arch) == 1
    assert arch[0]["requires"] == ["feature_blocked_withdrawn_reason"]
    # verified (terminal): the wildcard must not invent a transition.
    assert task_mod.transitions_for("feature_blocked", "verified") == []
    # feature_dev (active resume): wildcard still applies, wake-check stays.
    resume = task_mod.transitions_for("feature_blocked", "feature_dev")
    assert len(resume) == 1
    assert resume[0]["requires"] == ["all_dependencies_exist_per_wake_check"]


def test_transitions_for_singular_back_compat(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "review_sessions", "to": "archive", "by": "ARCHITECT-PLANNER"},
        {"from": "review_sessions", "to": "archive", "by": "EXPLORER"},
    ])
    # transition_for returns the FIRST match, unchanged from prior behavior.
    t = task_mod.transition_for("review_sessions", "archive")
    assert t is not None
    assert t["by"] == "ARCHITECT-PLANNER"


# ---------------------------------------------------------------------------
# can_role_move: both roles for a two-row pair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema_order", [
    [
        {"from": "review_sessions", "to": "archive", "by": "ARCHITECT-PLANNER"},
        {"from": "review_sessions", "to": "archive", "by": "EXPLORER"},
    ],
    [
        # Flip the order — multi-by must not depend on which row is first.
        {"from": "review_sessions", "to": "archive", "by": "EXPLORER"},
        {"from": "review_sessions", "to": "archive", "by": "ARCHITECT-PLANNER"},
    ],
])
def test_both_authorized_roles_pass_regardless_of_schema_order(monkeypatch, schema_order):
    _install_schema(monkeypatch, schema_order)
    assert task_mod.can_role_move("ARCHITECT-PLANNER", "review_sessions",
                                  "archive", {}) is None
    assert task_mod.can_role_move("EXPLORER", "review_sessions",
                                  "archive", {}) is None


# ---------------------------------------------------------------------------
# can_role_move: single-row, wrong role
# ---------------------------------------------------------------------------


def test_single_row_wrong_role_lists_only_permitted_role(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "feature_dev", "to": "archive", "by": "ARCHITECT-PLANNER"},
    ])
    msg = task_mod.can_role_move("DEVELOPER", "feature_dev", "archive", {})
    assert msg is not None
    assert "only role ARCHITECT-PLANNER" in msg


def test_three_row_permission_error_lists_all_three_sorted(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "feature_dev", "to": "archive", "by": "ARCHITECT-REVIEWER"},
        {"from": "feature_dev", "to": "archive", "by": "ARCHITECT-PLANNER"},
        {"from": "feature_dev", "to": "archive", "by": "MAINTAINER"},
    ])
    msg = task_mod.can_role_move("DEVELOPER", "feature_dev", "archive", {})
    assert msg is not None
    # Sorted, joined with " or ".
    assert (
        "ARCHITECT-PLANNER or ARCHITECT-REVIEWER or MAINTAINER" in msg
    ), msg


# ---------------------------------------------------------------------------
# can_role_move: current_owner wildcard semantics preserved
# ---------------------------------------------------------------------------


def test_current_owner_literal_accepts_queue_owner(monkeypatch):
    _install_schema(monkeypatch, [
        {"from": "any_active_queue", "to": "feature_blocked", "by": "current_owner"},
    ])
    # DEVELOPER owns feature_dev → may park to feature_blocked.
    assert task_mod.can_role_move(
        "DEVELOPER", "feature_dev", "feature_blocked", {},
    ) is None


def test_current_owner_literal_accepts_queue_writer(monkeypatch):
    """A role listed in queue writers (but not the canonical owner) may
    still satisfy the current_owner literal — matches the existing
    `writers` check in the function."""
    _install_schema(monkeypatch, [
        {"from": "any_active_queue", "to": "feature_blocked", "by": "current_owner"},
    ])
    # TESTER is in feature_blocked.writers per the real schema; we mirror
    # by adding TESTER to feature_dev.writers in the install fixture.
    queues = {
        "feature_dev": {"owner": "DEVELOPER", "writers": ["DEVELOPER", "TESTER"]},
        "feature_blocked": {"owner": "ARCHITECT-REVIEWER", "writers": ["TESTER"]},
    }
    _install_schema(monkeypatch, [
        {"from": "any_active_queue", "to": "feature_blocked", "by": "current_owner"},
    ], queues=queues)
    assert task_mod.can_role_move(
        "TESTER", "feature_dev", "feature_blocked", {},
    ) is None


# ---------------------------------------------------------------------------
# requires: per-row distinction (documentary at runtime, no spurious rejection)
# ---------------------------------------------------------------------------


def test_authorized_role_passes_even_if_other_row_has_unmet_requires(monkeypatch):
    """Two rows for the same (from, to). Row A requires a plan_block; row B
    requires nothing. A task with no plan_block, called by the role
    authorized in row B, must succeed — the failing requires of row A
    must not block row B's caller.
    """
    _install_schema(monkeypatch, [
        {"from": "feature_dev", "to": "archive", "by": "ARCHITECT-PLANNER",
         "requires": ["plan_block"]},
        {"from": "feature_dev", "to": "archive", "by": "DEVELOPER",
         "requires": []},
    ])
    assert task_mod.can_role_move(
        "DEVELOPER", "feature_dev", "archive", {},  # no plan_block in task data
    ) is None


# ---------------------------------------------------------------------------
# No matching transition
# ---------------------------------------------------------------------------


def test_no_transition_returns_clear_error(monkeypatch):
    _install_schema(monkeypatch, [])
    msg = task_mod.can_role_move("DEVELOPER", "feature_dev", "archive", {})
    assert msg is not None
    assert "no transition feature_dev → archive in schema" in msg


# ---------------------------------------------------------------------------
# require_block_cross_state — audit_only relaxation (task 0025)
# ---------------------------------------------------------------------------


def _make_task_with_reader_outcome(audit_only: bool, outcome: str) -> dict:
    """Build a minimal task dict with a plan + reader_review block."""
    return {
        "id": "9999-fixture",
        "blocks": [
            {"kind": "plan", "audit_only": audit_only},
            {"kind": "reader_review", "outcome": outcome},
        ],
    }


def _approve_block() -> dict:
    return {"kind": "review", "outcome": "approved"}


def test_audit_only_partial_outcome_approves(monkeypatch):
    """REVIEWER may approve an audit-only task whose reader_review came
    back with outcome=partial — that's the EXPECTED useful result for an
    audit (findings consumed by PLANNER spawning fixer tasks)."""
    data = _make_task_with_reader_outcome(audit_only=True, outcome="partial")
    # No exception → approval would succeed.
    task_mod.require_block_cross_state(_approve_block(), data)


def test_audit_only_fail_outcome_approves(monkeypatch):
    """Same as partial — fail is also a legitimate audit conclusion."""
    data = _make_task_with_reader_outcome(audit_only=True, outcome="fail")
    task_mod.require_block_cross_state(_approve_block(), data)


def test_audit_only_pass_outcome_approves(monkeypatch):
    """Pass still approves on the audit-only path (no regression)."""
    data = _make_task_with_reader_outcome(audit_only=True, outcome="pass")
    task_mod.require_block_cross_state(_approve_block(), data)


def test_non_audit_partial_outcome_rejects(monkeypatch):
    """Regression guard: non-audit docs tasks still REQUIRE pass."""
    data = _make_task_with_reader_outcome(audit_only=False, outcome="partial")
    with pytest.raises(task_mod.GreatMindsError) as exc_info:
        task_mod.require_block_cross_state(_approve_block(), data)
    assert "expected 'pass'" in str(exc_info.value)


def test_non_audit_fail_outcome_rejects(monkeypatch):
    """Regression guard: non-audit fail still blocks approval."""
    data = _make_task_with_reader_outcome(audit_only=False, outcome="fail")
    with pytest.raises(task_mod.GreatMindsError):
        task_mod.require_block_cross_state(_approve_block(), data)


def test_audit_only_garbage_outcome_still_rejects(monkeypatch):
    """Even for audit-only, an unknown outcome value is rejected — the
    relaxation accepts only {pass, partial, fail}, not arbitrary strings."""
    data = _make_task_with_reader_outcome(audit_only=True, outcome="weird")
    with pytest.raises(task_mod.GreatMindsError) as exc_info:
        task_mod.require_block_cross_state(_approve_block(), data)
    assert "audit-only" in str(exc_info.value)
