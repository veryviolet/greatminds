"""Tests for task 0237: verify 0186 coordd tmux-send-keys reaches
chat-mode panes reliably.

USER pattern: «PLANNER misses messages between turns when USER
silent». Root cause investigation: the original 0186 helper sent
text + Enter in ONE ``tmux send-keys`` call. Some TUIs (claude
included) classify single-blast text+CR as a paste and DON'T fire
the prompt-submit handler. The injected text sits in the input box
unsent until the operator manually presses Enter.

0237 fix: split into two ``tmux send-keys`` calls separated by
``WAKE_GAP_SECONDS`` (0.35s). Same pattern codex push_to_role uses
(lines 51–58 of coordd.py).
"""
from __future__ import annotations

import subprocess
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


def test_text_and_enter_are_separate_send_keys_calls(
    tmp_path: Path, monkeypatch,
) -> None:
    """0237 contract: two ``tmux send-keys`` invocations per wake —
    first the text, then (after WAKE_GAP_SECONDS) Enter. Pin
    against accidental regression to the single-blast pattern that
    claude TUI classifies as paste."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    calls: list[list[str]] = []
    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(coordd_mod.time, "sleep", lambda _s: None)

    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    send_keys = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert len(send_keys) == 2
    # First: literal text. Second: Enter.
    assert send_keys[0][-1] == coordd_mod.WAKE_TEXT
    assert send_keys[1][-1] == "Enter"


def test_wake_gap_seconds_present_between_calls(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pin that ``time.sleep(WAKE_GAP_SECONDS)`` fires between the
    two send-keys calls. Without the gap, fast-enough delivery
    re-creates the paste-blast classification."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    sleeps: list[float] = []
    monkeypatch.setattr(coordd_mod.time, "sleep",
                        lambda s: sleeps.append(s))
    monkeypatch.setattr(coordd_mod.subprocess, "run",
                        lambda cmd, *a, **kw: subprocess.CompletedProcess(
                            list(cmd), 0, "", ""))

    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    assert sleeps, "0237: WAKE_GAP_SECONDS sleep was never called"
    assert any(abs(s - coordd_mod.WAKE_GAP_SECONDS) < 1e-6 for s in sleeps), (
        f"0237: expected sleep({coordd_mod.WAKE_GAP_SECONDS}) "
        f"between text and Enter; got sleeps={sleeps}"
    )


def test_enter_call_omitted_when_text_call_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    """Defensive: if the text send-keys fails (tmux session gone,
    no server, etc.), we DON'T fire Enter — would press Enter on
    nothing or, worse, on a leftover paste."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    calls: list[list[str]] = []
    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        # First (text) call fails; second (Enter) call shouldn't happen.
        return subprocess.CompletedProcess(list(cmd), 1, "", "no server")
    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(coordd_mod.time, "sleep", lambda _s: None)

    ok = coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    assert ok is False
    send_keys = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert len(send_keys) == 1, (
        "0237: when text call fails, Enter call must be skipped"
    )


def test_rate_limit_timestamp_not_set_on_text_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pin: a failed text send-keys must NOT update _LAST_TMUX_NUDGE
    (existing 0186 contract). Otherwise a transient failure would
    rate-limit recovery retries within the same window."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    monkeypatch.setattr(coordd_mod.subprocess, "run",
                        lambda cmd, *a, **kw: subprocess.CompletedProcess(
                            list(cmd), 1, "", "no server"))
    monkeypatch.setattr(coordd_mod.time, "sleep", lambda _s: None)

    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    assert "ARCHITECT-PLANNER" not in coordd_mod._LAST_TMUX_NUDGE


def test_rate_limit_timestamp_not_set_on_enter_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """Same pin for the Enter call (second of the two). Failure on
    Enter is rare (text fired OK then session disappeared) but the
    semantic is the same: don't lock out recovery retries."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    call_count = {"n": 0}
    def fake_run(cmd, *_a, **_kw):
        call_count["n"] += 1
        # text succeeds, Enter fails.
        rc = 0 if call_count["n"] == 1 else 1
        return subprocess.CompletedProcess(list(cmd), rc, "", "x")

    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(coordd_mod.time, "sleep", lambda _s: None)

    ok = coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    assert ok is False
    assert "ARCHITECT-PLANNER" not in coordd_mod._LAST_TMUX_NUDGE


def test_per_role_window_resolution_from_coord_yaml(
    tmp_path: Path, monkeypatch,
) -> None:
    """0237 verification step: PLANNER → planner window;
    MAINTAINER → maintainer window. Pre-0237 a case-sensitivity
    or role-name confusion was a suspected failure mode."""
    coord = _make_project(tmp_path)
    coordd_mod._LAST_TMUX_NUDGE.clear()

    calls: list[list[str]] = []
    monkeypatch.setattr(coordd_mod.subprocess, "run",
                        lambda cmd, *a, **kw: (
                            calls.append(list(cmd)) or
                            subprocess.CompletedProcess(list(cmd), 0, "", "")
                        ))
    monkeypatch.setattr(coordd_mod.time, "sleep", lambda _s: None)

    coordd_mod.tmux_send_keys_wake(coord, "ARCHITECT-PLANNER")
    planner_targets = [c[c.index("-t") + 1] for c in calls
                       if "-t" in c]
    assert all(t == "test-session:planner" for t in planner_targets)

    coordd_mod._LAST_TMUX_NUDGE.clear()
    calls.clear()
    coordd_mod.tmux_send_keys_wake(coord, "MAINTAINER")
    maintainer_targets = [c[c.index("-t") + 1] for c in calls
                          if "-t" in c]
    assert all(t == "test-session:maintainer" for t in maintainer_targets)
