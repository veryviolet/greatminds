"""Tests for task 0152: notify-from-journal must suppress self-wake.

Pre-fix, when PLANNER appended a triage block or ran ``greatminds
plan``, the resulting journal lines (actor=ARCHITECT-PLANNER, to_q in
PLANNER's claims_from) caused notify-from-journal to drop a wake-*.md
into PLANNER's own inbox. The stop-decide hook then nudged PLANNER
with stale self-wakes. Fix: filter same-actor → same-target wakes,
while preserving wakes to other interested roles. System events
(actor='') wake all targets — there's no self to subtract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import notify_from_journal as nfj_mod


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """Lay out the minimal coord dir + canon dir for nfj.notify_journal."""
    project = tmp_path / "proj"
    coord = project / "coordination"
    coord.mkdir(parents=True)
    canon = tmp_path / "canon"
    canon.mkdir()
    # Minimal schema covering the queues we touch.
    schema_yaml = {
        "queues": {
            "feature_inbox":  {"owner": "ARCHITECT-PLANNER", "writers": []},
            "feature_plan":   {"owner": "ARCHITECT-PLANNER", "writers": []},
            "feature_dev":    {"owner": "DEVELOPER", "writers": []},
            "feature_review": {"owner": "ARCHITECT-REVIEWER", "writers": []},
            "verified":       {"owner": "ARCHITECT-REVIEWER", "writers": []},
            "stand_done":     {"owner": "STAND-KEEPER", "writers": []},
        },
        "roles": {
            "ARCHITECT-PLANNER":  {"claims_from": ["feature_inbox", "feature_plan"]},
            "DEVELOPER":          {"claims_from": ["feature_dev"]},
            "ARCHITECT-REVIEWER": {"claims_from": ["feature_review"]},
        },
    }
    (canon / "schema.yaml").write_text(
        yaml.safe_dump(schema_yaml), encoding="utf-8",
    )
    return project, canon


def _write_journal(coord: Path, lines: list[dict]) -> None:
    journal = coord / "journal.ndjson"
    journal.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )


def _place_task(coord: Path, queue: str, task_id: str) -> None:
    """GitHub #20: a wake only fires when the task currently RESIDES in
    the destination queue. Tests that expect a wake must place the task
    file there."""
    qdir = coord / queue
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / f"{task_id}.yaml").write_text(
        f"id: {task_id}\n", encoding="utf-8",
    )


def _run_notify(project: Path, canon: Path):
    return CliRunner().invoke(
        nfj_mod.notify_journal,
        ["--project-dir", str(project),
         "--canon-dir", str(canon),
         "--once"],
        catch_exceptions=False,
    )


def _inbox_wakes(coord: Path, role: str) -> list[str]:
    inbox = coord / "inbox" / role.lower()
    if not inbox.is_dir():
        return []
    return sorted(f.name for f in inbox.iterdir()
                  if f.name.startswith("wake-"))


# ---------- self-wake suppression (0152 contract) ----------


def test_planner_action_does_not_wake_planner_itself(tmp_path: Path) -> None:
    """PLANNER's own mv into feature_plan must not drop a wake-*.md
    into PLANNER's inbox. The to_q=feature_plan is owned/claimed by
    PLANNER → without the filter, PLANNER would self-wake."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        "actor": "ARCHITECT-PLANNER",
        "task": "0123-some-task",
        "from": "feature_inbox",
        "to": "feature_plan",
        "reason": "triaged; planning",
        "intent_id": "abc",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "ARCHITECT-PLANNER") == [], (
        "0152: PLANNER's own action must not self-wake"
    )


def test_planner_action_still_wakes_other_interested_roles(tmp_path: Path) -> None:
    """When PLANNER routes a task into DEVELOPER's queue, DEVELOPER
    MUST still receive the wake. The filter is per-actor==per-target,
    not a global suppression."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_dev", "0123-some-task")
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        "actor": "ARCHITECT-PLANNER",
        "task": "0123-some-task",
        "from": "feature_plan",
        "to": "feature_dev",
        "reason": "route scope:backend",
        "intent_id": "def",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "DEVELOPER"), (
        "0152: DEVELOPER must still get the wake when PLANNER routes "
        "a task into feature_dev"
    )
    assert _inbox_wakes(coord, "ARCHITECT-PLANNER") == []


def test_developer_self_action_does_not_self_wake(tmp_path: Path) -> None:
    """Symmetric: DEVELOPER's own append-block in feature_dev (a
    self-mv where from==to==feature_dev) must not self-wake."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        "actor": "DEVELOPER",
        "task": "0123-some-task",
        "from": "feature_dev",
        "to": "feature_dev",
        "reason": "append-block implementation",
        "intent_id": "ghi",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "DEVELOPER") == []


def test_reviewer_archiving_does_not_self_wake_via_verified_hook(tmp_path: Path) -> None:
    """REVIEWER's mv → verified triggers determine_wakeups' verified-
    hook which adds a REVIEWER wake for wake_check. Pre-fix this
    landed in REVIEWER's own inbox even though REVIEWER just acted.
    Filter must catch this case too."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        "actor": "ARCHITECT-REVIEWER",
        "task": "0123-some-task",
        "from": "feature_review",
        "to": "verified",
        "reason": "",
        "intent_id": "jkl",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "ARCHITECT-REVIEWER") == [], (
        "REVIEWER's own mv to verified must not produce a self-wake "
        "via the verified-side-effect branch"
    )


# ---------- system events still wake all targets ----------


def test_empty_actor_wakes_all_targets(tmp_path: Path) -> None:
    """A system event with actor='' (e.g. backfill, daemon-side
    journal rewrite) must wake every legitimate target. There's no
    self to subtract — actor is unknown by design."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_plan", "0123-some-task")
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        "actor": "",  # system event
        "task": "0123-some-task",
        "from": "feature_inbox",
        "to": "feature_plan",
        "reason": "backfill",
        "intent_id": "",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "ARCHITECT-PLANNER"), (
        "0152: actor='' system events must wake all legitimate targets"
    )


def test_missing_actor_key_treated_as_system_event(tmp_path: Path) -> None:
    """Defensive: a journal entry with no ``actor`` key at all (old
    schema, partial write) must NOT crash and must wake all targets
    (treated as system event, same as actor='')."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _place_task(coord, "feature_plan", "0123-some-task")
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        # no "actor" key
        "task": "0123-some-task",
        "from": "feature_inbox",
        "to": "feature_plan",
        "reason": "ancient",
        "intent_id": "",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "ARCHITECT-PLANNER")


# ---------- case-insensitivity guard ----------


def test_actor_case_does_not_evade_self_filter(tmp_path: Path) -> None:
    """If a journal entry writes the actor in mixed case (or some
    legacy producer differs from the canonical role-name casing),
    the filter must still catch the self-wake. Compare uppercase."""
    project, canon = _make_project(tmp_path)
    coord = project / "coordination"
    _write_journal(coord, [{
        "t": "2026-05-25T00:00:00Z",
        "actor": "architect-planner",  # lowercase
        "task": "0123-some-task",
        "from": "feature_inbox",
        "to": "feature_plan",
        "reason": "",
        "intent_id": "",
    }])
    _run_notify(project, canon)
    assert _inbox_wakes(coord, "ARCHITECT-PLANNER") == []
