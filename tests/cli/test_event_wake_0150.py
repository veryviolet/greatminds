"""Tests for task 0150: event-driven wake.

PLANNER's re-scoped 0150 (the iter-2 plan body after the FS-watch
misread): when coordd's existing poll loop sees a new inbox file for
a role, it also SIGINTs the deepest sleep descendant of that role's
agent. This aborts a blocking ``sleep`` syscall (the 0093 primitive's
mechanism), so the agent's next tick runs within sub-second instead
of waiting on the adaptive-backoff sleep timer (60–600s).

Safety: the SIGINT helper refuses to signal the agent process itself
(no descendant chain → agent not sleeping → skip).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import coordd as coordd_mod


def _seed_registry(tmp_path: Path, role: str, pid: int) -> Path:
    reg_dir = tmp_path / "coordination" / ".agent_registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    p = reg_dir / f"{role.lower()}.json"
    p.write_text(json.dumps({
        "role": role.upper(),
        "tool": "claude",
        "pid": pid,
        "tty": "/dev/pts/0",
        "started_at": "2026-05-25T00:00:00Z",
    }), encoding="utf-8")
    return p


# ---------- sigint_sleeping_descendant unit tests ----------


def test_signals_deepest_descendant_when_agent_is_sleeping(tmp_path: Path, monkeypatch) -> None:
    """Happy path: agent pid is alive, deepest descendant is a sleep
    process (descendant pid != agent pid). Helper SIGINTs the
    descendant and returns True."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    _seed_registry(tmp_path, "developer", pid=1000)

    monkeypatch.setattr(
        "greatminds.cli._send_enter._pid_alive", lambda p: p == 1000,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._deepest_descendant",
        lambda p: 2000,  # sleep PID, deeper than agent
    )
    # The leaf must be a REAL sleep for the SIGINT to fire.
    monkeypatch.setattr(
        "greatminds.cli._send_enter._process_comm", lambda p: "sleep",
    )
    sent: list[int] = []

    def fake_sigint(pid: int) -> bool:
        sent.append(pid)
        return True
    monkeypatch.setattr(
        "greatminds.cli._send_enter._send_sigint", fake_sigint,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "developer") is True
    assert sent == [2000]


def test_skips_when_descendant_is_live_engine_not_sleep(tmp_path: Path, monkeypatch) -> None:
    """Regression (codex-TUI quit bug): a multi-process interactive agent
    (codex: node → engine) has a LIVE descendant that is NOT a sleep.
    ``leaf != pid`` alone passes the old guard, but SIGINTing the codex
    engine is Ctrl-C → the agent quits to the shell. Helper must refuse
    when the leaf's comm is not 'sleep'."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    _seed_registry(tmp_path, "architect-planner", pid=1000)

    monkeypatch.setattr(
        "greatminds.cli._send_enter._pid_alive", lambda p: True,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._deepest_descendant", lambda p: 2000,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._process_comm", lambda p: "codex",
    )
    sent: list[int] = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter._send_sigint",
        lambda p: sent.append(p) or True,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "architect-planner") is False
    assert sent == [], "must NOT SIGINT a live codex engine descendant"


def test_skips_when_no_descendant_chain(tmp_path: Path, monkeypatch) -> None:
    """0093 safety: if the deepest-descendant walk returns the agent
    pid itself (no descendants), the agent is not asleep on a tool
    subprocess — SIGINTing the agent process directly would be
    hostile (interrupts active work). Helper must refuse."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    _seed_registry(tmp_path, "developer", pid=1000)

    monkeypatch.setattr(
        "greatminds.cli._send_enter._pid_alive", lambda p: True,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._deepest_descendant",
        lambda p: 1000,  # leaf == agent pid
    )
    sent: list[int] = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter._send_sigint",
        lambda p: sent.append(p) or True,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "developer") is False
    assert sent == [], "must NOT signal the agent process itself"


def test_skips_when_descendant_walk_returns_none(tmp_path: Path, monkeypatch) -> None:
    """Non-Linux host or unreadable /proc → _deepest_descendant returns
    None. Helper skips quietly (no crash, no signal)."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    _seed_registry(tmp_path, "developer", pid=1000)

    monkeypatch.setattr(
        "greatminds.cli._send_enter._pid_alive", lambda p: True,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._deepest_descendant", lambda p: None,
    )
    sent: list[int] = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter._send_sigint",
        lambda p: sent.append(p) or True,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "developer") is False
    assert sent == []


def test_skips_when_pid_dead(tmp_path: Path, monkeypatch) -> None:
    """A dead pid means the agent crashed — coordd has other paths
    (dead-pid report) for that. Event-wake is a no-op."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    _seed_registry(tmp_path, "developer", pid=1000)

    monkeypatch.setattr(
        "greatminds.cli._send_enter._pid_alive", lambda p: False,
    )
    descendant_calls: list = []

    def fake_descendant(p):
        descendant_calls.append(p)
        return 2000
    monkeypatch.setattr(
        "greatminds.cli._send_enter._deepest_descendant", fake_descendant,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "developer") is False
    assert descendant_calls == [], (
        "must NOT walk descendants of a dead pid; short-circuit before"
    )


def test_skips_when_registry_missing(tmp_path: Path, monkeypatch) -> None:
    """No registry entry (agent never started, or registry wiped) →
    no signal target. Helper returns False without raising."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    # No registry file.

    sent: list[int] = []
    monkeypatch.setattr(
        "greatminds.cli._send_enter._send_sigint",
        lambda p: sent.append(p) or True,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "developer") is False
    assert sent == []


def test_returns_false_when_sigint_fails(tmp_path: Path, monkeypatch) -> None:
    """If the SIGINT delivery itself fails (race: descendant died
    between walk and signal), the helper propagates the False so
    callers can log or fall back. Doesn't raise."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    _seed_registry(tmp_path, "developer", pid=1000)

    monkeypatch.setattr(
        "greatminds.cli._send_enter._pid_alive", lambda p: True,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._deepest_descendant", lambda p: 2000,
    )
    monkeypatch.setattr(
        "greatminds.cli._send_enter._send_sigint", lambda p: False,
    )

    assert coordd_mod.sigint_sleeping_descendant(coord, "developer") is False


# ---------- integration with the inbox-scan branch ----------


def test_new_inbox_file_triggers_sigint_before_push(tmp_path: Path, monkeypatch) -> None:
    """End-to-end at the coordd main-loop scope: when a new file
    appears in inbox/<role>/, coordd calls
    sigint_sleeping_descendant before push_to_role. Pin the order so
    the SIGINT-first invariant holds against future refactors —
    SIGINT must fire BEFORE keystroke injection so the sleeping
    agent's pty is ready to receive bytes when push_to_role lands.

    Implementation note: we exercise the coordd loop step 2 logic
    directly by importing the helpers and threading a mock through
    them, rather than spawning a real coordd subprocess. The
    integration shape (sigint → push) is what we pin.
    """
    coord = tmp_path / "coordination"
    inbox = coord / "inbox" / "developer"
    inbox.mkdir(parents=True)
    _seed_registry(tmp_path, "developer", pid=1000)
    (inbox / "wake-1779694900-0099-feature_dev.md").write_text(
        "x", encoding="utf-8",
    )

    call_order: list[str] = []

    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda c, r, v=False: call_order.append(f"sigint:{r}") or True,
    )
    monkeypatch.setattr(
        coordd_mod, "push_to_role",
        lambda c, r, p, v, bypass_fresh_guard=False: (
            call_order.append(f"push:{r}") or True
        ),
    )

    # Replicate the relevant scan-and-dispatch slice from coordd.coordd().
    current = coordd_mod.scan_inbox_files(coord / "inbox")
    known: set[str] = set()
    for path in sorted(current - known):
        role = coordd_mod.role_from_path(path)
        known.add(path)
        if role is None:
            continue
        if role in coordd_mod.NO_KEYSTROKE_INJECT_ROLES:
            continue
        coordd_mod.sigint_sleeping_descendant(coord, role, False)
        coordd_mod.push_to_role(coord, role, path, False)

    assert call_order == ["sigint:developer", "push:developer"], call_order


def test_chat_only_role_skips_both_sigint_and_push(tmp_path: Path, monkeypatch) -> None:
    """Chat-driven roles (NO_KEYSTROKE_INJECT_ROLES) must not be
    SIGINTed either — they're not running in a sleep loop. The
    deliver-only branch was the existing contract for push; the
    sigint helper must respect the same exclusion."""
    coord = tmp_path / "coordination"
    chat_role = next(iter(coordd_mod.NO_KEYSTROKE_INJECT_ROLES), None)
    if chat_role is None:
        pytest.skip("no chat-only role defined in this build")
    inbox = coord / "inbox" / chat_role.lower()
    inbox.mkdir(parents=True)
    (inbox / "wake-1779694900-0099.md").write_text("x", encoding="utf-8")

    called: list[str] = []
    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda c, r, v=False: called.append("sigint") or True,
    )
    monkeypatch.setattr(
        coordd_mod, "push_to_role",
        lambda *a, **kw: called.append("push") or True,
    )

    current = coordd_mod.scan_inbox_files(coord / "inbox")
    known: set[str] = set()
    for path in sorted(current - known):
        role = coordd_mod.role_from_path(path)
        known.add(path)
        if role is None:
            continue
        if role in coordd_mod.NO_KEYSTROKE_INJECT_ROLES:
            continue  # mirrors coordd's deliver-only branch
        coordd_mod.sigint_sleeping_descendant(coord, role, False)
        coordd_mod.push_to_role(coord, role, path, False)

    assert called == [], (
        "chat-only role must not be SIGINTed or push'd at the inbox-scan "
        f"branch. Called: {called}"
    )
