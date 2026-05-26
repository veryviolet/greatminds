"""Tests for task 0186: chat-mode claude wake.

Pre-0186 chat-mode roles (claude PLANNER + MAINTAINER) sat in
``NO_KEYSTROKE_INJECT_ROLES`` so coordd's event-wake dispatcher
silently skipped them — inbox messages to PLANNER / MAINTAINER never
auto-triggered a tick.

0186 added the ``tmux_send_keys`` mechanism dispatch in
schema.event_wake.by_tool; 0259 rewires the call to
``press_enter`` (input_sock Channel 1 with tmux send-keys as
fallback) — the proven path used by restart.py and the
stalled-sweep. The schema label ``tmux_send_keys`` is retained as
the dispatch key for chat-mode panes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as coordd_mod


def _make_project(tmp_path: Path, windows: list[dict] | None = None) -> Path:
    project = tmp_path / "project"
    coord = project / "coordination"
    coord.mkdir(parents=True)
    cfg = {
        "session": "test-session",
        "project_dir": str(project),
        "windows": windows if windows is not None else [
            {"name": "planner", "role": "ARCHITECT-PLANNER",
             "tool": "claude", "mode": "chat"},
            {"name": "dev", "role": "DEVELOPER",
             "tool": "codex", "mode": "loop"},
        ],
    }
    (project / "coord.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8",
    )
    return coord


# ---------- _read_coord_yaml + _window_and_tool_for_role ----------


def test_read_coord_yaml_returns_dict(tmp_path: Path) -> None:
    coord = _make_project(tmp_path)
    doc = coordd_mod._read_coord_yaml(coord.parent)
    assert doc is not None
    assert doc.get("session") == "test-session"


def test_read_coord_yaml_returns_none_on_missing(tmp_path: Path) -> None:
    project = tmp_path / "no-coord"
    project.mkdir()
    assert coordd_mod._read_coord_yaml(project) is None


def test_window_and_tool_resolves_claude_role(tmp_path: Path) -> None:
    """Happy path: claude chat-mode role resolves to (window, 'claude')."""
    coord = _make_project(tmp_path)
    doc = coordd_mod._read_coord_yaml(coord.parent)
    located = coordd_mod._window_and_tool_for_role(doc, "ARCHITECT-PLANNER")
    assert located == ("planner", "claude")


def test_window_and_tool_resolves_codex_role(tmp_path: Path) -> None:
    coord = _make_project(tmp_path)
    doc = coordd_mod._read_coord_yaml(coord.parent)
    located = coordd_mod._window_and_tool_for_role(doc, "DEVELOPER")
    assert located == ("dev", "codex")


def test_window_and_tool_returns_none_for_unknown_role(tmp_path: Path) -> None:
    coord = _make_project(tmp_path)
    doc = coordd_mod._read_coord_yaml(coord.parent)
    located = coordd_mod._window_and_tool_for_role(doc, "NONEXISTENT-ROLE")
    assert located is None


# ---------- _wake_mechanism_for_tool dispatch ----------


def test_wake_mechanism_claude_is_tmux_send_keys() -> None:
    """0186 contract: claude tool maps to the tmux_send_keys mechanism.
    Pre-0186 chat-mode silently skipped — the missing entry was the
    bug."""
    assert coordd_mod._wake_mechanism_for_tool("claude") == "tmux_send_keys"


def test_wake_mechanism_codex_is_sigint() -> None:
    """0150's existing path stays intact: codex/cursor → sigint."""
    assert coordd_mod._wake_mechanism_for_tool("codex") == "sigint_deepest_descendant"
    assert coordd_mod._wake_mechanism_for_tool("cursor") == "sigint_deepest_descendant"


def test_wake_mechanism_unknown_tool_returns_empty() -> None:
    """An exotic tool (or empty string for role-less windows) returns
    empty — caller uses that as 'deliver-only, no event wake'."""
    assert coordd_mod._wake_mechanism_for_tool("exotic") == ""
    assert coordd_mod._wake_mechanism_for_tool("") == ""


# ---------- chat-mode wake dispatches press_enter (0259) ----------


def _route(coord, queue, filename, verbose=False):
    """Drive the inotify-wake dispatcher (``_route_queue_event``)
    with a canon_dir that resolves the queue's owner to the role we
    want woken. We pass a canon dir with a minimal schema.yaml that
    declares the queue's owner."""
    canon = coord.parent / "canon"
    canon.mkdir(exist_ok=True)
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {
            "feature_dev": {"owner": "ARCHITECT-PLANNER"},
        },
        "event_wake": {
            "by_tool": {
                "claude": "tmux_send_keys",
                "codex": "sigint_deepest_descendant",
            },
        },
    }), encoding="utf-8")
    return coordd_mod._route_queue_event(
        coord, canon, queue, filename, verbose,
    )


def test_route_queue_event_dispatches_press_enter_for_claude(
    tmp_path: Path, monkeypatch,
) -> None:
    """0259: chat-mode wake (mechanism=tmux_send_keys in schema)
    now invokes ``press_enter`` with the claude agent type, the
    target role's session+window, and ``mode='wake', verify=False``.
    This is the production-proven channel — input_sock Channel 1
    with tmux send-keys fallback."""
    coord = _make_project(tmp_path)

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

    ok = _route(coord, "feature_dev", "0001.yaml")
    assert ok is True
    assert len(calls) == 1
    c = calls[0]
    assert c["agent_type"] == "claude"
    assert c["session"] == "test-session"
    assert c["window"] == "planner"
    assert c["role_lower"] == "architect-planner"
    assert c["mode"] == "wake"
    assert c["verify"] is False


def test_route_queue_event_press_enter_failure_propagates(
    tmp_path: Path, monkeypatch,
) -> None:
    """press_enter returns ``(False, diag)`` — dispatcher returns
    False so coordd's downstream signal that 'no wake landed' fires
    (delivery still happened; just no agent kick)."""
    coord = _make_project(tmp_path)

    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: (False, "input_sock missing"),
    )

    ok = _route(coord, "feature_dev", "0001.yaml")
    assert ok is False


def test_press_enter_prefers_input_sock_when_present(
    tmp_path: Path, monkeypatch,
) -> None:
    """0259 verification: with input_sock registered, press_enter
    routes via input_sock Channel 1 (no tmux send-keys writes).
    Pins the wiring promise of the 0259 fix — tmux send-keys is now
    a fallback, not the primary path."""
    from greatminds.cli import _send_enter as se

    # Fake registry with input_sock present + alive pid.
    sock_path = tmp_path / "agent.sock"
    sock_path.write_text("")  # exists, but writes intercepted below
    monkeypatch.setattr(se, "_read_registry",
                        lambda *a, **kw: {"input_sock": str(sock_path),
                                          "pid": None})
    monkeypatch.setattr(se, "_pid_alive", lambda _p: True)

    sock_writes: list[bytes] = []
    def fake_send_via_sock(path, payload):
        sock_writes.append(payload)
        return True
    monkeypatch.setattr(se, "_send_via_input_sock", fake_send_via_sock)

    tmux_writes: list[tuple] = []
    monkeypatch.setattr(
        se, "_send_via_tmux",
        lambda s, w, k: tmux_writes.append((s, w, k)) or True,
    )
    monkeypatch.setattr(se.time, "sleep", lambda _s: None)

    ok, diag = se.press_enter(
        tmp_path, "test-session", "planner", "architect-planner",
        "claude", mode="wake", verify=False,
    )
    assert ok is True
    # input_sock was used; tmux send-keys was NOT.
    assert sock_writes, "input_sock channel must be exercised"
    assert not tmux_writes, (
        f"0259: tmux send-keys must NOT fire when input_sock works "
        f"(got writes: {tmux_writes})"
    )
    assert "input_sock" in diag


# ---------- schema.yaml event_wake section ----------


def test_schema_event_wake_by_tool_present() -> None:
    """0186: schema carries the by_tool dispatch table so operators
    can re-map tool → mechanism without code changes."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    table = (doc.get("event_wake") or {}).get("by_tool")
    assert table is not None, (
        "0186: schema.yaml missing event_wake.by_tool table"
    )
    assert table.get("codex") == "sigint_deepest_descendant"
    assert table.get("cursor") == "sigint_deepest_descendant"
    assert table.get("claude") == "tmux_send_keys"


def test_schema_event_wake_tmux_send_keys_config_present() -> None:
    """The keys + enter + rate_limit_seconds sub-mapping lives under
    event_wake.tmux_send_keys."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    cfg = (doc.get("event_wake") or {}).get("tmux_send_keys")
    assert cfg is not None
    assert cfg.get("keys") == "check inbox and continue your tick"
    assert cfg.get("enter") is True
    assert cfg.get("rate_limit_seconds") == 5
