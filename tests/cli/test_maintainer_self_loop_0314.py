"""Tests for task 0314 (0311 Phase 1b): MAINTAINER self-loop
watchdog.

MAINTAINER flips from chat to a periodic /loop health-check that
auto-restarts dead agents / coordd and escalates queue/FSM stalls
to ARCHITECT-PLANNER. It becomes non-user-facing: USER reaches
infra topics through PLANNER.

This task is stand_required (live recovery validation on avatar
is TESTER's job). The Python tests pin the canon shape — the
command_START bootstrap launch mode, the tick body's recovery +
escalation instructions, the coord.yaml template mode, and the
role doc's self-loop / non-user-facing framing.
"""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


def _command_start() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8")
    ) or {}


def _maintainer_entry() -> dict:
    roles = (_command_start().get("roles") or {})
    return roles.get("MAINTAINER") or {}


# ---------- command_START launch mode ----------


def test_maintainer_launch_is_loop() -> None:
    """0314: MAINTAINER's bootstrap ``launch`` flips chat → /loop."""
    entry = _maintainer_entry()
    assert entry.get("launch") == "/loop", (
        f"0314: MAINTAINER launch must be '/loop' (got "
        f"{entry.get('launch')!r})"
    )


def test_maintainer_body_describes_health_check_tick() -> None:
    """The bootstrap body must instruct the periodic health-check
    tick: inbox drain + safe auto-fix + escalation."""
    body = _maintainer_entry().get("body") or ""
    low = body.lower()
    assert "health-check" in low or "health check" in low
    assert "self-loop" in low
    # Safe auto-fix instructions.
    assert "greatminds restart" in body
    assert "daemon restart" in body or "daemon repair" in body
    # Escalation to PLANNER for FSM stalls.
    assert "ARCHITECT-PLANNER" in body


def test_maintainer_body_is_non_user_facing() -> None:
    """0314: bootstrap must declare MAINTAINER non-user-facing —
    USER reaches it through PLANNER."""
    body = _maintainer_entry().get("body") or ""
    assert "non-user-facing" in body.lower() \
        or "NON-USER-FACING" in body
    assert "ARCHITECT-PLANNER" in body


def test_maintainer_body_forbids_self_fixing_fsm() -> None:
    """Phase 1b contract: MAINTAINER does NOT fix queue/FSM stalls
    itself — it escalates to PLANNER."""
    body = _maintainer_entry().get("body") or ""
    low = body.lower()
    assert "escalate" in low
    # Don't-fix-FSM-yourself instruction present.
    assert ("do not fix" in low and ("fsm" in low or "queue" in low)) \
        or "escalate to planner" in low


def test_maintainer_body_mentions_configurable_interval() -> None:
    """Tick cadence is configurable, default 1h, with the
    claude/codex self-wake split documented."""
    body = _maintainer_entry().get("body") or ""
    low = body.lower()
    assert "1h" in low or "default 1h" in low or "cadence" in low
    assert "schedulewakeup" in low or "self-wake" in low


# ---------- coord.yaml template ----------


def test_coord_template_maintainer_mode_is_loop() -> None:
    """0314: the coord.yaml template must set the maintainer window
    to mode=loop so fresh fleets launch it as a self-loop."""
    doc = yaml.safe_load(
        (find_canon_dir() / "coord.yaml.template").read_text(
            encoding="utf-8")
    ) or {}
    windows = doc.get("windows") or []
    maint = next(
        (w for w in windows
         if isinstance(w, dict) and w.get("role") == "MAINTAINER"),
        None,
    )
    assert maint is not None, "coord template missing MAINTAINER window"
    assert maint.get("mode") == "loop", (
        f"0314: maintainer window mode must be 'loop' (got "
        f"{maint.get('mode')!r})"
    )


# ---------- role doc ----------


def test_maintainer_md_declares_self_loop() -> None:
    """MAINTAINER.md must frame the role as a self-loop watchdog,
    not chat-mode."""
    text = (find_canon_dir() / "roles" / "MAINTAINER.md").read_text(
        encoding="utf-8")
    assert "self-loop" in text.lower()
    assert "watchdog" in text.lower()


def test_maintainer_md_says_non_user_facing() -> None:
    text = (find_canon_dir() / "roles" / "MAINTAINER.md").read_text(
        encoding="utf-8")
    assert "non-user-facing" in text.lower()
    assert "ARCHITECT-PLANNER" in text


def test_maintainer_md_no_longer_claims_chat_mode() -> None:
    """Negative pin: the old 'MAINTAINER is chat-mode, not /loop'
    line must be gone (it contradicted the 0314 self-loop model)."""
    text = (find_canon_dir() / "roles" / "MAINTAINER.md").read_text(
        encoding="utf-8")
    assert "chat-mode**, not /loop" not in text, (
        "0314: stale 'chat-mode, not /loop' framing must be removed"
    )


# ---------- consistency with 0312 lifecycle ----------


def test_schema_lifecycle_self_loop_matches_canon() -> None:
    """0314 builds on 0312's lifecycle field — MAINTAINER's
    schema lifecycle must be self-loop, consistent with the
    bootstrap + role doc."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    maint = (doc.get("roles") or {}).get("MAINTAINER") or {}
    assert maint.get("lifecycle") == "self-loop"
