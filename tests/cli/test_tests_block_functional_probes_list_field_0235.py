"""Tests for task 0235: ``functional_probes`` is a LIST_FIELD.

0228 added functional_probes to the tests-block validator (must be a
non-empty list). cli/task.py's LIST_FIELDS set didn't include
``functional_probes``, so coerce_value stored ``--field
functional_probes=cmd1,cmd2`` as the literal string ``"cmd1,cmd2"``
and the 0228 validator then rejected with «expected list, got str».

0235 adds the entry to LIST_FIELDS. ``tester_observations`` stays
scalar (a single text blob per probe-run).
"""
from __future__ import annotations

import pytest

from greatminds.cli import task as task_mod


def test_functional_probes_in_list_fields() -> None:
    """0235 schema pin: LIST_FIELDS includes 'functional_probes'."""
    assert "functional_probes" in task_mod.LIST_FIELDS


def test_tester_observations_not_in_list_fields() -> None:
    """tester_observations is a single text blob, NOT a list."""
    assert "tester_observations" not in task_mod.LIST_FIELDS


def test_coerce_value_splits_functional_probes_comma() -> None:
    """``--field functional_probes=cmd1,cmd2`` → stored as
    ['cmd1', 'cmd2']. Pin against accidental regression that would
    leave it as a scalar string."""
    out = task_mod.coerce_value("functional_probes",
                                  "curl /api, psql -c 'SELECT 1'")
    assert isinstance(out, list)
    # The CLI splits on comma (with trim).
    assert "curl /api" in out
    assert any("psql -c" in s for s in out)


def test_coerce_value_keeps_single_probe_as_one_element_list() -> None:
    """One probe without commas → single-element list, NOT a scalar
    (the validator's list check would otherwise fail this case
    even though it's semantically valid)."""
    out = task_mod.coerce_value("functional_probes",
                                  "curl localhost:8080/api/v1/items")
    assert isinstance(out, list)
    assert out == ["curl localhost:8080/api/v1/items"]


def test_validator_accepts_list_from_coerce_value() -> None:
    """End-to-end: coerce_value output for functional_probes flows
    through to the 0228 validator without false rejection."""
    probes = task_mod.coerce_value("functional_probes",
                                    "curl /api,curl /health")
    data = {
        "scope": "backend",
        "blocks": [],
    }
    block = {
        "kind": "tests",
        "functional_probes": probes,
        "stand_evidence": {
            "reproduction_steps": "ssh and curl",
            "observed_without_fix": "404",
            "observed_with_fix": "container UP",
            "tester_observations": "curl /api returned 200 with data",
        },
    }
    # Must not raise (functional_probes is a non-empty list per 0228).
    task_mod._enforce_tests_functional_probes_per_scope(block, data)


def test_validator_rejects_scalar_functional_probes() -> None:
    """Negative pin: a scalar string (the pre-0235 broken case)
    fails the 0228 'not a list / empty list' check."""
    data = {"scope": "backend", "blocks": []}
    block = {
        "kind": "tests",
        "functional_probes": "curl /api,curl /health",  # scalar, not list
        "stand_evidence": {
            "reproduction_steps": "ssh and curl",
            "observed_without_fix": "404",
            "observed_with_fix": "container UP",
            "tester_observations": "curl /api returned 200",
        },
    }
    with pytest.raises(task_mod.GreatMindsError) as exc:
        task_mod._enforce_tests_functional_probes_per_scope(block, data)
    assert "functional_probes" in str(exc.value)
