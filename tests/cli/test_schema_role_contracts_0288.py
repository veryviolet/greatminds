"""Tests for task 0288: schema-driven role contracts for ALL roles.

Pre-0288 role workflow lived in prose ``roles/*.md``; agents had
no machine-readable contract and PLANNER had to dictate steps via
inbox-asks. 0288 codifies per-role ``responsibilities``,
``forbidden_actions``, and ``event_triggers`` in ``schema.yaml`` +
ships a ``greatminds role contract <ROLE>`` CLI that renders the
contract for an agent's prompt context at tick start.

Doc shrinkage in roles/*.md is intentionally deferred — schema +
CLI is what 0288 lands.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import role_contract as rc_mod
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


# Roles 0288 declares contracts for. Excludes USER (entry; no actions).
PRODUCT_ROLES = (
    "ARCHITECT-PLANNER",
    "ARCHITECT-REVIEWER",
    "DEVELOPER",
    "UI-DEVELOPER",
    "TECHNICAL-WRITER",
    "TESTER",
    "READER",
    "EXPLORER",
    "STAND-KEEPER",
    "MAINTAINER",
)

ALL_ROLES = PRODUCT_ROLES + ("USER",)


def _schema_roles() -> dict:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return doc.get("roles") or {}


# ---------- schema shape ----------


def test_all_roles_present_in_schema() -> None:
    """Sanity: the 13 roles we expect (12 + USER) all live in schema."""
    roles = _schema_roles()
    for name in ALL_ROLES:
        assert name in roles, f"0288: schema.roles missing {name!r}"


@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_has_responsibilities_list(role: str) -> None:
    """Every role must declare ``responsibilities`` as a non-empty
    list (USER included — even entry/marginal roles benefit from a
    one-liner duty statement)."""
    entry = _schema_roles()[role]
    resp = entry.get("responsibilities")
    assert isinstance(resp, list) and resp, (
        f"0288: schema.roles.{role}.responsibilities must be a "
        f"non-empty list (got {resp!r})"
    )
    for r in resp:
        assert isinstance(r, str) and r.strip()


@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_has_forbidden_actions_list(role: str) -> None:
    """Every role declares forbidden_actions (may be a short list).
    Required so the contract surface is uniform — a missing list
    would let role drift sneak in silently."""
    entry = _schema_roles()[role]
    forb = entry.get("forbidden_actions")
    assert isinstance(forb, list), (
        f"0288: schema.roles.{role}.forbidden_actions must be a list"
    )


@pytest.mark.parametrize("role", PRODUCT_ROLES)
def test_product_role_has_event_triggers(role: str) -> None:
    """Product / service roles must declare ``event_triggers`` —
    these are the action sequences the agent runs on each event.
    USER is exempt (interactive entry role)."""
    entry = _schema_roles()[role]
    triggers = entry.get("event_triggers")
    assert isinstance(triggers, dict) and triggers, (
        f"0288: schema.roles.{role}.event_triggers must be a "
        f"non-empty mapping (got {triggers!r})"
    )
    # Each trigger maps to a non-empty list of step strings.
    for event, steps in triggers.items():
        assert event.startswith("on_"), (
            f"0288: trigger {event!r} on {role} should start with 'on_'"
        )
        assert isinstance(steps, list) and steps
        for s in steps:
            assert isinstance(s, str) and s.strip()


# ---------- specific contracts ----------


def test_tester_contract_pins_lease_probe_chain() -> None:
    """TESTER's lifecycle must include acquire-lease → wait-ready
    → probe → release → mv. This is the contract 0286 et al. enforce
    at runtime — the schema declares it for the agent to read."""
    entry = _schema_roles()["TESTER"]
    steps = entry["event_triggers"]["on_feature_test_claim"]
    joined = " ".join(steps)
    assert "acquire_lease" in joined
    assert "wait_for_state_ready" in joined or "wait_for_state" in joined
    assert "probe_via_ssh" in joined
    assert "release_lease" in joined or "release_lease_with_result" in joined
    assert "mv_to_feature_review" in joined


def test_tester_forbidden_includes_fake_evidence() -> None:
    """0286 hard-learned: TESTER must NEVER fill shape-only
    stand_evidence. Declare it in forbidden_actions."""
    forb = _schema_roles()["TESTER"]["forbidden_actions"]
    assert "fill_fake_stand_evidence" in forb
    assert "deploy_stand" in forb


def test_sk_event_triggers_lease_preparing_executor_chain() -> None:
    """STAND-KEEPER's on_lease_preparing must invoke the executor
    BEFORE stand ready — exactly the 0286 mandatory contract."""
    entry = _schema_roles()["STAND-KEEPER"]
    steps = entry["event_triggers"]["on_lease_preparing"]
    joined = " ".join(steps)
    assert "load_profile" in joined
    assert "dispatch_profile" in joined or "execute_yaml" in joined
    assert "stand_ready" in joined
    assert "stand_down" in joined


def test_sk_forbidden_includes_mv_to_review() -> None:
    """SK must not perform product-pipeline moves; that's TESTER /
    REVIEWER territory."""
    forb = _schema_roles()["STAND-KEEPER"]["forbidden_actions"]
    assert "probe_feature" in forb
    assert "mv_to_feature_review" in forb


def test_developer_forbidden_pins_loop_contract_rules() -> None:
    """The standing /loop contract forbids DEVELOPER claim of
    ui/docs/stand tasks. Declare it in schema so the contract is
    a single source of truth."""
    forb = _schema_roles()["DEVELOPER"]["forbidden_actions"]
    assert "claim_ui_or_docs_or_stand_tasks" in forb
    assert "deploy_stand" in forb


def test_reviewer_event_triggers_gate_check() -> None:
    """REVIEWER's review-claim event must include gate-check."""
    entry = _schema_roles()["ARCHITECT-REVIEWER"]
    steps = entry["event_triggers"]["on_feature_review_claim"]
    assert "run_gate_check" in steps


# ---------- CLI render ----------


def test_role_contract_cli_renders_tester(tmp_path) -> None:
    """``greatminds role contract TESTER`` outputs the structured
    summary with all four sections + the event_triggers detail."""
    runner = CliRunner()
    result = runner.invoke(
        rc_mod.role,
        ["contract", "TESTER"],
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "ROLE: TESTER" in out
    assert "Responsibilities:" in out
    assert "Forbidden actions:" in out
    assert "Event triggers:" in out
    assert "on_feature_test_claim:" in out
    # Step numbering: "1." prefix proves the ordered render.
    assert "  1. acquire_lease_via_stand_lease" in out \
        or "    1. acquire_lease_via_stand_lease" in out


def test_role_contract_cli_rejects_unknown_role() -> None:
    runner = CliRunner()
    result = runner.invoke(
        rc_mod.role,
        ["contract", "GHOST-ROLE"],
    )
    assert result.exit_code != 0
    msg = result.output + (str(result.exception)
                            if result.exception else "")
    assert "GHOST-ROLE" in msg
    assert "Available" in msg


def test_role_list_cli_returns_all_roles() -> None:
    """``greatminds role list`` returns every declared role, one
    per line."""
    runner = CliRunner()
    result = runner.invoke(rc_mod.role, ["list"])
    assert result.exit_code == 0
    lines = set(result.output.strip().splitlines())
    for name in ALL_ROLES:
        assert name in lines


# ---------- load_role_contract library API ----------


def test_load_role_contract_returns_full_entry(tmp_path) -> None:
    entry = rc_mod.load_role_contract(find_canon_dir(), "TESTER")
    assert "responsibilities" in entry
    assert "forbidden_actions" in entry
    assert "event_triggers" in entry


def test_load_role_contract_raises_on_unknown(tmp_path) -> None:
    with pytest.raises(GreatMindsError) as exc:
        rc_mod.load_role_contract(find_canon_dir(), "NONEXISTENT")
    assert "NONEXISTENT" in str(exc.value)


# ---------- main CLI integration ----------


def test_main_cli_registers_role_group() -> None:
    """``greatminds role contract TESTER`` works through the main
    CLI dispatch (not just the standalone group)."""
    proc = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "role", "contract", "TESTER"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ROLE: TESTER" in proc.stdout
