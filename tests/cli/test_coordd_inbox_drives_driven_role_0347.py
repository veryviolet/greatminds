"""Tests for task 0347: a wake event for a DRIVEN role must re-drive it
(claude -p / codex app-server), not sigint/press_enter an idle pane.

A killed driven codex worker (TECHNICAL-WRITER) left no registry; an
inbox wake event was consumed but the worker was never recreated because
coordd's inbox-scan (Step 2) only used the sigint/press_enter mechanism —
there is no agent in an idle driven pane to wake. 0347 routes BOTH the
queue path and the inbox path through _maybe_drive_driven_role, so a wake
event runs a fresh driven turn (force-fresh session when none exists),
re-registering the worker.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as cd


def _proj(tmp_path, *, writer_mode="driven", writer_tool="codex"):
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "roles": {
            "TECHNICAL-WRITER": {"lifecycle": "driven"},
            "DEVELOPER": {"lifecycle": "driven"},
            "ARCHITECT-PLANNER": {"lifecycle": "interactive"},
            "MAINTAINER": {"lifecycle": "self-loop"},
        },
    }), encoding="utf-8")
    proj = tmp_path / "proj"
    coord = proj / "coordination"
    (coord / ".agent_registry").mkdir(parents=True)
    (proj / "coord.yaml").write_text(yaml.safe_dump({
        "session": "s",
        "windows": [
            {"name": "writer", "role": "TECHNICAL-WRITER",
             "tool": writer_tool, "mode": writer_mode},
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "driven"},
            {"name": "planner", "role": "ARCHITECT-PLANNER",
             "tool": "claude", "mode": "chat"},
        ],
    }), encoding="utf-8")
    return canon, coord


def _located(coord, role):
    doc = cd._read_coord_yaml(coord.parent)
    return doc, cd._window_and_tool_for_role(doc, role)


# ---------- _maybe_drive_driven_role unit ----------


def test_driven_codex_role_is_driven(tmp_path, monkeypatch):
    canon, coord = _proj(tmp_path, writer_tool="codex")
    doc, located = _located(coord, "TECHNICAL-WRITER")
    calls = {}
    monkeypatch.setattr(cd, "_spawn_driven_codex_turn",
                        lambda c, r, bi, pd, v, **kw: (calls.setdefault("codex", r), (True, "ok"))[1])
    res = cd._maybe_drive_driven_role(coord, canon, doc, located,
                                      "TECHNICAL-WRITER", False)
    assert res is True
    assert calls["codex"] == "technical-writer"


def test_driven_generic_headless_role_is_driven(tmp_path, monkeypatch):
    canon, coord = _proj(tmp_path, writer_tool="gemini")
    doc, located = _located(coord, "TECHNICAL-WRITER")
    calls = {}

    def fake_spawn(c, r, tool, argv, v, **kw):
        calls["role"] = r
        calls["tool"] = tool
        calls["argv"] = argv
        return (True, "ok")

    monkeypatch.setattr(cd, "_spawn_driven_headless_turn", fake_spawn)
    res = cd._maybe_drive_driven_role(coord, canon, doc, located,
                                      "TECHNICAL-WRITER", False)
    assert res is True
    assert calls["role"] == "technical-writer"
    assert calls["tool"] == "gemini"
    assert calls["argv"][:4] == ["gemini", "--skip-trust", "--yolo", "-p"]


def test_driven_claude_role_is_driven(tmp_path, monkeypatch):
    canon, coord = _proj(tmp_path)
    doc, located = _located(coord, "DEVELOPER")
    calls = {}

    def fake_spawn(c, r, sid, pane, sname, bf, v, **kw):
        calls["claude"] = r
        calls["force_fresh"] = kw.get("force_fresh")
        return (True, "ok")

    monkeypatch.setattr(cd, "_spawn_driven_turn", fake_spawn)
    res = cd._maybe_drive_driven_role(coord, canon, doc, located,
                                      "DEVELOPER", False)
    assert res is True
    assert calls["claude"] == "developer"
    # no session_id in registry → first turn forced fresh
    assert calls["force_fresh"] is True


def test_non_driven_window_mode_returns_none(tmp_path, monkeypatch):
    # writer window mode is chat → NOT driven (gate requires window-driven)
    canon, coord = _proj(tmp_path, writer_mode="chat")
    doc, located = _located(coord, "TECHNICAL-WRITER")
    monkeypatch.setattr(cd, "_spawn_driven_codex_turn",
                        lambda *a, **k: pytest.fail("must not drive"))
    res = cd._maybe_drive_driven_role(coord, canon, doc, located,
                                      "TECHNICAL-WRITER", False)
    assert res is None  # caller falls back to legacy wake


def test_interactive_role_returns_none(tmp_path, monkeypatch):
    canon, coord = _proj(tmp_path)
    doc, located = _located(coord, "ARCHITECT-PLANNER")
    res = cd._maybe_drive_driven_role(coord, canon, doc, located,
                                      "ARCHITECT-PLANNER", False)
    assert res is None


# ---------- queue path still routes through the shared helper ----------


def test_route_queue_event_drives_driven_codex(tmp_path, monkeypatch):
    canon, coord = _proj(tmp_path, writer_tool="codex")
    # make review_sessions owned by TECHNICAL-WRITER for the test by
    # using the inbox path instead — here assert the helper is invoked
    # via _route_queue_event for a driven owner.
    seen = {}
    monkeypatch.setattr(cd, "_maybe_drive_driven_role",
                        lambda *a, **k: seen.setdefault("called", True) or True)
    # _route_queue_event needs an owner for the queue; patch resolver
    monkeypatch.setattr(cd, "_owning_role_for_queue",
                        lambda canon_dir, q: "TECHNICAL-WRITER")
    monkeypatch.setattr(cd, "_last_journal_actor_for", lambda c, f: None)
    ok = cd._route_queue_event(coord, canon, "review_sessions",
                               "0400.md", verbose=False)
    assert seen.get("called") is True
    assert ok is True
