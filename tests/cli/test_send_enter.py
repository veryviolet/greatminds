"""Unit tests for the unified Enter-pressing primitive (task 0051 iter-2).

iter-2 redesign after TESTER's 4-cell avatar matrix: pane-bytes-diff
verify was producing both false negatives (cell 1) and false positives
(cell 4). iter-2 uses heartbeat-mtime polling as the success signal
for ``mode="wake"`` (the user-visible "wake an idle agent" path) and
keeps pane-diff for ``mode="bare-enter"`` (launcher/dialog case where
no role heartbeat exists yet).

External effects (tmux subprocess, unix socket, os.kill, heartbeat
files) are mocked. The live-stand 4-cell matrix is still TESTER's job.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from greatminds.cli import _send_enter as se


@pytest.fixture
def coord_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "coordination"
    (cd / ".agent_registry").mkdir(parents=True)
    return cd


def _write_registry(coord_dir: Path, role_lower: str,
                    *, pid: int = 12345,
                    input_sock: str | None = None,
                    tool: str = "claude") -> Path:
    payload: dict = {
        "role": role_lower.upper(),
        "tool": tool,
        "pid": pid,
        "tty": "/dev/pts/0",
        "started_at": "2026-05-24T00:00:00Z",
    }
    if input_sock:
        payload["input_sock"] = input_sock
    p = coord_dir / ".agent_registry" / f"{role_lower}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _write_heartbeat(coord_dir: Path, role_lower: str, mtime: float) -> Path:
    """Create heartbeat.<role-lower> with a controlled mtime baseline."""
    p = coord_dir / f"heartbeat.{role_lower}"
    p.touch()
    os.utime(p, (mtime, mtime))
    return p


class _FakeTmux:
    """Records subprocess.run calls and serves capture-pane outputs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pane_sequence: list[str] = []
        self._capture_idx = 0
        self.send_keys_rc: int = 0

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        if cmd[:2] == ["tmux", "capture-pane"]:
            if self._capture_idx < len(self.pane_sequence):
                out = self.pane_sequence[self._capture_idx]
            else:
                out = self.pane_sequence[-1] if self.pane_sequence else ""
            self._capture_idx += 1
            return subprocess.CompletedProcess(list(cmd), 0, out, "")
        if cmd[:2] == ["tmux", "send-keys"]:
            return subprocess.CompletedProcess(list(cmd), self.send_keys_rc, "", "")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")


@pytest.fixture
def tmux(monkeypatch) -> _FakeTmux:
    t = _FakeTmux()
    monkeypatch.setattr(se.subprocess, "run", t.run)
    monkeypatch.setattr(se.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(se, "_pid_alive", lambda pid: True)
    return t


# ---------------------------------------------------------------------------
# mode="wake" — heartbeat-mtime polling (the primary path)
# ---------------------------------------------------------------------------


def test_wake_succeeds_when_heartbeat_advances(coord_dir, tmux, monkeypatch):
    """The user-visible 'wake an idle agent' path. Pre-send heartbeat
    is the baseline; if mtime advances within the timeout, success."""
    _write_registry(coord_dir, "developer", tool="claude")
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)

    advanced_calls = {"n": 0}
    def fake_poll(coord, role, baseline, timeout):
        advanced_calls["n"] += 1
        return True  # heartbeat advanced
    monkeypatch.setattr(se, "_poll_heartbeat_advance", fake_poll)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert ok, diag
    assert "heartbeat advanced" in diag
    assert advanced_calls["n"] == 1


def test_wake_sends_text_plus_enter_recipe(coord_dir, tmux, monkeypatch):
    """mode='wake' must send WAKE_TEXT then the Enter key (mirrors
    coordd.push_to_role — the only proven 'agent acts' recipe)."""
    _write_registry(coord_dir, "developer", tool="claude")
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)
    monkeypatch.setattr(se, "_poll_heartbeat_advance",
                        lambda *a, **kw: True)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert ok, diag
    # tmux fallback path (no input_sock): expect a text send THEN an Enter.
    send_keys_args = [c[-1] for c in tmux.calls if c[:2] == ["tmux", "send-keys"]]
    assert send_keys_args == ["continue your tick", "Enter"]


def test_wake_fails_when_heartbeat_does_not_advance(coord_dir, tmux, monkeypatch):
    """The user-stated failure mode: bytes delivered but agent did NOT
    act. iter-1's pane-diff false-negatived here (cell 1). iter-2 must
    correctly report False, with a ship-blocking diagnostic."""
    _write_registry(coord_dir, "developer", tool="claude")
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)
    monkeypatch.setattr(se, "_poll_heartbeat_advance",
                        lambda *a, **kw: False)  # no advance

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert not ok
    assert "heartbeat did not advance" in diag


def test_wake_falls_back_to_pane_diff_when_no_heartbeat_baseline(
    coord_dir, tmux, monkeypatch,
):
    """First-launch agent has no heartbeat file yet — fall back to
    proper pane-diff + process-state check (REVIEWER iter-2 fix).
    Success requires: pane CHANGED, not trust-blocked, process not
    SIGSTOPped."""
    _write_registry(coord_dir, "developer", tool="claude")
    # NO heartbeat file written.
    # Two captures: before (pane_baseline) and after (post-send + sleep).
    tmux.pane_sequence = [
        "developer> idle",                     # pre-send baseline
        "developer> tick 1 starting query…",   # post-send: visibly changed
    ]
    monkeypatch.setattr(se, "_pid_stopped", lambda pid: False)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert ok, diag
    assert "no prior heartbeat" in diag
    assert "pane changed" in diag


def test_wake_no_heartbeat_baseline_rejects_sigstopped_process(
    coord_dir, tmux, monkeypatch,
):
    """REVIEWER iter-2 changes_requested cell 4: bytes echo into the
    pty buffer even when the underlying process is SIGSTOPped and
    cannot act. iter-3 must NOT count that as success — process-state
    check (Linux /proc/<pid>/status: State: T) rejects this case."""
    _write_registry(coord_dir, "developer", tool="claude", pid=987)
    # NO heartbeat. Pane visibly changed (echo of our bytes).
    tmux.pane_sequence = [
        "developer> ...stalled",                # pre-send baseline
        "developer> ...stalledcontinue your tick\r",  # echo bytes appear
    ]
    # Process IS stopped.
    monkeypatch.setattr(se, "_pid_stopped", lambda pid: True)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert not ok, diag
    assert "SIGSTOPped" in diag or "stopped" in diag.lower()


def test_wake_no_heartbeat_baseline_rejects_unchanged_pane(
    coord_dir, tmux, monkeypatch,
):
    """If pane is unchanged after the send (regardless of why) AND
    no heartbeat baseline exists, that's NOT success. Previously the
    primitive returned True if the pane was simply non-empty — the
    iter-2 false-positive REVIEWER caught."""
    _write_registry(coord_dir, "developer", tool="claude")
    # Pane sequence: before == after (no change).
    tmux.pane_sequence = [
        "developer> stuck",
        "developer> stuck",
    ]
    monkeypatch.setattr(se, "_pid_stopped", lambda pid: False)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert not ok, diag
    assert "pane unchanged" in diag


def test_default_verify_timeout_is_30_seconds():
    """REVIEWER iter-2: default 5s is too short — TESTER observed
    claude pane changes visibly within ~30s of a wake. iter-3 bumps
    the default ceiling to 30s. Heartbeat poll exits early on advance,
    so fast wakes still return fast."""
    assert se._DEFAULT_VERIFY_TIMEOUT_S == 30.0


def test_wake_codex_falls_through_to_c_j_when_first_key_no_advance(
    coord_dir, tmux, monkeypatch,
):
    """codex defensive fallback: Enter didn't advance heartbeat,
    primitive tries C-j next."""
    _write_registry(coord_dir, "developer", tool="codex")
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)

    # First call (Enter): no advance. Second call (C-j): advance.
    calls = {"n": 0}
    def poll(coord, role, baseline, timeout):
        calls["n"] += 1
        return calls["n"] >= 2
    monkeypatch.setattr(se, "_poll_heartbeat_advance", poll)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "codex", mode="wake",
    )
    assert ok, diag
    # Both Enter and C-j must have been attempted via tmux.
    sent_keys = [c[-1] for c in tmux.calls if c[:2] == ["tmux", "send-keys"]]
    # Wake recipe = text-send then key, twice (Enter, C-j).
    assert sent_keys == ["continue your tick", "Enter",
                          "continue your tick", "C-j"]
    assert "key=C-j" in diag or "C-j" in diag


def test_wake_codex_returns_ship_blocking_when_no_key_advances_heartbeat(
    coord_dir, tmux, monkeypatch,
):
    """All three codex keys tried, heartbeat never advanced → ship-blocking."""
    _write_registry(coord_dir, "developer", tool="codex")
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)
    monkeypatch.setattr(se, "_poll_heartbeat_advance",
                        lambda *a, **kw: False)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "codex", mode="wake",
    )
    assert not ok
    assert "no key advanced the agent after 3 attempt" in diag
    assert "mode=wake" in diag


# ---------------------------------------------------------------------------
# mode="bare-enter" — pane-diff verify (launcher/dialog case)
# ---------------------------------------------------------------------------


def test_bare_enter_success_on_pane_change(coord_dir, tmux):
    """Used by greatminds restart after launching the tool shell.
    A bare Enter accepts the pre-filled start-agent line. Pane changes
    when the launcher proceeds."""
    _write_registry(coord_dir, "developer", tool="claude")
    tmux.pane_sequence = [
        "$ greatminds start-agent DEVELOPER claude --mode loop  ",  # before
        "$ greatminds start-agent DEVELOPER claude --mode loop\nstarting…",  # after
    ]
    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="bare-enter",
    )
    assert ok, diag
    assert "pane changed" in diag


def test_bare_enter_does_not_send_text(coord_dir, tmux):
    """mode='bare-enter' sends ONLY the Enter key (no WAKE_TEXT)."""
    _write_registry(coord_dir, "developer", tool="claude")
    tmux.pane_sequence = ["before", "after"]

    ok, _diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="bare-enter",
    )
    assert ok
    send_keys_args = [c[-1] for c in tmux.calls if c[:2] == ["tmux", "send-keys"]]
    assert send_keys_args == ["Enter"]
    assert "continue your tick" not in send_keys_args


# ---------------------------------------------------------------------------
# Two-channel send: input_sock first, tmux fallback
# ---------------------------------------------------------------------------


def test_prefers_input_sock_when_available_in_wake_mode(
    coord_dir, tmux, tmp_path, monkeypatch,
):
    """input_sock writes WAKE_TEXT then \\r; tmux send-keys NOT used."""
    sock_path = tmp_path / "developer.sock"
    sock_path.touch()
    _write_registry(coord_dir, "developer", tool="claude",
                    input_sock=str(sock_path))
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)
    monkeypatch.setattr(se, "_poll_heartbeat_advance",
                        lambda *a, **kw: True)

    sent: list[bytes] = []
    def fake_sock(path, payload, timeout_s=2.0):
        assert path == str(sock_path)
        sent.append(payload)
        return True
    monkeypatch.setattr(se, "_send_via_input_sock", fake_sock)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert ok, diag
    # Two writes: WAKE_TEXT then \r.
    assert sent == [b"continue your tick", b"\r"]
    # tmux send-keys NOT called for the key transport itself.
    sk = [c for c in tmux.calls if c[:2] == ["tmux", "send-keys"]]
    assert sk == []


def test_falls_back_to_tmux_when_input_sock_missing(coord_dir, tmux, monkeypatch):
    _write_registry(coord_dir, "developer", tool="claude")
    _write_heartbeat(coord_dir, "developer", mtime=1000.0)
    monkeypatch.setattr(se, "_poll_heartbeat_advance",
                        lambda *a, **kw: True)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert ok, diag
    assert "tmux send-keys" in diag


# ---------------------------------------------------------------------------
# Pid liveness gate
# ---------------------------------------------------------------------------


def test_dead_pid_short_circuits_with_clear_diag(coord_dir, monkeypatch):
    _write_registry(coord_dir, "developer", tool="claude", pid=12345)
    monkeypatch.setattr(se, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(se.time, "sleep", lambda *_a, **_k: None)

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="wake",
    )
    assert not ok
    assert "pid 12345 not alive" in diag
    assert "fresh launch" in diag


# ---------------------------------------------------------------------------
# Bad mode
# ---------------------------------------------------------------------------


def test_unknown_mode_returns_failure(coord_dir, tmux):
    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude", mode="garbage",
    )
    assert not ok
    assert "unknown mode" in diag


# ---------------------------------------------------------------------------
# verify=False mode (used by greatminds restart's fire-and-forget)
# ---------------------------------------------------------------------------


def test_verify_false_returns_after_first_send_no_polling(coord_dir, tmux):
    _write_registry(coord_dir, "developer", tool="claude")

    ok, diag = se.press_enter(
        coord_dir, "s", "dev", "developer", "claude",
        mode="bare-enter", verify=False,
    )
    assert ok, diag
    assert "no verify" in diag
    # No capture-pane / no heartbeat-poll in verify=False mode.
    cp = [c for c in tmux.calls if c[:2] == ["tmux", "capture-pane"]]
    assert cp == []
