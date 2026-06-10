"""Regression tests for task 0386: COORDINATE §9.1 self-referential
fix-for-self-blocker carve-out in the CLI review-approval validator.

§9.1 (COORDINATE.md) lets ARCHITECT-REVIEWER approve a self-referential
fix-for-self task with honest partial/fail tests evidence when the missing
gate-pass is caused by the exact verification-infrastructure limitation
that THIS task's fix removes (a chicken-and-egg blocker). Per the canon
text the carve-out conditions are:

  * tests block: plan.stand_required is true, lease evidence
    result/test_result in {partial, fail}, and the three stand_evidence
    prose subfields present (reproduction-steps, observed-without-fix,
    observed-with-fix). Canon mandates NO dedicated marker field on the
    tests block — an honest partial/fail TESTER block IS the eligible
    shape, so already-parked tasks (0369/0361, whose chicken-and-egg
    justification lives in the block's notes/tester_observations) qualify
    as-filed.
  * review block: self_referential_carveout: true + non-empty
    carveout_citation. §9.1 makes the REVIEWER the sole authority, and
    only ARCHITECT-REVIEWER may author a review block, so this single
    explicit opt-in is the forge-proof gate.

Two gates must honour it consistently: the append-time approval check
(require_block_cross_state) and the feature_review → verified gate
(_check_gate_for_stand_required). Ordinary partial evidence stays
rejected; approved_sprint is untouched.

Tests directly invoke the validators to avoid full CLI plumbing.
"""
from __future__ import annotations

import pytest

from greatminds.cli.task import (
    require_block_cross_state,
    _check_gate_for_stand_required,
)
from greatminds.core.errors import GreatMindsError


def _plan(stand_required: bool = True) -> dict:
    return {
        "kind": "plan",
        "by": "ARCHITECT-PLANNER",
        "at": "2026-06-10T00:00:00Z",
        "stand_required": stand_required,
        "base_commit": "deadbeef",
        "stand_reason": "see body" if stand_required else "",
        "assignee_role": "DEVELOPER",
        "ready_for_implementation": True,
        "plan_kind": "bugfix",
        "mode": "A",
    }


def _stand_evidence(**overrides) -> dict:
    ev = {
        "reproduction_steps": "lease stand; deploy worktree fix; drive turn",
        "observed_without_fix": "driven turn blocked by the verify limitation",
        "observed_with_fix": "turn advances once THIS fix is deployed",
        "lease_id": "none",
        "result": "partial",
        "commit": "deadbeef",
        "tester_observations": "fix demonstrably removes the limitation",
    }
    ev.update(overrides)
    return ev


def _tests_block(
    *,
    test_result: str = "partial",
    stand_evidence: dict | None = None,
) -> dict:
    """An honest TESTER block. Carries NO dedicated §9.1 marker field —
    this is exactly the shape canon §9.1 makes eligible (the chicken-and-
    egg justification lives in free-text notes the REVIEWER cites)."""
    return {
        "kind": "tests",
        "by": "TESTER",
        "at": "2026-06-10T00:00:00Z",
        "base_commit": "deadbeef",
        "test_files": ["tests/cli/test_self_referential_carveout_0386.py"],
        "test_command": ".venv/bin/pytest -q",
        "test_result": test_result,
        "gate_check_result": "missing",
        "gate_check_at": "2026-06-10T00:00:00Z",
        "gate_check_commit": "deadbeef",
        "ready_for_review": True,
        "functional_probes": ["probe-1"],
        "stand_evidence": _stand_evidence() if stand_evidence is None
        else stand_evidence,
        "notes": "chicken-and-egg: cannot stand-prove green until the fix "
        "removes the verify limitation being fixed",
    }


def _real_0369_shaped_tests_block() -> dict:
    """A faithful copy of the kind of block 0369/0361 carry today: partial
    result, the three stand_evidence subfields, lease_id=none, and the
    chicken-and-egg explanation ONLY in free-text notes/tester_observations
    — no dedicated self_referential field. The REVIEWER caught that the
    earlier 0386 design left this exact shape unapprovable."""
    return {
        "kind": "tests",
        "by": "TESTER",
        "at": "2026-06-10T04:53:01Z",
        "base_commit": "79de657",
        "test_files": ["tests/cli/test_driven_fail_fast_env_18.py",
                       "src/greatminds/cli/coordd.py"],
        "test_command": "PYTHONPATH=<wt>/src .venv/bin/python module probe",
        "test_result": "partial",
        "gate_check_result": "missing",
        "gate_check_at": "2026-06-10T04:53:01Z",
        "gate_check_commit": "79de657",
        "ready_for_review": False,
        "functional_probes": ["_driven_subprocess_env override-wins 120000/2"],
        "stand_evidence": {
            "reproduction_steps": "PYTHONPATH module probe of the 0369 "
            "worktree coordd at rebased commit 79de657.",
            "observed_without_fix": "predecessor coordd returns env with no "
            "API_TIMEOUT_MS / CLAUDE_CODE_MAX_RETRIES pinning; rate-limited "
            "driven turn hangs in-process holding the run-lock (#18).",
            "observed_with_fix": "module constants present; injected env "
            "overrides inherited; worst-case stall well under the backstop.",
            "tester_observations": "GATE CANNOT PASS: the only live stand "
            "path runs the long-running local coordd where the full "
            "behavioral repro is not cleanly reproducible while the fleet "
            "lacks the very fail-fast fix 0369 adds — chicken-and-egg.",
            "lease_id": "none",
            "result": "partial",
            "commit": "79de657",
        },
        "notes": "Honest tests block: stand_required, no fresh true gate-pass "
        "is possible because the live-validatable path is the exact "
        "limitation 0369 removes; result=partial with the three "
        "stand_evidence fields and the chicken-and-egg explanation.",
    }


def _review_block(
    *,
    outcome: str = "approved",
    carveout: bool = True,
    citation: str = "COORDINATE §9.1: tests block documents the "
    "chicken-and-egg verify limitation this fix removes",
) -> dict:
    block: dict = {
        "kind": "review",
        "by": "ARCHITECT-REVIEWER",
        "at": "2026-06-10T00:00:00Z",
        "outcome": outcome,
        "commit": "deadbeef",
    }
    if carveout:
        block["self_referential_carveout"] = True
        block["carveout_citation"] = citation
    return block


def _data(*blocks) -> dict:
    return {"stream": "product", "id": "0386-x", "blocks": list(blocks)}


# ---------------------------------------------------------------------------
# Append-time approval gate (require_block_cross_state)
# ---------------------------------------------------------------------------


def test_self_referential_partial_can_be_approved():
    """Honest partial tests + REVIEWER carve-out citation → approval OK."""
    data = _data(_plan(), _tests_block())
    require_block_cross_state(_review_block(), data)  # must not raise


def test_self_referential_fail_can_be_approved():
    """result=fail is also a valid §9.1 evidence state."""
    data = _data(_plan(), _tests_block(test_result="fail"))
    require_block_cross_state(_review_block(), data)  # must not raise


def test_real_0369_shaped_block_is_approvable_as_filed():
    """The REVIEWER's changes_requested: a real 0369/0361-shaped tests
    block (partial + 3 stand_evidence subfields, NO dedicated marker field,
    chicken-and-egg only in notes) must be approvable as-filed once the
    REVIEWER cites the carve-out — no TESTER re-issue / semantic mismatch."""
    data = _data(_plan(), _real_0369_shaped_tests_block())
    require_block_cross_state(_review_block(), data)  # must not raise


def test_ordinary_partial_still_rejected():
    """Partial evidence WITHOUT a REVIEWER carve-out citation stays refused."""
    data = _data(_plan(), _tests_block())
    with pytest.raises(GreatMindsError, match="expected 'pass'"):
        require_block_cross_state(_review_block(carveout=False), data)


def test_partial_rejected_when_review_omits_carveout_citation():
    """Honest partial tests but review block does NOT cite §9.1."""
    data = _data(_plan(), _real_0369_shaped_tests_block())
    with pytest.raises(GreatMindsError, match="carveout_citation"):
        require_block_cross_state(_review_block(carveout=False), data)


def test_partial_rejected_when_carveout_citation_blank():
    """self_referential_carveout flag set but citation is blank → refused."""
    data = _data(_plan(), _tests_block())
    with pytest.raises(GreatMindsError, match="expected 'pass'"):
        require_block_cross_state(_review_block(citation="   "), data)


def test_partial_rejected_when_stand_evidence_subfield_missing():
    """A missing stand_evidence prose subfield defeats the carve-out even
    when the review cites it (the tests block isn't honest §9.1 evidence)."""
    ev = _stand_evidence()
    ev["observed_with_fix"] = ""
    data = _data(_plan(), _tests_block(stand_evidence=ev))
    with pytest.raises(GreatMindsError, match="expected 'pass'"):
        require_block_cross_state(_review_block(), data)


def test_carveout_inert_when_stand_not_required():
    """§9.1 only applies to stand_required tasks; citation alone can't bypass
    the ordinary pass requirement for a non-stand task."""
    data = _data(_plan(stand_required=False), _tests_block())
    with pytest.raises(GreatMindsError, match="expected 'pass'"):
        require_block_cross_state(_review_block(), data)


def test_ordinary_pass_still_approves_without_carveout():
    """Normal happy path: test_result=pass approves with no §9.1 citation."""
    data = _data(_plan(), _tests_block(test_result="pass"))
    require_block_cross_state(_review_block(carveout=False), data)  # no raise


# ---------------------------------------------------------------------------
# mv feature_review → verified gate (_check_gate_for_stand_required)
# ---------------------------------------------------------------------------


def test_gate_allows_self_referential_carveout():
    """The verified gate passes when honest partial tests + a carve-out
    citing approved review are present."""
    data = _data(_plan(), _tests_block(), _review_block())
    assert _check_gate_for_stand_required(
        data, "feature_review", "verified") is None


def test_gate_allows_real_0369_shaped_carveout():
    """End-to-end for the parked-task case: a 0369-shaped partial block +
    carve-out-citing approved review clears the verified gate."""
    data = _data(_plan(), _real_0369_shaped_tests_block(), _review_block())
    assert _check_gate_for_stand_required(
        data, "feature_review", "verified") is None


def test_gate_rejects_ordinary_partial_without_carveout():
    """Without a carve-out citation the gate still demands a real pass."""
    data = _data(
        _plan(),
        _tests_block(),
        _review_block(carveout=False),
    )
    msg = _check_gate_for_stand_required(data, "feature_review", "verified")
    assert msg is not None
    assert "gate_check_pass_if_stand_required" in msg


def test_gate_rejects_carveout_review_over_ineligible_tests():
    """Review cites the carve-out but the tests block is missing a
    stand_evidence subfield, so it doesn't qualify → gate refuses."""
    ev = _stand_evidence()
    ev["reproduction_steps"] = ""
    data = _data(
        _plan(),
        _tests_block(stand_evidence=ev),
        _review_block(),
    )
    msg = _check_gate_for_stand_required(data, "feature_review", "verified")
    assert msg is not None


def test_gate_approved_sprint_unchanged():
    """approved_sprint stays a no-gate path — carve-out logic doesn't touch it."""
    data = _data(_plan(), _review_block(outcome="approved_sprint", carveout=False))
    assert _check_gate_for_stand_required(
        data, "feature_review", "verified") is None
