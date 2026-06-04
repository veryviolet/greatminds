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


# ---------- the rule reaches the agent-facing surface (bootstrap.md) ----------


def test_static_bootstrap_carries_cli_only_rule() -> None:
    """The single static system prompt every agent receives must state
    the CLI-only coordination-access rule (the agent also reads
    schema.coordination_access itself)."""
    text = (find_canon_dir() / "bootstrap.md").read_text(encoding="utf-8").lower()
    # 1.5.7 reworded this to scope the rule to MUTATIONS (reading docs like
    # PROJECT.md is explicitly allowed) — match case-insensitively so the
    # clarified phrasing ("through the `greatminds` CLI ONLY") still passes.
    assert "greatminds` cli only" in text or "greatminds cli only" in text, (
        "bootstrap.md must state coordination/ FSM-state access is CLI-only")
    assert "coordination/" in text
