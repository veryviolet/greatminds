"""Tests for task 0389: a blocked review_session whose objective targets a
REMOTE stand must have its `requires_live_roles` evaluated against THAT
stand's coordination project, not the local one.

0388 holds a resume while a required role is wedged, but it always inspects
the LOCAL coordination project. Review session 0379 targets avatar
`/srv/greatminds-stand`; its required ARCHITECT-PLANNER lives there. With a
healthy LOCAL planner, 0388 reported 0379 READY even while the avatar planner
was auth-wedged.

0389 adds an opt-in `requires_live_roles_context` (a coordination dir or a
project dir) that redirects the live-role check to the target stand:
  * `required_live_roles_context` / `resolve_live_roles_coord` /
    `held_live_roles` / `describe_live_role_hold` helpers (agent.py)
  * wake-check (advisory) and the feature_blocked → resume validator
    (enforcing) both route through `held_live_roles`

Opt-in (no field → unchanged 0388 local behaviour) and CONSERVATIVE on an
explicitly-declared-but-unreachable target (HELD, not silently READY).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli import agent as agent_mod
from greatminds.cli import task as task_mod
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


def _make_coord(root: Path) -> Path:
    coord = root / "coordination"
    (coord / REGISTRY_DIR).mkdir(parents=True)
    return coord


def _write_reg(coord: Path, role_lower: str, **fields) -> None:
    payload = {"role": role_lower.upper(), "tool": "codex"}
    payload.update(fields)
    (coord / REGISTRY_DIR / f"{role_lower}.json").write_text(
        json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# required_live_roles_context (pure)
# ---------------------------------------------------------------------------

def test_context_absent_is_none():
    assert agent_mod.required_live_roles_context({}, None) is None
    assert agent_mod.required_live_roles_context(
        {"id": "x"}, {"kind": "blocked"}) is None


def test_context_from_header():
    hdr = {"requires_live_roles_context": "/srv/greatminds-stand"}
    assert agent_mod.required_live_roles_context(hdr, None) == \
        "/srv/greatminds-stand"


def test_context_blocked_block_overrides_header():
    hdr = {"requires_live_roles_context": "/opt/x"}
    blk = {"kind": "blocked", "requires_live_roles_context": "/srv/y"}
    assert agent_mod.required_live_roles_context(hdr, blk) == "/srv/y"


def test_context_non_str_is_none():
    assert agent_mod.required_live_roles_context(
        {"requires_live_roles_context": ["/srv/x"]}, None) is None


def test_context_blank_is_none():
    assert agent_mod.required_live_roles_context(
        {"requires_live_roles_context": "   "}, None) is None


# ---------------------------------------------------------------------------
# resolve_live_roles_coord (pure)
# ---------------------------------------------------------------------------

def test_resolve_none_context_is_local(tmp_path):
    local = _make_coord(tmp_path / "local")
    coord, err = agent_mod.resolve_live_roles_coord(None, local)
    assert coord == local and err is None


def test_resolve_project_dir_with_coordination(tmp_path):
    remote = _make_coord(tmp_path / "remote")
    local = _make_coord(tmp_path / "local")
    coord, err = agent_mod.resolve_live_roles_coord(
        str(tmp_path / "remote"), local)
    assert coord == remote and err is None


def test_resolve_coordination_dir_directly(tmp_path):
    remote = _make_coord(tmp_path / "remote")
    local = _make_coord(tmp_path / "local")
    coord, err = agent_mod.resolve_live_roles_coord(str(remote), local)
    assert coord == remote and err is None


def test_resolve_existing_dir_without_registry(tmp_path):
    # A coord dir that exists but has no registry yet still resolves (its
    # agents simply read as not-registered downstream).
    bare = tmp_path / "bare" / "coordination"
    bare.mkdir(parents=True)
    local = _make_coord(tmp_path / "local")
    coord, err = agent_mod.resolve_live_roles_coord(str(tmp_path / "bare"), local)
    assert coord == bare and err is None


def test_resolve_unreachable_context_holds(tmp_path):
    local = _make_coord(tmp_path / "local")
    coord, err = agent_mod.resolve_live_roles_coord(
        str(tmp_path / "does-not-exist"), local)
    assert coord is None
    assert err is not None and "not found" in err


# ---------------------------------------------------------------------------
# held_live_roles (composed)
# ---------------------------------------------------------------------------

def test_held_no_roles_never_held(tmp_path):
    local = _make_coord(tmp_path / "local")
    hold = agent_mod.held_live_roles(local, {}, None)
    assert hold.held is False
    assert hold.wedged == [] and hold.context_error is None


def test_held_local_wedged(tmp_path):
    # No context → evaluated locally (0388 behaviour preserved).
    local = _make_coord(tmp_path / "local")
    _write_reg(local, "architect-planner", pid=os.getpid())
    hdr = {"requires_live_roles": ["ARCHITECT-PLANNER"]}
    hold = agent_mod.held_live_roles(
        local, hdr, None,
        pane_texts={"ARCHITECT-PLANNER": CODEX_LOGIN_TIMEOUT_PANE})
    assert hold.held is True
    assert hold.wedged == [("ARCHITECT-PLANNER", "login_timeout")]
    assert hold.context is None


def test_held_remote_wedged_while_local_healthy(tmp_path, monkeypatch):
    # The crux of 0389: local planner healthy, REMOTE planner wedged → HELD.
    local = _make_coord(tmp_path / "local")
    remote = _make_coord(tmp_path / "remote")
    _write_reg(local, "architect-planner", pid=os.getpid())
    _write_reg(remote, "architect-planner", pid=os.getpid())

    def fake_pane(coord, role_lower):
        if "planner" not in role_lower:
            return None
        return (CODEX_LOGIN_TIMEOUT_PANE if "remote" in str(coord)
                else HEALTHY_AGENT_PANE)

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    blk = {"kind": "blocked", "requires_live_roles": ["ARCHITECT-PLANNER"],
           "requires_live_roles_context": str(tmp_path / "remote")}
    hold = agent_mod.held_live_roles(local, {}, blk)
    assert hold.held is True
    assert hold.wedged == [("ARCHITECT-PLANNER", "login_timeout")]
    assert hold.context == str(tmp_path / "remote")


def test_held_remote_healthy_not_held_even_if_local_wedged(tmp_path, monkeypatch):
    # Symmetric: only the REMOTE role matters. Local wedged is irrelevant.
    local = _make_coord(tmp_path / "local")
    remote = _make_coord(tmp_path / "remote")
    _write_reg(local, "architect-planner", pid=os.getpid())
    _write_reg(remote, "architect-planner", pid=os.getpid())

    def fake_pane(coord, role_lower):
        if "planner" not in role_lower:
            return None
        return (HEALTHY_AGENT_PANE if "remote" in str(coord)
                else CODEX_LOGIN_TIMEOUT_PANE)

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    blk = {"kind": "blocked", "requires_live_roles": ["ARCHITECT-PLANNER"],
           "requires_live_roles_context": str(tmp_path / "remote")}
    hold = agent_mod.held_live_roles(local, {}, blk)
    assert hold.held is False


def test_held_unreachable_remote_context_holds_conservatively(tmp_path):
    local = _make_coord(tmp_path / "local")
    _write_reg(local, "architect-planner", pid=os.getpid())
    blk = {"kind": "blocked", "requires_live_roles": ["ARCHITECT-PLANNER"],
           "requires_live_roles_context": str(tmp_path / "gone")}
    hold = agent_mod.held_live_roles(local, {}, blk)
    assert hold.held is True
    assert hold.wedged == []
    assert hold.context_error is not None
    msg = agent_mod.describe_live_role_hold(hold)
    assert "not found" in msg


def test_describe_names_context_role_and_state(tmp_path):
    hold = agent_mod.LiveRoleHold(
        wedged=[("ARCHITECT-PLANNER", "auth_prompt")],
        context="/srv/greatminds-stand", context_error=None)
    msg = agent_mod.describe_live_role_hold(hold)
    assert "/srv/greatminds-stand" in msg
    assert "ARCHITECT-PLANNER" in msg
    assert "auth_prompt" in msg


# ---------------------------------------------------------------------------
# resume validator (enforcing): remote context
# ---------------------------------------------------------------------------

def _blocked_task(deps, *, requires, context=None, on_block=True):
    blk = {"kind": "blocked", "reason": "dep", "dependencies": deps,
           "resume_to": "review_sessions"}
    hdr = {"id": "0379-x", "queue": "feature_blocked", "blocks": [blk]}
    if on_block:
        blk["requires_live_roles"] = requires
        if context is not None:
            blk["requires_live_roles_context"] = context
    else:
        hdr["requires_live_roles"] = requires
        if context is not None:
            hdr["requires_live_roles_context"] = context
    return hdr


def _setup(tmp_path, monkeypatch, *, local_pane, remote_pane):
    local = _make_coord(tmp_path / "local")
    remote = _make_coord(tmp_path / "remote")
    (local / "verified").mkdir()
    (local / "verified" / "0388-fix.yaml").write_text("id: x\n")
    _write_reg(local, "architect-planner", pid=os.getpid())
    _write_reg(remote, "architect-planner", pid=os.getpid())
    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: local)

    def fake_pane(coord, role_lower):
        if "planner" not in role_lower:
            return None
        return remote_pane if "remote" in str(coord) else local_pane

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    return local, remote


def test_validator_refuses_when_remote_wedged_local_healthy(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch,
           local_pane=HEALTHY_AGENT_PANE, remote_pane=CODEX_LOGIN_TIMEOUT_PANE)
    data = _blocked_task(["verified/0388-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"],
                         context=str(tmp_path / "remote"))
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None
    assert "wedged" in err
    assert "ARCHITECT-PLANNER" in err
    assert "login_timeout" in err
    assert str(tmp_path / "remote") in err


def test_validator_allows_when_remote_healthy_local_wedged(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch,
           local_pane=CODEX_LOGIN_TIMEOUT_PANE, remote_pane=HEALTHY_AGENT_PANE)
    data = _blocked_task(["verified/0388-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"],
                         context=str(tmp_path / "remote"))
    assert task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions") is None


def test_validator_refuses_unreachable_remote_context(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch,
           local_pane=HEALTHY_AGENT_PANE, remote_pane=HEALTHY_AGENT_PANE)
    data = _blocked_task(["verified/0388-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"],
                         context=str(tmp_path / "missing-stand"))
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None and "not found" in err


def test_validator_no_context_uses_local(tmp_path, monkeypatch):
    # No context field → 0388 local semantics preserved: local wedged HOLDS.
    _setup(tmp_path, monkeypatch,
           local_pane=CODEX_LOGIN_TIMEOUT_PANE, remote_pane=HEALTHY_AGENT_PANE)
    data = _blocked_task(["verified/0388-fix.yaml"],
                         requires=["ARCHITECT-PLANNER"])
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None and "wedged" in err


def test_validator_missing_dep_still_wins(tmp_path, monkeypatch):
    # The live-role gate runs only AFTER deps are satisfied.
    _setup(tmp_path, monkeypatch,
           local_pane=HEALTHY_AGENT_PANE, remote_pane=CODEX_LOGIN_TIMEOUT_PANE)
    data = _blocked_task(["verified/9999-absent.yaml"],
                         requires=["ARCHITECT-PLANNER"],
                         context=str(tmp_path / "remote"))
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None and "still missing" in err


# ---------------------------------------------------------------------------
# wake-check (advisory): remote context HELD
# ---------------------------------------------------------------------------

def _wc_project(tmp_path):
    project = tmp_path / "local"
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
    (coord / "verified" / "0388-fix.yaml").write_text("id: x\n")
    return project, coord, canon


def _wc_blocked(coord, tid, *, requires, context=None):
    blk = {"kind": "blocked", "dependencies": ["verified/0388-fix.yaml"],
           "resume_to": "review_sessions",
           "requires_live_roles": requires}
    if context is not None:
        blk["requires_live_roles_context"] = context
    hdr = {"id": tid, "queue": "feature_blocked", "blocks": [blk]}
    (coord / "feature_blocked" / f"{tid}.yaml").write_text(
        yaml.safe_dump(hdr), encoding="utf-8")


def _run_wc(project, canon):
    return CliRunner().invoke(
        wake_check, ["--project-dir", str(project), "--canon-dir", str(canon)])


def test_wake_check_holds_when_remote_wedged_local_healthy(tmp_path, monkeypatch):
    project, coord, canon = _wc_project(tmp_path)
    remote = _make_coord(tmp_path / "remote")
    _wc_blocked(coord, "0379-campaign", requires=["ARCHITECT-PLANNER"],
                context=str(tmp_path / "remote"))
    _write_reg(coord, "architect-planner", pid=os.getpid())
    _write_reg(remote, "architect-planner", pid=os.getpid())

    def fake_pane(c, role_lower):
        if "planner" not in role_lower:
            return None
        return (CODEX_LOGIN_TIMEOUT_PANE if "remote" in str(c)
                else HEALTHY_AGENT_PANE)

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    res = _run_wc(project, canon)
    assert res.exit_code == 0, res.output
    assert "HELD" in res.output
    assert "0379-campaign" in res.output
    assert "login_timeout" in res.output
    assert "READY TO WAKE" not in res.output


def test_wake_check_ready_when_remote_healthy(tmp_path, monkeypatch):
    project, coord, canon = _wc_project(tmp_path)
    remote = _make_coord(tmp_path / "remote")
    _wc_blocked(coord, "0379-campaign", requires=["ARCHITECT-PLANNER"],
                context=str(tmp_path / "remote"))
    _write_reg(coord, "architect-planner", pid=os.getpid())
    _write_reg(remote, "architect-planner", pid=os.getpid())

    def fake_pane(c, role_lower):
        if "planner" not in role_lower:
            return None
        # local wedged, remote healthy → only remote matters → READY
        return (HEALTHY_AGENT_PANE if "remote" in str(c)
                else CODEX_LOGIN_TIMEOUT_PANE)

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    res = _run_wc(project, canon)
    assert res.exit_code == 0, res.output
    assert "READY TO WAKE" in res.output
    assert "0379-campaign" in res.output
    assert "HELD" not in res.output


def test_wake_check_holds_unreachable_remote_context(tmp_path, monkeypatch):
    project, coord, canon = _wc_project(tmp_path)
    _wc_blocked(coord, "0379-campaign", requires=["ARCHITECT-PLANNER"],
                context=str(tmp_path / "no-such-stand"))
    _write_reg(coord, "architect-planner", pid=os.getpid())
    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda c, r: HEALTHY_AGENT_PANE if "planner" in r else None)
    res = _run_wc(project, canon)
    assert res.exit_code == 0, res.output
    assert "HELD" in res.output
    assert "0379-campaign" in res.output
    assert "not found" in res.output
    assert "READY TO WAKE" not in res.output


# ---------------------------------------------------------------------------
# CLI mutation can set the remote context field through the sanctioned path
# ---------------------------------------------------------------------------

def test_cli_append_block_sets_remote_context_field(tmp_path, monkeypatch):
    # The field is a plain string: `--field requires_live_roles_context=<path>`
    # must round-trip as that string (not fragmented, not a list).
    val = agent_mod.required_live_roles_context(
        {}, {"requires_live_roles_context":
             task_mod.coerce_value(
                 "requires_live_roles_context", "/srv/greatminds-stand")})
    assert val == "/srv/greatminds-stand"
    # requires_live_roles still coerces to a real list (0388).
    assert task_mod.coerce_value(
        "requires_live_roles", "[ARCHITECT-PLANNER]") == ["ARCHITECT-PLANNER"]
