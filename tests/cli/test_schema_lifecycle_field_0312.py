"""Tests for task 0312 (0311 Phase 1a): per-role ``lifecycle``
field in schema role contracts.

Declares HOW each agent is driven — ``interactive`` (human-paced
chat), ``self-loop`` (wakes itself), or ``driven`` (woken by
coordd on queue + inbox events). Orthogonal to coord.yaml's
``tool``. Phase 2/3 will select launch mechanics as
f(lifecycle, tool); 1a is the declaration only.
"""
from __future__ import annotations

import yaml
from click.testing import CliRunner

from greatminds.cli import role_contract as rc_mod
from greatminds.core.paths import find_canon_dir


VALID_LIFECYCLES = {"interactive", "self-loop", "driven"}

# Per the plan's explicit assignments.
EXPECTED = {
    "ARCHITECT-PLANNER": "interactive",
    "MAINTAINER": "self-loop",
    "DEVELOPER": "driven",
    "UI-DEVELOPER": "driven",
    "TESTER": "driven",
    "READER": "driven",
    "STAND-KEEPER": "driven",
    "ARCHITECT-REVIEWER": "driven",
    "TECHNICAL-WRITER": "driven",
    "EXPLORER": "driven",
}


def _roles() -> dict:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return doc.get("roles") or {}


# ---------- every role has a valid lifecycle ----------


def test_every_role_has_lifecycle_field() -> None:
    """0312: each role in schema.roles must declare ``lifecycle``
    with a value from the valid enum."""
    roles = _roles()
    for name, entry in roles.items():
        assert isinstance(entry, dict)
        lc = entry.get("lifecycle")
        assert lc in VALID_LIFECYCLES, (
            f"0312: role {name!r} has invalid/missing lifecycle "
            f"{lc!r} (must be one of {sorted(VALID_LIFECYCLES)})"
        )


# ---------- plan's explicit assignments ----------


def test_lifecycle_assignments_match_plan() -> None:
    """Pin the 10 plan-specified assignments verbatim."""
    roles = _roles()
    for name, expected_lc in EXPECTED.items():
        assert name in roles, f"0312: schema missing role {name!r}"
        assert roles[name].get("lifecycle") == expected_lc, (
            f"0312: {name} lifecycle should be {expected_lc!r}, "
            f"got {roles[name].get('lifecycle')!r}"
        )


def test_planner_is_interactive() -> None:
    assert _roles()["ARCHITECT-PLANNER"]["lifecycle"] == "interactive"


def test_maintainer_is_self_loop() -> None:
    assert _roles()["MAINTAINER"]["lifecycle"] == "self-loop"


def test_workers_are_driven() -> None:
    roles = _roles()
    for worker in ("DEVELOPER", "UI-DEVELOPER", "TESTER", "READER",
                    "STAND-KEEPER", "ARCHITECT-REVIEWER",
                    "TECHNICAL-WRITER", "EXPLORER"):
        assert roles[worker]["lifecycle"] == "driven", (
            f"0312: worker {worker} must be driven"
        )


# ---------- lifecycle is orthogonal to tool / launch_modes ----------


def test_lifecycle_does_not_replace_launch_modes() -> None:
    """0312 is additive: ``launch_modes`` (and every other existing
    role field) survive. Lifecycle is a NEW orthogonal axis."""
    roles = _roles()
    for name, entry in roles.items():
        # launch_modes was present pre-0312 for every role.
        assert "launch_modes" in entry, (
            f"0312 regression: role {name!r} lost launch_modes"
        )


# ---------- role contract CLI surfaces lifecycle ----------


def test_role_contract_cli_shows_lifecycle() -> None:
    """``greatminds role contract <ROLE>`` must render the
    Lifecycle line so agents read it at tick start."""
    result = CliRunner().invoke(
        rc_mod.role, ["contract", "MAINTAINER"])
    assert result.exit_code == 0, result.output
    assert "Lifecycle: self-loop" in result.output


def test_role_contract_cli_shows_driven_for_developer() -> None:
    result = CliRunner().invoke(
        rc_mod.role, ["contract", "DEVELOPER"])
    assert result.exit_code == 0
    assert "Lifecycle: driven" in result.output
