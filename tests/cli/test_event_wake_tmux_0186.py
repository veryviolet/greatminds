"""Tests for task 0186: chat-mode claude wake via tmux send-keys.

Pre-0186 chat-mode roles (claude PLANNER + MAINTAINER) sat in
``NO_KEYSTROKE_INJECT_ROLES`` so coordd's event-wake dispatcher
silently skipped them — inbox messages to PLANNER / MAINTAINER never
auto-triggered a tick.

0186 adds ``tmux_send_keys_wake(coord, role)`` that reads coord.yaml
for the role's tmux window + session and runs ``tmux send-keys -t
<session>:<window> "check inbox and continue your tick" Enter`` —
identical to operator-typed input. Rate-limited per role per coordd
process so a burst doesn't flood the pane.
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


# ---------- tmux_send_keys_wake mechanics ----------


def test_tmux_send_keys_wake_runs_tmux_command(tmp_path: Path,
                                                 monkeypatch) -> None:
    """Happy path: the helper invokes ``tmux send-keys -t
    session:window "<keys>" Enter`` with the correct args."""
    coord = _make_project(tmp_path)
    # Reset rate-limit state so this test isn't gated by previous nudges.
    coordd_mod._LAST_TMUX_NUDGE.clear()

    calls: list[list[str]] = []

    import subprocess
    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)

    ok = coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")

    assert ok is True
    assert calls, "tmux send-keys was never invoked"
    cmd = calls[0]
    assert cmd[:2] == ["tmux", "send-keys"]
    assert "-t" in cmd
    target = cmd[cmd.index("-t") + 1]
    assert target == "test-session:planner"
    # The literal keystroke is the second-to-last arg, Enter is last.
    assert "check inbox and continue your tick" in cmd
    assert cmd[-1] == "Enter"


def test_tmux_send_keys_wake_rate_limits(tmp_path: Path, monkeypatch) -> None:
    """0186: a burst of N nudges within rate_limit_seconds must
    collapse to ONE tmux send-keys call. Without this, a flurry of
    inbox writes (e.g. PLANNER fanning out info messages) would
    flood the chat pane and the operator gets a wall of 'check
    inbox' lines."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    calls: list[list[str]] = []
    import subprocess
    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)

    # Three rapid-fire nudges to the same role.
    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")

    send_keys_calls = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert len(send_keys_calls) == 1, (
        f"0186: expected 1 nudge after rate-limit; got {len(send_keys_calls)}"
    )


def test_tmux_send_keys_wake_skips_when_role_not_in_coord_yaml(
    tmp_path: Path, monkeypatch,
) -> None:
    """Defensive: role exists in CLI scope but operator didn't
    declare a window in coord.yaml → log + skip; don't crash."""
    coord = _make_project(tmp_path, windows=[
        {"name": "dev", "role": "DEVELOPER", "tool": "codex"},
    ])
    coordd_mod._LAST_TMUX_NUDGE.clear()

    calls: list = []
    import subprocess
    monkeypatch.setattr(
        coordd_mod.subprocess, "run",
        lambda cmd, *a, **kw: calls.append(list(cmd)) or
        subprocess.CompletedProcess(list(cmd), 0, "", ""),
    )

    ok = coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    assert ok is False
    assert not calls


def test_tmux_send_keys_wake_propagates_tmux_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """tmux returns non-zero (session doesn't exist, etc.) → helper
    returns False; the bursty fail doesn't update _LAST_TMUX_NUDGE
    so a recovery retry isn't rate-limited."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    import subprocess
    monkeypatch.setattr(
        coordd_mod.subprocess, "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            list(cmd), 1, "", "no server",
        ),
    )

    ok = coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    assert ok is False
    assert "ARCHITECT-PLANNER" not in coordd_mod._LAST_TMUX_NUDGE, (
        "0186: failed nudges must not update the rate-limit timestamp "
        "(otherwise recovery retries are blocked)"
    )


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
