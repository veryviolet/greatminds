"""Tests for task 0388: a resumed review_session must not silently run
against a stand whose required runtime role is wedged.

0387 made ``agent status`` / ``watchdog`` SURFACE a wedged role (alive
pid but stuck at a codex sign-in / "Login timed out" / folder-trust
prompt). Nothing CONSUMED that signal, so a blocked review_session whose
objective needs a live PLANNER could be unblocked and resumed against the
wedged stand and rediscover the same wedge (the avatar 0379 regression).

0388 wires the 0387 detection into the resume/readiness boundary:
  * ``required_live_roles`` / ``wedged_required_roles`` helpers (agent.py)
  * the feature_blocked → resume validator REFUSES the mv while a required
    role is wedged (enforcing)
  * ``wake-check`` reports such a task as HELD, not READY TO WAKE (advisory)

Opt-in (only tasks that declare ``requires_live_roles``) and fail-open
(unknown / healthy / uninspectable role, or any inspection error, never
holds a resume) — so the guard adds no false blocks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli import agent as agent_mod
from greatminds.cli import task as task_mod
from greatminds.cli import wake_check as wc_mod
from greatminds.cli.wake_check import wake_check
from greatminds.cli.coordd import REGISTRY_DIR


CODEX_LOGIN_TIMEOUT_PANE = """\
  Welcome to Codex, OpenAI's command-line coding agent

  Sign in with ChatGPT to use your plan, or provide your own API key.

  > 3. Provide your own API key
  Press enter to continue

  Login timed out
"""

HEALTHY_AGENT_PANE = """\
> I have read the canon and checked my inbox. Continuing my tick.

esc to interrupt
"""


def _coord(tmp_path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / REGISTRY_DIR).mkdir(parents=True)
    return coord


def _write_reg(coord, role_lower, **fields):
    payload = {"role": role_lower.upper(), "tool": "codex"}
    payload.update(fields)
    (coord / REGISTRY_DIR / f"{role_lower}.json").write_text(
        json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# required_live_roles (pure)
# ---------------------------------------------------------------------------

def test_required_live_roles_absent_is_empty():
    assert agent_mod.required_live_roles({}, None) == []
    assert agent_mod.required_live_roles({"id": "x"}, {"kind": "blocked"}) == []


def test_required_live_roles_from_header():
    hdr = {"requires_live_roles": ["architect-planner", "DEVELOPER"]}
    assert agent_mod.required_live_roles(hdr, None) == [
        "ARCHITECT-PLANNER", "DEVELOPER"]


def test_required_live_roles_blocked_block_overrides_header():
    hdr = {"requires_live_roles": ["DEVELOPER"]}
    blk = {"kind": "blocked", "requires_live_roles": ["ARCHITECT-PLANNER"]}
    assert agent_mod.required_live_roles(hdr, blk) == ["ARCHITECT-PLANNER"]


def test_required_live_roles_normalizes_and_dedups():
    hdr = {"requires_live_roles": ["planner", "PLANNER", " tester ", 5, None]}
    assert agent_mod.required_live_roles(hdr, None) == ["PLANNER", "TESTER"]


def test_required_live_roles_non_list_is_empty():
    assert agent_mod.required_live_roles(
        {"requires_live_roles": "ARCHITECT-PLANNER"}, None) == []


# ---------------------------------------------------------------------------
# wedged_required_roles (injected panes)
# ---------------------------------------------------------------------------

def test_wedged_required_detects_login_timeout(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())
    wedged = agent_mod.wedged_required_roles(
        coord, ["ARCHITECT-PLANNER"],
        pane_texts={"ARCHITECT-PLANNER": CODEX_LOGIN_TIMEOUT_PANE})
    assert wedged == [("ARCHITECT-PLANNER", "login_timeout")]


def test_wedged_required_healthy_role_not_held(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())
    wedged = agent_mod.wedged_required_roles(
        coord, ["ARCHITECT-PLANNER"],
        pane_texts={"ARCHITECT-PLANNER": HEALTHY_AGENT_PANE})
    assert wedged == []


def test_wedged_required_unknown_role_not_held(tmp_path):
    coord = _coord(tmp_path)
    # not registered → usable None → never held (conservative)
    wedged = agent_mod.wedged_required_roles(
        coord, ["ARCHITECT-PLANNER"],
        pane_texts={"ARCHITECT-PLANNER": CODEX_LOGIN_TIMEOUT_PANE})
    assert wedged == []


def test_wedged_required_uninspectable_pane_not_held(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())
    # pane None (can't capture) → usable unknown → not held
    wedged = agent_mod.wedged_required_roles(
        coord, ["ARCHITECT-PLANNER"], pane_texts={"ARCHITECT-PLANNER": None})
    assert wedged == []


def test_wedged_required_fail_open_on_exception(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())

    def boom(*a, **k):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(agent_mod, "collect_agent_status", boom)
    # any inspection error is swallowed — never holds the resume
    assert agent_mod.wedged_required_roles(coord, ["ARCHITECT-PLANNER"]) == []


# ---------------------------------------------------------------------------
# resume validator (enforcing): feature_blocked → resume
# ---------------------------------------------------------------------------

def _blocked_task(deps, *, requires=None, on_block=False):
    blk = {"kind": "blocked", "reason": "dep", "dependencies": deps,
           "resume_to": "review_sessions"}
    hdr = {"id": "0379-x", "queue": "feature_blocked", "blocks": [blk]}
    if requires is not None:
        if on_block:
            blk["requires_live_roles"] = requires
        else:
            hdr["requires_live_roles"] = requires
    return hdr


def _setup_validator(tmp_path, monkeypatch, *, pane):
    coord = _coord(tmp_path)
    (coord / "verified").mkdir()
    (coord / "verified" / "0387-fix.yaml").write_text("id: x\n")
    _write_reg(coord, "architect-planner", pid=os.getpid())
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)

    def fake_pane(_coord, role_lower):
        return pane if "planner" in role_lower else None

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    return coord


def test_validator_refuses_resume_when_required_role_wedged(tmp_path, monkeypatch):
    _setup_validator(tmp_path, monkeypatch, pane=CODEX_LOGIN_TIMEOUT_PANE)
    data = _blocked_task(["verified/0387-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"])
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None
    assert "wedged" in err
    assert "ARCHITECT-PLANNER" in err
    assert "login_timeout" in err


def test_validator_allows_resume_when_required_role_healthy(tmp_path, monkeypatch):
    _setup_validator(tmp_path, monkeypatch, pane=HEALTHY_AGENT_PANE)
    data = _blocked_task(["verified/0387-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"])
    assert task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions") is None


def test_validator_no_field_is_unaffected(tmp_path, monkeypatch):
    # No requires_live_roles → behaves exactly as before even with a wedged
    # planner present (no new holds → no regression for ordinary tasks).
    _setup_validator(tmp_path, monkeypatch, pane=CODEX_LOGIN_TIMEOUT_PANE)
    data = _blocked_task(["verified/0387-fix.yaml"])
    assert task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions") is None


def test_validator_field_on_blocked_block(tmp_path, monkeypatch):
    _setup_validator(tmp_path, monkeypatch, pane=CODEX_LOGIN_TIMEOUT_PANE)
    data = _blocked_task(["verified/0387-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"], on_block=True)
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None and "wedged" in err


def test_validator_missing_dep_still_wins(tmp_path, monkeypatch):
    # The wedge guard runs only AFTER deps are satisfied — a missing dep is
    # still reported first.
    _setup_validator(tmp_path, monkeypatch, pane=CODEX_LOGIN_TIMEOUT_PANE)
    data = _blocked_task(["verified/9999-absent.yaml"],
                         requires=["ARCHITECT-PLANNER"])
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None and "still missing" in err


# ---------------------------------------------------------------------------
# wake-check (advisory): HELD section, excluded from READY TO WAKE
# ---------------------------------------------------------------------------

def _wc_project(tmp_path):
    project = tmp_path / "proj"
    coord = project / "coordination"
    for q in ("feature_blocked", "verified", "review_sessions"):
        (coord / q).mkdir(parents=True)
    (coord / REGISTRY_DIR).mkdir(parents=True)
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {
            "feature_blocked": {"kind": "active"},
            "review_sessions": {"kind": "active"},
            "verified": {"kind": "terminal"},
        }
    }), encoding="utf-8")
    (coord / "verified" / "0387-fix.yaml").write_text("id: x\n")
    return project, coord, canon


def _wc_blocked(coord, tid, *, requires=None):
    blk = {"kind": "blocked", "dependencies": ["verified/0387-fix.yaml"],
           "resume_to": "review_sessions"}
    hdr = {"id": tid, "queue": "feature_blocked", "blocks": [blk]}
    if requires is not None:
        hdr["requires_live_roles"] = requires
    (coord / "feature_blocked" / f"{tid}.yaml").write_text(
        yaml.safe_dump(hdr), encoding="utf-8")


def _run_wc(project, canon):
    return CliRunner().invoke(
        wake_check, ["--project-dir", str(project), "--canon-dir", str(canon)])


def test_wake_check_holds_wedged_review_session(tmp_path, monkeypatch):
    project, coord, canon = _wc_project(tmp_path)
    _wc_blocked(coord, "0379-campaign", requires=["ARCHITECT-PLANNER"])
    _write_reg(coord, "architect-planner", pid=os.getpid())

    def fake_pane(_coord, role_lower):
        return CODEX_LOGIN_TIMEOUT_PANE if "planner" in role_lower else None

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    res = _run_wc(project, canon)
    assert res.exit_code == 0, res.output
    assert "HELD" in res.output
    assert "0379-campaign" in res.output
    assert "login_timeout" in res.output
    # excluded from READY TO WAKE
    assert "READY TO WAKE" not in res.output


def test_wake_check_ready_when_role_healthy(tmp_path, monkeypatch):
    project, coord, canon = _wc_project(tmp_path)
    _wc_blocked(coord, "0379-campaign", requires=["ARCHITECT-PLANNER"])
    _write_reg(coord, "architect-planner", pid=os.getpid())

    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, r: HEALTHY_AGENT_PANE if "planner" in r else None)
    res = _run_wc(project, canon)
    assert res.exit_code == 0, res.output
    assert "READY TO WAKE" in res.output
    assert "0379-campaign" in res.output
    assert "HELD" not in res.output


def test_wake_check_no_field_ready_as_before(tmp_path, monkeypatch):
    project, coord, canon = _wc_project(tmp_path)
    _wc_blocked(coord, "0200-plain")  # no requires_live_roles
    _write_reg(coord, "architect-planner", pid=os.getpid())
    # even with a wedged planner present, a task without the field is READY
    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, r: CODEX_LOGIN_TIMEOUT_PANE if "planner" in r else None)
    res = _run_wc(project, canon)
    assert res.exit_code == 0, res.output
    assert "READY TO WAKE" in res.output
    assert "0200-plain" in res.output
    assert "HELD" not in res.output
