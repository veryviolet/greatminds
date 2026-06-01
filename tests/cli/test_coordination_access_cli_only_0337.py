"""Tests for task 0337 (DOD2): machine-readable CLI-only coordination
access rule, rendered for EVERY role.

Hard rule: access to coordination/ is ONLY via the greatminds CLI; raw
ls/cat/grep/sed/edit on coordination/ is forbidden for ALL roles incl.
ARCHITECT-PLANNER. Raw relative coordination/ paths break from a
per-task worktree cwd; the CLI resolves the project root regardless;
direct task-yaml edits bypass FSM validation.
"""
from __future__ import annotations

import yaml

from greatminds.cli import role_contract as rc
from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


# ---------- canon section ----------


def test_schema_declares_coordination_access_cli_only() -> None:
    ca = _schema().get("coordination_access")
    assert isinstance(ca, dict)
    assert ca.get("cli_only") is True
    assert ca.get("rule") == "coordination_access_via_greatminds_cli_only"
    assert "raw_ls_cat_grep_sed_edit_on_coordination" in (
        ca.get("forbidden") or [])
    surfaces = " ".join(ca.get("surfaces") or [])
    assert "greatminds inbox" in surfaces
    assert "greatminds task" in surfaces
    assert "greatminds stand status" in surfaces


# ---------- every role contract renders the rule ----------


def test_every_role_contract_renders_cli_only_rule() -> None:
    roles = (_schema().get("roles") or {})
    assert roles, "schema must declare roles"
    for role, entry in roles.items():
        text = rc.render_contract(role, entry)
        assert "Coordination access (CLI-only" in text, (
            f"0337: {role} contract missing the CLI-only rule")
        assert "coordination_access_via_greatminds_cli_only" in text, role
        assert "raw_ls_cat_grep_sed_edit_on_coordination" in text, role


def test_planner_contract_also_carries_the_rule() -> None:
    """ARCHITECT-PLANNER (who violated this) is NOT exempt."""
    entry = rc.load_role_contract(find_canon_dir(), "ARCHITECT-PLANNER")
    text = rc.render_contract("ARCHITECT-PLANNER", entry)
    assert "Coordination access (CLI-only" in text
    assert "raw_ls_cat_grep_sed_edit_on_coordination" in text


# ---------- helper + suppression ----------


def test_load_coordination_access_reads_canon() -> None:
    ca = rc.load_coordination_access()
    assert isinstance(ca, dict) and ca.get("cli_only") is True


def test_render_can_suppress_rule_with_explicit_none() -> None:
    entry = rc.load_role_contract(find_canon_dir(), "DEVELOPER")
    text = rc.render_contract("DEVELOPER", entry, coordination_access=None)
    assert "Coordination access (CLI-only" not in text


# ---- iter-2: the rule reaches the AGENT-FACING render-role CLI output ----


def test_render_role_cli_output_carries_rule(tmp_path) -> None:
    """0337 iter-2 (GATE blind spot): the rule must appear in the
    ``greatminds render-role <ROLE>`` OUTPUT — the actual surface
    injected into agent prompts — not only in render_contract (which
    nothing on the agent path calls)."""
    from click.testing import CliRunner
    from greatminds.cli import render_role as rr_mod
    proj = tmp_path / "proj"
    (proj / "coordination").mkdir(parents=True)
    for role in ("DEVELOPER", "ARCHITECT-PLANNER", "EXPLORER"):
        res = CliRunner().invoke(
            rr_mod.render_role, [role, "--project-dir", str(proj)],
            catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Coordination access (CLI-only" in res.output, (
            f"0337: render-role {role} output missing the CLI-only rule")
        assert "raw_ls_cat_grep_sed_edit_on_coordination" in res.output, role
