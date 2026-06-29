"""Tests for the LIVE-DEVELOPER interactive role + sprint-review path.

LIVE-DEVELOPER is USER-paced: it claims from feature_live, leases a
stand and deploys to it during the session, works live with the USER,
and on USER approval hands to feature_review as a sprint task (REVIEWER
no-regression review, outcome approved_sprint; TESTER skipped). It
replaces the old UI-DEVELOPER FAST chat variant. Its pane is `staged`
(launch pre-types the start command; the USER starts the session).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import launch as launch_mod
from greatminds.cli import task as task_mod
from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def _coord_template() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(encoding="utf-8")
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
    assert "VITE_DEV_PORT" in text
    assert "tags: [teardown]" in text
    assert "fuser -k" in text


# ---------- coord.yaml template: staged pane ----------


def test_coord_template_has_staged_live_pane() -> None:
    win = next((w for w in (_coord_template().get("windows") or [])
                if isinstance(w, dict) and w.get("role") == "LIVE-DEVELOPER"),
               None)
    assert win is not None, "coord template missing LIVE-DEVELOPER window"
    assert win["mode"] == "staged"
    assert win["tool"] == "claude"


# ---------- launch: staged pane pre-types but does NOT submit ----------


def _env_setup():
    return launch_mod.gm_env.EnvSetup(env_type=None, activation="", source="(test)")


def test_launch_staged_pane_pretypes_without_enter(tmp_path: Path, monkeypatch):
    """A staged window gets its window created and the start-agent
    command pre-typed, but NOT submitted (no trailing Enter) — the USER
    starts the session."""
    calls: list = []
    import subprocess as _sp
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or _sp.CompletedProcess(
                args=args, returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")))
    cfg = {
        "session": "test",
        "windows": [
            {"name": "maintainer", "role": "MAINTAINER", "tool": "claude",
             "mode": "loop"},
            {"name": "live", "role": "LIVE-DEVELOPER", "tool": "claude",
             "mode": "staged"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    # The live window is created.
    created = {c[c.index("-n") + 1] for c in calls if "-n" in c}
    assert "live" in created
    # The start-agent command for LIVE-DEVELOPER is sent, but the
    # send-keys call carrying it must NOT end with "Enter".
    live_sends = [c for c in calls
                  if c[:2] == ["tmux", "send-keys"]
                  and any(isinstance(a, str) and "start-agent LIVE-DEVELOPER" in a
                          for a in c)]
    assert live_sends, "staged pane must pre-type the start-agent command"
    for c in live_sends:
        assert c[-1] != "Enter", "staged command must NOT be submitted (no Enter)"


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


def test_live_developer_can_author_interactive_ui_implementation() -> None:
    data = {
        "stream": "product",
        "kind": "feature",
        "scope": "ui",
        "blocks": [{"kind": "plan", "interactive": True}],
    }

    assert task_mod.role_for_block_kind(
        "LIVE-DEVELOPER", "implementation", "feature_live", data,
        {"kind": "implementation"}) is None


def test_live_developer_can_author_interactive_backend_implementation() -> None:
    data = {
        "stream": "product",
        "kind": "feature",
        "scope": "backend",
        "blocks": [{"kind": "plan", "interactive": True}],
    }

    assert task_mod.role_for_block_kind(
        "LIVE-DEVELOPER", "implementation", "feature_live", data,
        {"kind": "implementation"}) is None


def test_live_developer_scope_bypass_is_limited_to_interactive_path() -> None:
    data = {
        "stream": "product",
        "kind": "feature",
        "scope": "ui",
        "blocks": [{"kind": "plan", "interactive": False}],
    }

    err = task_mod.role_for_block_kind(
        "LIVE-DEVELOPER", "implementation", "feature_dev", data,
        {"kind": "implementation"})

    assert err is not None
    assert "requires UI-DEVELOPER" in err
