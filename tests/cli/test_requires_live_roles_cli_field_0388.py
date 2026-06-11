"""Tests for task 0388 (REVIEWER changes_requested follow-up):
``requires_live_roles`` must be settable through the sanctioned
``greatminds task append-block`` CLI.

0388 added the resume/readiness wedge guard. Its opt-in,
``required_live_roles`` (agent.py), activates ONLY on a YAML *list*.
But cli/task.py ``LIST_FIELDS`` did not include
``requires_live_roles``, so ``coerce_value`` stored
``--field requires_live_roles=[ARCHITECT-PLANNER]`` as the literal
string ``"[ARCHITECT-PLANNER]"``. ``required_live_roles()`` then saw a
non-list and returned ``[]`` (fail-open), so neither the wake-check HELD
section nor the feature_blocked resume validator could ever be opted in
via the CLI-only FSM mutation rule — the current 0379 blocked task could
not be guarded.

This file pins: the CLI ``--field`` path produces a *list*, an
``append-block blocked`` appended via the CLI stores a list that
``required_live_roles()`` consumes, and the resume validator then
refuses a wedged opted-in task.
"""
from __future__ import annotations

import os

import yaml

from greatminds.cli import agent as agent_mod
from greatminds.cli import task as task_mod
from greatminds.cli.coordd import REGISTRY_DIR


CODEX_LOGIN_TIMEOUT_PANE = """\
  Welcome to Codex, OpenAI's command-line coding agent

  Sign in with ChatGPT to use your plan, or provide your own API key.

  > 3. Provide your own API key
  Press enter to continue

  Login timed out
"""


# ---------------------------------------------------------------------------
# LIST_FIELDS membership + coerce_value (the exact ``--field`` path)
# ---------------------------------------------------------------------------

def test_requires_live_roles_in_list_fields() -> None:
    """0388 schema pin: LIST_FIELDS includes 'requires_live_roles' so the
    CLI ``--field`` form coerces it to a list, not a string."""
    assert "requires_live_roles" in task_mod.LIST_FIELDS


def test_coerce_value_bracket_list() -> None:
    """``--field requires_live_roles=[ARCHITECT-PLANNER]`` → list, not the
    literal string '[ARCHITECT-PLANNER]' (the REVIEWER-found break)."""
    out = task_mod.coerce_value("requires_live_roles", "[ARCHITECT-PLANNER]")
    assert out == ["ARCHITECT-PLANNER"]


def test_coerce_value_comma_list() -> None:
    out = task_mod.coerce_value(
        "requires_live_roles", "ARCHITECT-PLANNER, DEVELOPER")
    assert out == ["ARCHITECT-PLANNER", "DEVELOPER"]


def test_coerce_value_single_value_is_one_element_list() -> None:
    out = task_mod.coerce_value("requires_live_roles", "ARCHITECT-PLANNER")
    assert out == ["ARCHITECT-PLANNER"]


def test_pre_fix_string_shape_fails_open() -> None:
    """Documents exactly what REVIEWER caught: the OLD scalar-string shape
    (what the CLI produced before this fix) yields no required roles."""
    hdr = {"requires_live_roles": "[ARCHITECT-PLANNER]"}
    assert agent_mod.required_live_roles(hdr, None) == []


def test_coerced_list_activates_required_live_roles() -> None:
    """End-to-end: the CLI-coerced list flows into required_live_roles()
    and IS picked up (the guard activates)."""
    coerced = task_mod.coerce_value("requires_live_roles", "[ARCHITECT-PLANNER]")
    blk = {"kind": "blocked", "requires_live_roles": coerced}
    assert agent_mod.required_live_roles({}, blk) == ["ARCHITECT-PLANNER"]


# ---------------------------------------------------------------------------
# Full CLI path: append-block blocked --field requires_live_roles=[...]
# stores a list, and the resume validator then refuses while wedged.
# ---------------------------------------------------------------------------

def _coord(tmp_path):
    coord = tmp_path / "proj" / "coordination"
    (coord / REGISTRY_DIR).mkdir(parents=True)
    (coord / "feature_blocked").mkdir()
    (coord / "verified").mkdir()
    (coord / "verified" / "0387-fix.yaml").write_text("id: x\n", encoding="utf-8")
    return coord


def _write_reg(coord, role_lower, **fields):
    import json
    payload = {"role": role_lower.upper(), "tool": "codex"}
    payload.update(fields)
    (coord / REGISTRY_DIR / f"{role_lower}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def test_append_block_via_cli_stores_list_and_guard_refuses(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    tid = "0379-campaign"
    (coord / "feature_blocked" / f"{tid}.yaml").write_text(
        yaml.safe_dump({"id": tid, "stream": "product",
                        "queue": "feature_blocked",
                        "kind": "bugfix", "scope": "backend",
                        "reporter": "EXPLORER",
                        "opened_at": "2026-06-11T00:00:00Z",
                        "priority": "high", "title": "campaign",
                        "blocks": []}),
        encoding="utf-8")
    _write_reg(coord, "architect-planner", pid=os.getpid())

    monkeypatch.setattr(task_mod, "find_coord_dir", lambda: coord)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")

    # The blocked block is authored by the current queue owner
    # (feature_blocked → ARCHITECT-REVIEWER). Append it through the same
    # function the CLI command calls, with the ``--field`` string form.
    task_mod.append_block(
        task_id=tid,
        kind="blocked",
        fields=[
            "blocked_by=ARCHITECT-PLANNER",
            "reason=needs live planner for the lifecycle objective",
            "dependencies=[verified/0387-fix.yaml]",
            "resume_to=review_sessions",
            "requires_live_roles=[ARCHITECT-PLANNER]",
        ],
    )

    # Reload from disk and prove the CLI stored a LIST, not a string.
    data = task_mod.load_task(coord / "feature_blocked" / f"{tid}.yaml")
    blk = [b for b in data["blocks"] if b.get("kind") == "blocked"][-1]
    assert blk["requires_live_roles"] == ["ARCHITECT-PLANNER"]
    assert agent_mod.required_live_roles(data, blk) == ["ARCHITECT-PLANNER"]

    # And the resume validator now refuses while the planner is wedged.
    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, r: CODEX_LOGIN_TIMEOUT_PANE if "planner" in r else None)
    err = task_mod._check_all_dependencies_exist(
        data, "feature_blocked", "review_sessions")
    assert err is not None
    assert "wedged" in err
    assert "ARCHITECT-PLANNER" in err
