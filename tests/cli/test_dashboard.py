"""Tests for the read-only `greatminds dashboard` status table.

Collection is split from rendering, so the render functions are pure
(snapshot dict → string) and unit-testable without a live fleet. A
couple of integration tests build a tmp coordination/ tree to exercise
collect_tasks / collect_stand end-to-end.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from greatminds.cli import dashboard as db


# ---------- _fmt_age ----------


@pytest.mark.parametrize("secs,expected", [
    (None, "—"),
    (0.0, "fresh"),
    (59.9, "fresh"),
    (60, "1m"),
    (905, "15m"),
    (3600, "1h"),
    (86400, "1d"),
    (3 * 86400, "3d"),
])
def test_fmt_age(secs, expected):
    assert db._fmt_age(secs) == expected


# ---------- _agent_doing inference ----------


def _rec(alive, age):
    return {"alive": alive, "heartbeat_age": age}


def test_collect_agents_held_lock_is_running(tmp_path, monkeypatch):
    """A fresh driven run-lock reads as `running` even with no live pid /
    heartbeat; driven turns have no persistent role process."""
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / ".locks" / "driven-tester.lock").write_text("")

    from greatminds.cli import agent as agent_mod
    from greatminds.cli import coordd as cd
    monkeypatch.setattr(db, "_fleet_roster",
                        lambda _y: [{"role": "TESTER", "tool": "claude",
                                     "mode": "driven"}])
    monkeypatch.setattr(agent_mod, "collect_agent_status",
                        lambda _c, _r: {"alive": False, "registered": True,
                                        "heartbeat_age": None, "tool": "claude"})
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")

    rows = db.collect_agents(coord, {"session": "x"}, tmp_path / "canon",
                             {"TESTER": ["0001-verify"]})
    assert rows[0]["state"] == "running"
    assert rows[0]["doing"].startswith("running turn")


def test_collect_agents_stale_lock_is_stuck_with_evidence(tmp_path, monkeypatch):
    """A stale run-lock is not healthy work. Surface it as stuck, including
    pending/no-log evidence, instead of the old misleading `running turn`."""
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    lock = coord / ".locks" / "driven-developer.lock"
    log_path = coord / ".turns" / "developer-20260613T000000Z.log"
    lock.write_text(json.dumps({
        "role": "DEVELOPER",
        "driver": "codex",
        "log_path": str(log_path),
    }))
    old = time.time() - 400
    os.utime(lock, (old, old))
    (coord / ".locks" / "driven-developer.pending").write_text("")

    from greatminds.cli import agent as agent_mod
    from greatminds.cli import coordd as cd
    monkeypatch.setattr(db, "_fleet_roster",
                        lambda _y: [{"role": "DEVELOPER", "tool": "codex",
                                     "mode": "driven"}])
    monkeypatch.setattr(agent_mod, "collect_agent_status",
                        lambda _c, _r: {"alive": False, "registered": True,
                                        "heartbeat_age": None, "tool": "codex"})
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_hang_threshold_seconds", lambda _c: 300.0)

    rows = db.collect_agents(coord, {"session": "x"}, tmp_path / "canon",
                             {"DEVELOPER": ["0396-no-git"]})
    assert rows[0]["state"] == "stuck"
    assert "stuck turn" in rows[0]["doing"]
    assert "0396-no-git" in rows[0]["doing"]
    assert "codex" in rows[0]["doing"]
    assert "log:developer-20260613T000000Z.log" in rows[0]["doing"]
    assert "pending" in rows[0]["doing"]
    assert "no turn log" in rows[0]["doing"]


def test_collect_agents_retry_backoff_is_visible(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / ".locks" / "driven-tester.retry.json").write_text(json.dumps({
        "role": "TESTER",
        "klass": "rate_limit",
        "attempts": 4,
        "next_at_epoch": time.time() + 120,
        "escalated": False,
        "detail": "429 temporarily limiting requests",
    }))

    from greatminds.cli import agent as agent_mod
    from greatminds.cli import coordd as cd
    monkeypatch.setattr(db, "_fleet_roster",
                        lambda _y: [{"role": "TESTER", "tool": "codex",
                                     "mode": "driven"}])
    monkeypatch.setattr(agent_mod, "collect_agent_status",
                        lambda _c, _r: {"alive": False, "registered": True,
                                        "heartbeat_age": None, "tool": "codex"})
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")

    rows = db.collect_agents(coord, {"session": "x"}, tmp_path / "canon", {})
    assert rows[0]["state"] == "backoff"
    assert "backoff" in rows[0]["doing"]
    assert "rate_limit attempt 4" in rows[0]["doing"]
    assert "429 temporarily limiting requests" in rows[0]["doing"]


def test_collect_agents_escalated_retry_is_failed(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / ".locks" / "driven-developer.retry.json").write_text(json.dumps({
        "role": "DEVELOPER",
        "klass": "timeout",
        "attempts": 3,
        "next_at_epoch": 0,
        "escalated": True,
        "detail": "turn timed out",
    }))

    from greatminds.cli import agent as agent_mod
    from greatminds.cli import coordd as cd
    monkeypatch.setattr(db, "_fleet_roster",
                        lambda _y: [{"role": "DEVELOPER", "tool": "claude",
                                     "mode": "driven"}])
    monkeypatch.setattr(agent_mod, "collect_agent_status",
                        lambda _c, _r: {"alive": False, "registered": True,
                                        "heartbeat_age": None, "tool": "claude"})
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")

    rows = db.collect_agents(coord, {"session": "x"}, tmp_path / "canon", {})
    assert rows[0]["state"] == "failed"
    assert "retry stopped" in rows[0]["doing"]
    assert "timeout attempt 3" in rows[0]["doing"]


def test_doing_driven_turn_wins():
    d = db._agent_doing(_rec(True, 5), "driven", "driven",
                        driven_turn=True, fresh_sec=60, claimed=["0042"])
    assert d == "running turn · 0042"


def test_doing_staged_dead_awaits_user():
    d = db._agent_doing(_rec(False, None), "staged", "interactive",
                        driven_turn=False, fresh_sec=60, claimed=[])
    assert d == "awaiting USER start"


def test_doing_dead_is_dash():
    d = db._agent_doing(_rec(False, 1000), "driven", "driven",
                        driven_turn=False, fresh_sec=60, claimed=[])
    assert d == "—"


def test_doing_fresh_heartbeat_is_working():
    d = db._agent_doing(_rec(True, 10), "loop", "self-loop",
                        driven_turn=False, fresh_sec=60, claimed=[])
    assert d == "working"


def test_doing_stale_heartbeat_is_idle_without_task():
    # 1.6.2: an idle role no longer carries its queue's task in DOING
    # (that read as "still on X" after a turn ended).
    d = db._agent_doing(_rec(True, 900), "chat", "interactive",
                        driven_turn=False, fresh_sec=60,
                        claimed=["0042", "0043", "0044"])
    assert d == "idle"


# ---------- render_dashboard (pure) ----------


def _snapshot(agents=None, tasks=None, stand=None):
    return {
        "session": "greatminds-dev",
        "agents": agents if agents is not None else [{
            "role": "ARCHITECT-PLANNER", "tool": "codex",
            "lifecycle": "interactive", "mode": "chat", "alive": True,
            "registered": True, "state": "alive", "heartbeat": "7m",
            "doing": "idle",
        }],
        "tasks": tasks if tasks is not None else [],
        "stand": stand or {
            "state": "free", "holder": "", "lease": "", "task": "",
            "queue_len": 0, "queue_next": "", "last_change_at": "",
            "last_change_by": "", "down_reason": "",
        },
    }


def test_render_has_all_three_sections():
    out = db.render_dashboard(_snapshot(), width=100)
    assert "AGENTS" in out
    assert "TASKS  (active: 0)" in out
    assert "STAND" in out
    assert "no active tasks" in out
    assert "ARCHITECT-PLANNER" in out


def test_collect_driven_logs_reads_latest_tail(tmp_path, monkeypatch):
    coord = tmp_path / "coordination"
    turns = coord / ".turns"
    turns.mkdir(parents=True)
    old = turns / "tester-20260613T010000Z.log"
    new = turns / "tester-20260613T020000Z.log"
    old.write_text("old\n", encoding="utf-8")
    new.write_text("one\ntwo\nthree\n", encoding="utf-8")
    os.utime(old, (time.time() - 200, time.time() - 200))
    os.utime(new, (time.time() - 20, time.time() - 20))
    monkeypatch.setattr(db, "_fleet_roster",
                        lambda _y: [{"role": "TESTER", "tool": "codex",
                                     "mode": "driven"}])

    rows = db.collect_driven_logs(coord, {"session": "x"}, lines_per_role=2)

    assert len(rows) == 1
    assert rows[0]["role"] == "TESTER"
    assert rows[0]["name"] == new.name
    assert rows[0]["lines"] == ["two", "three"]


def test_render_dashboard_includes_driven_logs_when_present():
    snap = _snapshot()
    snap["driven_logs"] = [{
        "role": "TESTER",
        "path": "/tmp/coordination/.turns/tester.log",
        "name": "tester.log",
        "age": "fresh",
        "lines": ["status: ok", "finished"],
    }]

    out = db.render_dashboard(snap, width=120)

    assert "DRIVEN LOGS" in out
    assert "TESTER · tester.log · fresh" in out
    assert "status: ok" in out


def test_render_dashboard_empty_driven_logs_section():
    snap = _snapshot()
    snap["driven_logs"] = []
    out = db.render_dashboard(snap, width=120)
    assert "DRIVEN LOGS" in out
    assert "no driven turn logs" in out


def test_render_no_color_has_no_ansi():
    out = db.render_dashboard(_snapshot(), width=100, color=False)
    assert "\033[" not in out


def test_render_color_emits_ansi():
    out = db.render_dashboard(_snapshot(), width=100, color=True)
    assert "\033[" in out


def test_render_alive_dead_staged_glyphs():
    agents = [
        {"role": "A", "tool": "codex", "lifecycle": "interactive",
         "mode": "chat", "alive": True, "registered": True,
         "state": "alive", "heartbeat": "fresh", "doing": "working"},
        {"role": "B", "tool": "claude", "lifecycle": "interactive",
         "mode": "staged", "alive": False, "registered": True,
         "state": "staged", "heartbeat": "—", "doing": "awaiting USER start"},
        {"role": "C", "tool": "claude", "lifecycle": "self-loop",
         "mode": "loop", "alive": False, "registered": True,
         "state": "dead", "heartbeat": "1d", "doing": "—"},
    ]
    out = db.render_dashboard(_snapshot(agents=agents), width=100)
    assert "● alive" in out
    assert "◌ staged" in out
    assert "○ dead" in out


# ---------- driven STATE / DOING coherence (the dead+running bug) ----------


def test_agent_state_driven_never_dead():
    """A driven role is NEVER 'dead' — idle between turns, running during
    a live turn. This is the fix for the 'dead + running turn' contradiction."""
    rec_dead = {"alive": False, "heartbeat_age": 99999}
    assert db._agent_state(rec_dead, "driven", "driven", running=False) == "idle"
    assert db._agent_state(rec_dead, "driven", "driven", running=True) == "running"
    # interactive/self-loop with a dead pid IS dead (should be persistent).
    assert db._agent_state(rec_dead, "loop", "self-loop", running=False) == "dead"
    assert db._agent_state({"alive": True}, "chat", "interactive", False) == "alive"


def test_render_driven_running_and_idle_are_coherent():
    """Render: a driven role shows 'running' (not dead) during a turn and
    'idle' (not dead) between turns — never 'dead + running turn'."""
    agents = [
        {"role": "DEV", "tool": "claude", "lifecycle": "driven",
         "mode": "driven", "alive": False, "registered": True,
         "state": "running", "heartbeat": "fresh", "doing": "running turn · 0042"},
        {"role": "TESTER", "tool": "claude", "lifecycle": "driven",
         "mode": "driven", "alive": False, "registered": True,
         "state": "idle", "heartbeat": "—", "doing": "—"},
    ]
    out = db.render_dashboard(_snapshot(agents=agents), width=120)
    assert "● running" in out
    assert "◦ idle" in out
    # the contradiction must NOT appear: no driven row says dead.
    assert "○ dead" not in out
    for line in out.splitlines():
        if "running turn" in line:
            assert "dead" not in line, f"dead + running turn contradiction: {line}"


def test_task_num_extracts_leading_number():
    assert db._task_num("0001-verify-full-deploy-stand-from-unify") == "0001"
    assert db._task_num("0042") == "0042"
    assert db._task_num("weird-no-number") == "weird-no-number"


def test_render_tasks_shows_number_not_slug():
    rows = [{"id": "0001-verify-full-deploy-stand-from-unify",
             "queue": "feature_test", "owner": "TESTER", "title": "verify"}]
    out = db.render_dashboard(_snapshot(tasks=rows), width=120)
    assert "0001 " in out
    assert "verify-full-deploy" not in out      # long slug gone from ID col


def test_render_tasks_table():
    tasks = [{"id": "0042", "queue": "feature_test", "owner": "TESTER",
              "title": "fix the thing"}]
    out = db.render_dashboard(_snapshot(tasks=tasks), width=120)
    assert "TASKS  (active: 1)" in out
    assert "0042" in out and "feature_test" in out and "fix the thing" in out


def test_render_stand_with_active_lease():
    st = {"state": "ready", "holder": "TESTER", "lease": "1a8e60fd",
          "task": "0042-foo", "queue_len": 2, "queue_next": "0043-bar",
          "last_change_at": "2026-06-04T08:00:00+00:00",
          "last_change_by": "STAND-KEEPER", "down_reason": ""}
    out = db.render_dashboard(_snapshot(stand=st), width=120)
    assert "ready" in out
    assert "lease 1a8e60fd · TESTER · 0042-foo" in out
    assert "queue: 2 (next 0043-bar)" in out


def test_render_clips_to_width():
    tasks = [{"id": "0042", "queue": "feature_test", "owner": "TESTER",
              "title": "x" * 200}]
    out = db.render_dashboard(_snapshot(tasks=tasks), width=50)
    for line in out.splitlines():
        assert len(line) <= 50


# ---------- collect_* integration (tmp coord tree) ----------


def _mk_coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    coord.mkdir()
    return coord


def test_collect_tasks_reads_active_queues(tmp_path):
    coord = _mk_coord(tmp_path)
    q = coord / "feature_test"
    q.mkdir()
    (q / "0042-fix.yaml").write_text(
        "id: 0042-fix\ntitle: fix the thing\nkind: bugfix\n", encoding="utf-8")
    (q / "processed-skip.yaml").write_text("id: skip\ntitle: no\n",
                                           encoding="utf-8")
    (q / "_TEMPLATE.yaml").write_text("id: t\n", encoding="utf-8")
    rows = db.collect_tasks(coord)
    ids = [r["id"] for r in rows]
    assert "0042-fix" in ids
    assert "skip" not in ids and "t" not in ids
    row = next(r for r in rows if r["id"] == "0042-fix")
    assert row["queue"] == "feature_test"
    assert row["title"] == "fix the thing"
    assert row["owner"] == "TESTER"


def test_collect_stand_missing_file_is_synthetic(tmp_path):
    coord = _mk_coord(tmp_path)
    st = db.collect_stand(coord)
    # read_stand_state returns a synthetic free state when no file exists.
    assert "state" in st
    assert st["queue_len"] == 0


def test_collect_snapshot_shape(tmp_path):
    coord = _mk_coord(tmp_path)
    snap = db.collect_snapshot(coord)
    assert set(snap) >= {"session", "agents", "tasks", "stand"}
    assert isinstance(snap["agents"], list)
    assert isinstance(snap["tasks"], list)


def test_collect_snapshot_can_include_driven_logs(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    (coord / ".turns").mkdir()
    (coord / ".turns" / "tester-20260613T000000Z.log").write_text(
        "hello\n", encoding="utf-8")
    monkeypatch.setattr(db, "_read_coord_yaml_safe",
                        lambda _c: {"session": "x"})
    monkeypatch.setattr(db, "_fleet_roster",
                        lambda _y: [{"role": "TESTER", "tool": "codex",
                                     "mode": "driven"}])

    snap = db.collect_snapshot(coord, include_logs=True, log_lines=1)

    assert "driven_logs" in snap
    assert snap["driven_logs"][0]["lines"] == ["hello"]
