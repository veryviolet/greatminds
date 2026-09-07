"""Tests for the LIVE-DEVELOPER interactive role + sprint-review path.

LIVE-DEVELOPER is USER-paced: it claims from feature_live, leases a
stand and deploys to it during the session, works live with the USER,
and on USER approval hands to feature_review as a sprint task (REVIEWER
no-regression review, outcome approved_sprint; TESTER skipped). Its ACP binding can use on-demand scheduling for operator input.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import task as task_mod
from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


# ---------- canon: role + queue ----------


def test_role_is_interactive_claims_feature_live() -> None:
    role = _schema()["roles"]["LIVE-DEVELOPER"]
    assert role["lifecycle"] == "interactive"
    assert role["claims_from"] == ["feature_live"]
    assert "deploy_to_own_leased_stand_during_session" in role["responsibilities"]


def test_feature_live_queue() -> None:
    q = _schema()["queues"]["feature_live"]
    assert q["owner"] == "LIVE-DEVELOPER"
    assert q["kind"] == "active"
    assert "ARCHITECT-PLANNER" in q["writers"]


def test_glossary_defines_role_and_queue() -> None:
    g = _schema()["glossary"]
    assert "LIVE-DEVELOPER" in g["roles"]
    assert "feature_live" in g["queues"]


def test_review_allows_approved_sprint() -> None:
    assert "approved_sprint" in _schema()["block_kinds"]["review"]["allowed_outcomes"]


def test_implementation_block_authored_by_live_developer() -> None:
    assert "LIVE-DEVELOPER" in _schema()["block_kinds"]["implementation"]["authored_by"]


def test_queue_accepts_blocks_feature_live() -> None:
    assert _schema()["queue_accepts_blocks"]["feature_live"] == [
        "implementation", "blocked"]


# ---------- transitions ----------


def _transitions() -> list:
    return _schema()["transitions"]


def test_feature_plan_to_feature_live_transition() -> None:
    t = next((r for r in _transitions()
              if r.get("from") == "feature_plan" and r.get("to") == "feature_live"),
             None)
    assert t is not None, "feature_plan → feature_live transition missing"
    assert t["by"] == "ARCHITECT-PLANNER"
    assert "plan.interactive" in t["requires"]


def test_feature_live_to_feature_review_transition() -> None:
    t = next((r for r in _transitions()
              if r.get("from") == "feature_live" and r.get("to") == "feature_review"),
             None)
    assert t is not None, "feature_live → feature_review transition missing"
    assert t["by"] == "LIVE-DEVELOPER"
    assert "implementation_block" in t["requires"]


# ---------- FAST removed; scenario C is LIVE-DEVELOPER ----------


def test_ui_developer_glossary_drops_fast_variant() -> None:
    assert "FAST" not in _schema()["glossary"]["roles"]["UI-DEVELOPER"]


def test_scenario_c_active_roles_is_live_developer() -> None:
    c = _schema()["scenarios"]["C"]
    assert "LIVE-DEVELOPER" in c["active_roles"]
    assert "UI-DEVELOPER" not in c["active_roles"]
    assert c["stand_profile"] == "vite-dev"


def test_vite_dev_profile_preset_ships() -> None:
    p = find_canon_dir() / "templates" / "stand-profiles" / "vite-dev.yaml"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "npm run dev" in text  # HMR dev server
    assert "vite_port" in text


# ---------- validators ----------


def _plan_data(**plan) -> dict:
    return {"blocks": [{"kind": "plan", **plan}]}


def test_plan_interactive_validator() -> None:
    ok = task_mod._check_plan_interactive(
        _plan_data(interactive=True), "feature_plan", "feature_live")
    assert ok is None
    bad = task_mod._check_plan_interactive(
        _plan_data(interactive=False), "feature_plan", "feature_live")
    assert bad and "plan.interactive" in bad


def test_review_block_approved_accepts_sprint() -> None:
    data = {"blocks": [{"kind": "review", "outcome": "approved_sprint"}]}
    assert task_mod._check_review_block_approved(
        data, "feature_review", "verified") is None


def test_gate_satisfied_by_approved_sprint() -> None:
    """A stand_required sprint task verifies on approved_sprint without
    TESTER gate-check evidence."""
    data = {"blocks": [
        {"kind": "plan", "stand_required": True, "interactive": True},
        {"kind": "implementation"},
        {"kind": "review", "outcome": "approved_sprint"},
    ]}
    assert task_mod._check_gate_for_stand_required(
        data, "feature_review", "verified") is None
