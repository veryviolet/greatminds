"""Tests for task 0228: TESTER vs STAND-KEEPER role boundary.

Pre-0228 a backend/UI task could mv → feature_review with TESTER's
``tests.stand_evidence.observed_with_fix`` literally copied from
SK's stand_result. SK ended up doing the functional verification;
TESTER rubber-stamped. 0228 enforces at the CLI level:

- backend/UI task → tests block must carry non-empty
  ``functional_probes`` (TESTER's own commands against the
  prepared stand).
- ``stand_evidence.tester_observations`` must be present and
  DISTINCT from latest ``stand_result.observed_with_fix``.
- docs / research scopes are exempt (READER review covers docs;
  audits cover research).
"""
from __future__ import annotations

import pytest
import yaml

from greatminds.cli import task as task_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema pin ----------


def test_schema_tests_block_validation_has_required_for_scopes() -> None:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    cfg = doc.get("tests_block_validation") or {}
    table = cfg.get("required_for_scopes") or {}
    assert "functional_probes" in (table.get("backend") or [])
    assert "stand_evidence.tester_observations" in (
        table.get("backend") or []
    )
    assert "functional_probes" in (table.get("ui") or [])
    assert "docs" in (cfg.get("exempt_scopes") or [])


# ---------- _enforce_tests_functional_probes_per_scope ----------


def _stand_evidence_min() -> dict:
    """Minimal three-field stand_evidence used as the carrier for
    tester_observations across the negative-case tests."""
    return {
        "reproduction_steps": "ssh and run curl",
        "observed_without_fix": "404",
        "observed_with_fix": "container UP at v1.2.10; /health 200",
    }


def test_backend_rejects_missing_functional_probes() -> None:
    """Backend task with no functional_probes → reject."""
    data = {"scope": "backend", "blocks": []}
    block = {
        "kind": "tests",
        "stand_evidence": {
            **_stand_evidence_min(),
            "tester_observations": "curl /api returned [1,2,3]",
        },
    }
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod._enforce_tests_functional_probes_per_scope(block, data)
    assert "functional_probes" in str(exc.value)


def test_backend_rejects_empty_functional_probes_list() -> None:
    data = {"scope": "backend", "blocks": []}
    block = {
        "kind": "tests",
        "functional_probes": [],
        "stand_evidence": {
            **_stand_evidence_min(),
            "tester_observations": "curl /api returned [1,2,3]",
        },
    }
    with pytest.raises(task_mod.GreatMindsError):
        task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_backend_rejects_missing_tester_observations() -> None:
    """functional_probes present, but stand_evidence lacks
    tester_observations → reject."""
    data = {"scope": "backend", "blocks": []}
    block = {
        "kind": "tests",
        "functional_probes": ["curl localhost:8080/api"],
        "stand_evidence": _stand_evidence_min(),  # no tester_observations
    }
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod._enforce_tests_functional_probes_per_scope(block, data)
    assert "tester_observations" in str(exc.value)


def test_backend_rejects_rubber_stamp_verbatim_copy() -> None:
    """0228 rubber-stamp guard: tester_observations literally equal
    to SK's observed_with_fix → reject. The whole point of 0228 is
    to catch this lazy copy-paste."""
    sk_text = "backend UP at v1.2.10; /health 200; DB schema ok"
    data = {
        "scope": "backend",
        "blocks": [
            {"kind": "stand_result",
             "observed_with_fix": sk_text},
        ],
    }
    block = {
        "kind": "tests",
        "functional_probes": ["curl /api"],
        "stand_evidence": {
            **_stand_evidence_min(),
            "tester_observations": sk_text,  # VERBATIM copy
        },
    }
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod._enforce_tests_functional_probes_per_scope(block, data)
    assert "rubber-stamp" in str(exc.value).lower() or "verbatim" in str(exc.value).lower()


def test_backend_accepts_distinct_observations() -> None:
    """Happy path: functional_probes + distinct tester_observations."""
    data = {
        "scope": "backend",
        "blocks": [
            {"kind": "stand_result",
             "observed_with_fix": "container UP at v1.2.10; /health 200"},
        ],
    }
    block = {
        "kind": "tests",
        "functional_probes": ["curl localhost:8080/api/items"],
        "stand_evidence": {
            **_stand_evidence_min(),
            "tester_observations": (
                "curl /api/items returned 3 records as expected"
            ),
        },
    }
    # Should not raise.
    task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_ui_scope_required_same_as_backend() -> None:
    data = {"scope": "ui", "blocks": []}
    block = {"kind": "tests"}
    with pytest.raises(task_mod.GreatMindsError):
        task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_docs_scope_exempt() -> None:
    """docs scope: validator no-ops (READER review covers
    documentation verification)."""
    data = {"scope": "docs", "blocks": []}
    block = {"kind": "tests"}  # no functional_probes, no observations
    # Must not raise.
    task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_research_scope_exempt() -> None:
    data = {"scope": "research", "blocks": []}
    block = {"kind": "tests"}
    task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_unknown_scope_noop() -> None:
    """Scope not in required_for_scopes and not in exempt → no-op
    (forward-compat for future scopes)."""
    data = {"scope": "release", "blocks": []}
    block = {"kind": "tests"}
    task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_no_scope_field_noop() -> None:
    """Task with no scope field → no-op."""
    data = {"blocks": []}
    block = {"kind": "tests"}
    task_mod._enforce_tests_functional_probes_per_scope(block, data)


# ---------- integration: require_block_cross_state calls it ----------


def test_require_block_cross_state_invokes_enforcer() -> None:
    """Pin that the 0228 enforcer fires through the normal
    cross-state path (require_block_cross_state for kind=tests)."""
    data = {"scope": "backend", "blocks": []}
    block = {
        "kind": "tests",
        # 0091-mandatory subfields:
        "stand_evidence": {
            **_stand_evidence_min(),
            "tester_observations": "curl /api returned [1,2,3]",
        },
        # 0228: missing functional_probes
    }
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod.require_block_cross_state(block, data)
    assert "functional_probes" in str(exc.value)
