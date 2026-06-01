"""Tests for task 0331 (0311 Phase 5): EXPLORER review-session surface
is machine-readably black-box (CLI/REST) only — no host access.

EXPLORER-found bug: 0329's review-session scenarios (``ssh violet``,
kill coordd/app-server, logout-survival) were unexecutable under the
EXPLORER no-host-probe boundary, so EXPLORER could only black-box
probe and released the lease result=partial. Fix: encode the boundary
in schema (rendered into the role contract) + a scenario-ownership
rule on the review-session author (ARCHITECT-PLANNER) so host-
destructive lifecycle validation routes to STAND-KEEPER, not EXPLORER.
"""
from __future__ import annotations

import yaml

from greatminds.cli import role_contract as rc
from greatminds.core.paths import find_canon_dir


def _schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


# ---------- schema: EXPLORER black-box boundary ----------


def test_explorer_surface_is_black_box_only() -> None:
    explorer = _schema()["roles"]["EXPLORER"]
    assert explorer.get("review_session_surface") == "black_box_cli_rest_only"
    assert explorer.get("host_destructive_validation_owner") == "STAND-KEEPER"


def test_explorer_forbids_host_probe_actions() -> None:
    forb = set(_schema()["roles"]["EXPLORER"].get("forbidden_actions") or [])
    for action in ("ssh_into_stand_hosts", "probe_host_filesystem",
                   "run_docker_or_compose_on_stand",
                   "kill_processes_on_stand",
                   "validate_logout_login_host_survival"):
        assert action in forb, f"0331: EXPLORER must forbid {action}"


def test_planner_owns_black_box_scenario_authoring() -> None:
    """The review-session AUTHOR (ARCHITECT-PLANNER) carries the
    scenario-ownership rule: author EXPLORER sessions black-box only,
    do not emit host-destructive steps to EXPLORER."""
    planner = _schema()["roles"]["ARCHITECT-PLANNER"]
    resp = set(planner.get("responsibilities") or [])
    forb = set(planner.get("forbidden_actions") or [])
    assert "author_explorer_review_sessions_black_box_only" in resp
    assert ("emit_host_destructive_steps_to_explorer_review_sessions"
            in forb)


# ---------- rendered contract surfaces the boundary ----------


def test_rendered_explorer_contract_shows_boundary() -> None:
    """``render-role``/role-contract render of EXPLORER must surface the
    SSH / host-probe forbiddens (they ride forbidden_actions)."""
    canon = find_canon_dir()
    entry = rc.load_role_contract(canon, "EXPLORER")
    text = rc.render_contract("EXPLORER", entry)
    assert "ssh_into_stand_hosts" in text
    assert "kill_processes_on_stand" in text
    assert "probe_only_via_black_box_cli_and_rest" in text


# ---------- review_session template carries no host steps ----------


def test_review_session_template_has_no_host_steps() -> None:
    """The EXPLORER review-session scaffold (the file PLANNER authors
    from) must not instruct ssh/kill/logout host steps."""
    tpl = (find_canon_dir()
           / "templates" / "coordination" / "review_sessions"
           / "_TEMPLATE.md").read_text(encoding="utf-8").lower()
    # The scaffold's scenario placeholders + guidance must be black-box;
    # no literal host-destructive instructions to EXPLORER.
    for bad in ("ssh violet", "docker compose", "kill coordd",
                "logout-survival"):
        assert bad not in tpl, (
            f"0331: review_session template must not emit {bad!r} to EXPLORER")
    # The black-box constraint is stated.
    assert "black-box" in tpl
