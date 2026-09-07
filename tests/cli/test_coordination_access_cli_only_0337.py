"""Tests for machine-readable CLI-only runtime
access rule, rendered for EVERY role.

Hard rule: mutation and queue inspection for .greatminds/ runtime state is
ONLY via the greatminds CLI; raw ls/cat/grep/sed/edit is forbidden for ALL
roles incl. ARCHITECT-PLANNER. The CLI resolves the project root regardless
of cwd; direct task-yaml edits bypass FSM validation.
"""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


# ---------- canon section ----------


def test_schema_declares_runtime_access_cli_only() -> None:
    ca = _schema().get("runtime_access")
    assert isinstance(ca, dict)
    assert ca.get("cli_only") is True
    assert ca.get("rule") == "runtime_access_via_greatminds_cli_only"
    assert "raw_ls_cat_grep_sed_edit_on_runtime_state" in (
        ca.get("forbidden") or [])
    surfaces = " ".join(ca.get("surfaces") or [])
    assert "greatminds inbox" in surfaces
    assert "greatminds task" in surfaces
    assert "greatminds stand status" in surfaces
