"""PLANNER role contract covers stand-profile coordination."""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


def _planner_contract() -> dict:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return doc["roles"]["ARCHITECT-PLANNER"]


# ---------- responsibilities ----------


def test_planner_responsibilities_include_stand_profile_coordination() -> None:
    """The PLANNER contract must list stand-profile coordination as
    an explicit duty so the agent reads it at tick start."""
    resp = _planner_contract().get("responsibilities") or []
    text = " ".join(resp)
    assert "stand_profile" in text or "stand-profile" in text, (
        f"0297: PLANNER responsibilities must mention stand "
        f"profile coordination (got {resp!r})"
    )


def test_planner_responsibilities_include_schema_extension_duty() -> None:
    """When a lease is blocked by registry data, PLANNER files that task."""
    resp = _planner_contract().get("responsibilities") or []
    found = any("registry" in r or "unregistered_profile" in r for r in resp)
    assert found, (
        f"0297: PLANNER responsibilities must include "
        f"profile-registry duty (got {resp!r})"
    )


# ---------- event_triggers ----------


def test_planner_event_triggers_yaml_down_reason() -> None:
    """A stand_down with a YAML-playbook error → PLANNER files a
    bugfix targeting ``coordination/stand-profiles/<name>.yaml``."""
    triggers = _planner_contract().get("event_triggers") or {}
    yaml_event = triggers.get("on_stand_down_yaml_playbook_error")
    assert isinstance(yaml_event, list) and yaml_event
    steps = " ".join(yaml_event)
    assert "yaml" in steps.lower() or "playbook" in steps.lower()
    assert "developer" in steps.lower()


def test_planner_event_triggers_unregistered_profile() -> None:
    """A lease blocked by missing registry entry goes to PLANNER."""
    triggers = _planner_contract().get("event_triggers") or {}
    event = triggers.get("on_stand_unregistered_profile")
    assert isinstance(event, list) and event
    steps = " ".join(event)
    assert "registry" in steps
    assert "used_for" in steps
    assert "default_for" in steps


def test_planner_event_triggers_preserve_intake_flow() -> None:
    """Stand-profile events must not displace intake/plan triggers."""
    triggers = _planner_contract().get("event_triggers") or {}
    for event in ("on_user_feedback_claim", "on_feature_inbox_claim",
                  "on_feature_plan_claim"):
        assert event in triggers
