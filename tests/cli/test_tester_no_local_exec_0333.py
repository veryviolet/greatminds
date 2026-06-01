"""Tests for task 0333: canon forbids TESTER local execution
(no `uv run`/`--active`) — the root cause of recurring .venv-coord
editable contamination (fleet-wide ModuleNotFoundError: greatminds).

TESTER ran `uv run --active` from inside a per-task worktree; `--active`
synced the worktree project into the ACTIVE fleet venv (.venv-coord),
writing ``_editable_impl_greatminds.pth -> .worktrees/<id>/src``. When
the worktree was pruned on merge the .pth dangled → every agent died at
import. schema already forbade local exec; COORDINATE.md §12.5 lumped
TESTER with implementers and said "testing", inviting it.
"""
from __future__ import annotations

import yaml

from greatminds.cli import role_contract as rc
from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


# ---------- schema: TESTER forbids local execution ----------


def test_tester_forbids_local_tests_and_uv_run() -> None:
    forb = set(_schema()["roles"]["TESTER"].get("forbidden_actions") or [])
    assert "run_local_tests" in forb, (
        "0333: TESTER must forbid local test runs")
    assert "uv_run_or_active_against_fleet_venv" in forb, (
        "0333: TESTER must forbid uv run/--active against the fleet venv")


def test_rendered_tester_contract_shows_no_local_exec() -> None:
    entry = rc.load_role_contract(find_canon_dir(), "TESTER")
    text = rc.render_contract("TESTER", entry)
    assert "run_local_tests" in text
    assert "uv_run_or_active_against_fleet_venv" in text


# ---------- COORDINATE.md §12.5 no longer invites TESTER to test ----------


def _coordinate() -> str:
    return (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")


def test_worktree_section_no_longer_lumps_tester_with_testing() -> None:
    """§12.5's cd-into-worktree line must not name TESTER nor say
    'editing/testing' — that lumping is what licensed the local run."""
    text = _coordinate()
    assert "Implementers + TESTER `cd" not in text
    assert "before editing/testing" not in text


def test_worktree_section_bans_uv_run_active() -> None:
    """§12.5 must explicitly ban uv run/--active (the contamination
    mechanism) and state TESTER probes the deployed stand only."""
    text = _coordinate()
    # The ban is stated.
    assert "uv run --active" in text or "uv run`/`uv run --active`" in text
    assert "--active" in text
    # TESTER's surface is the deployed stand, not local execution.
    section = text.split("## 12.5", 1)[1].split("## 13", 1)[0]
    assert "deployed stand" in section
    assert "does **not** edit or execute in the worktree" in section
