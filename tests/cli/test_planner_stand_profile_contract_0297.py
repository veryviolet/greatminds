"""Tests for task 0297: PLANNER role contract closes the
fresh-install gap on stand-profile coordination.

Pre-0297 a new project's PLANNER had no machine-readable
knowledge of stand profiles — neither responsibilities nor
event_triggers mentioned them. Operators had to manually ping
PLANNER to read the dir structure and figure it out. 0297 ships
the contract in schema (primary source) + a short cross-ref in
the prose canon.
"""
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
    """When a lease is blocked by an enum gap, PLANNER's duty is
    to file the schema-extension task — pinned here so the contract
    is explicit, not implicit."""
    resp = _planner_contract().get("responsibilities") or []
    found = any("schema_extension" in r or "enum" in r for r in resp)
    assert found, (
        f"0297: PLANNER responsibilities must include "
        f"schema-extension duty (got {resp!r})"
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


def test_planner_event_triggers_md_down_reason() -> None:
    """A stand_down with an MD-interpretation error → PLANNER files
    a bugfix targeting the ``.md`` prose."""
    triggers = _planner_contract().get("event_triggers") or {}
    md_event = triggers.get("on_stand_down_md_interpretation_error")
    assert isinstance(md_event, list) and md_event
    steps = " ".join(md_event)
    assert "md" in steps.lower() or "prose" in steps.lower() \
        or "stand_profiles" in steps.lower()


def test_planner_event_triggers_enum_block() -> None:
    """A lease blocked by enum gap → PLANNER files the schema
    extension task BEFORE proceeding."""
    triggers = _planner_contract().get("event_triggers") or {}
    enum_event = triggers.get("on_stand_lease_enum_block")
    assert isinstance(enum_event, list) and enum_event
    steps = " ".join(enum_event)
    assert "schema_extension" in steps or "profiles_allowed" in steps


def test_planner_event_triggers_preserve_legacy() -> None:
    """Regression net: 0297 adds events but doesn't displace the
    legacy intake/plan triggers."""
    triggers = _planner_contract().get("event_triggers") or {}
    for legacy in ("on_user_feedback_claim", "on_feature_inbox_claim",
                   "on_feature_plan_claim"):
        assert legacy in triggers


# PLANNER's stand-profile prose pins moved to schema: the
# coordinate-stand-profiles responsibility + on_stand_lease_enum_block /
# on_stand_down_* event_triggers are in schema.roles.ARCHITECT-PLANNER
# (tested above + by test_schema_role_contracts_0288); profiles_allowed
# is schema.stand; the notification channel is schema.stand_keeper.
# The per-role prose doc is gone (system prompt = static bootstrap.md).
