"""Tests for task 0237 + 0259: chat-mode claude wake reliability.

USER pattern: «PLANNER misses messages between turns when USER
silent». 0237's original fix split text + Enter into two ``tmux
send-keys`` calls separated by ``WAKE_GAP_SECONDS`` (claude TUI
classifies single-blast text+CR as paste). 0259's deeper finding:
even the split tmux send-keys path lands the text into the tmux
terminal-emulator buffer rather than claude's input state — so
the prompt-submit handler never fires. 0259 switches the chat-
mode wake path to ``press_enter`` (input_sock Channel 1 writes
raw bytes to the agent's pty; tmux send-keys is the fallback).

This file pins 0259's wiring: the inotify-wake dispatcher MUST
invoke ``press_enter`` for chat-mode (claude) panes — never the
direct ``tmux send-keys`` shortcut that 0186 originally added.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as coordd_mod


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    coord = project / "coordination"
    coord.mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": "planner", "role": "ARCHITECT-PLANNER",
             "tool": "claude", "mode": "chat"},
            {"name": "maintainer", "role": "MAINTAINER",
             "tool": "claude", "mode": "chat"},
        ],
    }), encoding="utf-8")
    return coord


def _canon(coord: Path) -> Path:
    canon = coord.parent / "canon"
    canon.mkdir(exist_ok=True)
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {
            "feature_plan": {"owner": "ARCHITECT-PLANNER"},
            "feature_archive": {"owner": "MAINTAINER"},
        },
        "event_wake": {
            "by_tool": {
                "claude": "tmux_send_keys",
                "codex": "sigint_deepest_descendant",
            },
        },
    }), encoding="utf-8")
    return canon


def test_chat_mode_wake_invokes_press_enter_not_tmux(
    tmp_path: Path, monkeypatch,
) -> None:
    """0259 contract: chat-mode wake (mechanism=tmux_send_keys in
    schema) dispatches through ``press_enter``. The plain ``tmux
    send-keys`` shortcut function (``tmux_send_keys_wake``) is
    gone — pin against any regression that reinstates it."""
    coord = _make_project(tmp_path)
    canon = _canon(coord)

    calls: list[dict] = []
    def fake_press_enter(coord_dir, session, window, role_lower,
                         agent_type, *, mode, verify, **_kw):
        calls.append({
            "session": session, "window": window,
            "role_lower": role_lower, "agent_type": agent_type,
            "mode": mode, "verify": verify,
        })
        return (True, "fake-ok")
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter", fake_press_enter,
    )

    ok = coordd_mod._route_queue_event(
        coord, canon, "feature_plan", "0001.yaml", verbose=False,
    )
    assert ok is True
    assert len(calls) == 1
    c = calls[0]
    assert c["agent_type"] == "claude"
    assert c["mode"] == "wake"
    assert c["verify"] is False
    assert c["session"] == "test-session"
    assert c["window"] == "planner"


def test_chat_mode_wake_resolves_per_role_window(
    tmp_path: Path, monkeypatch,
) -> None:
    """PLANNER → planner window; MAINTAINER → maintainer window.
    Pre-0237 a case-sensitivity / role-name confusion was a
    suspected failure mode — the press_enter wiring must keep
    routing per-role correctly."""
    coord = _make_project(tmp_path)
    canon = _canon(coord)

    targets: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda c, s, w, r, a, **_kw: targets.append((s, w, r)) or
        (True, "ok"),
    )

    coordd_mod._route_queue_event(
        coord, canon, "feature_plan", "0001.yaml", verbose=False,
    )
    coordd_mod._route_queue_event(
        coord, canon, "feature_archive", "0002.yaml", verbose=False,
    )

    assert ("test-session", "planner", "architect-planner") in targets
    assert ("test-session", "maintainer", "maintainer") in targets


def test_chat_mode_wake_no_orphan_tmux_helper(tmp_path: Path) -> None:
    """0259 deletion contract: ``tmux_send_keys_wake`` is gone from
    coordd; the dispatcher no longer has that fast-path. If a
    regression re-adds it, this test fails so the migration is
    visible."""
    assert not hasattr(coordd_mod, "tmux_send_keys_wake"), (
        "0259: ``tmux_send_keys_wake`` was removed in favor of "
        "``press_enter``. Re-introducing it bypasses the input_sock "
        "Channel 1 path and reproduces the 0259 bug."
    )
    assert not hasattr(coordd_mod, "_LAST_TMUX_NUDGE"), (
        "0259: tmux-send-keys-specific rate limiter is gone too; "
        "press_enter has its own rate-limiting/verification."
    )
