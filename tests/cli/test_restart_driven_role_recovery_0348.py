"""Tests for task 0348: ``greatminds restart`` must not emit an invalid
``start-agent --mode driven`` for driven roles, and must not report a
driven role MISSING.

A driven role (coord.yaml ``mode: driven``) runs NO persistent agent —
the pane is idle bash between turns and coordd drives each turn (claude
--resume -p / codex app-server), force-freshing the session on the first
event after a kill. Pre-0348, restart saw the absent registry, treated it
as a dead agent, and sent ``greatminds start-agent <ROLE> codex --mode
driven`` — start-agent only accepts loop|chat, so it errored and left the
role MISSING (and restart exited non-zero). launch.py already skips
``mode == driven``; restart must mirror that.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

# reuse the harness from the sibling restart test module (same dir is on
# sys.path under pytest's default prepend import mode)
from test_restart import (  # noqa: E402
    FakeSubprocess, _write_coord_yaml, _write_registry,
)
from greatminds.cli import restart as restart_mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeSubprocess()
    monkeypatch.setattr(restart_mod.subprocess, "run", fake)
    monkeypatch.setattr(restart_mod.time, "sleep", lambda *_a, **_kw: None)
    alive: set[int] = set()
    monkeypatch.setattr(restart_mod.os, "kill",
                        lambda pid, sig: None if pid in alive
                        else (_ for _ in ()).throw(ProcessLookupError(pid)))
    fake.set(("tmux", "has-session"), rc=0)
    fake.set(("systemctl", "--user", "is-active"), rc=0)
    fake.set(("systemctl", "--user", "show"), rc=0, stdout="123\n")
    fake.set(("tmux", "send-keys"), rc=0)
    fake.set(("greatminds", "launch"), rc=0)
    return SimpleNamespace(sub=fake, alive=alive, project_dir=tmp_path)


def _run(env_):
    coord_yaml = env_.project_dir / "coord.yaml"
    return CliRunner().invoke(restart_mod.restart,
                              ["--config", str(coord_yaml)],
                              catch_exceptions=False)


_DRIVEN_WINDOWS = [
    {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude",
     "mode": "chat"},
    {"name": "writer", "role": "TECHNICAL-WRITER", "tool": "codex",
     "mode": "driven"},
]


def _all_send_keys_text(env_) -> str:
    return " ".join(" ".join(c) for c in env_.sub.find("send-keys"))


def test_driven_role_no_start_agent_emitted(env):
    """The reported bug: restart must NOT send start-agent --mode driven
    for the driven codex writer (start-agent rejects it)."""
    _write_coord_yaml(env.project_dir, windows=_DRIVEN_WINDOWS)
    # TECHNICAL-WRITER has no registry (killed / idle between turns).
    _run(env)
    text = _all_send_keys_text(env)
    assert "--mode driven" not in text, (
        "0348: restart must never emit start-agent --mode driven")
    # no start-agent launch targeting the writer window at all
    writer_sends = [c for c in env.sub.find("send-keys")
                    if "-t" in c and "test-session:writer" in c
                    and any("start-agent" in part for part in c)]
    assert not writer_sends, (
        "0348: driven role must not get a start-agent launch command")


def test_driven_role_not_reported_missing_exit_zero(env):
    """A healthy fleet whose only non-chat role is a driven codex worker
    (no persistent registry) must verify exit 0, not MISSING/non-zero."""
    _write_coord_yaml(env.project_dir, windows=_DRIVEN_WINDOWS)
    env.alive.add(101)
    _write_registry(env.project_dir, "architect-planner", pid=101,
                    with_sock=True)
    # writer: intentionally NO registry (driven, idle between turns)
    result = _run(env)
    assert result.exit_code == 0, result.output
    assert "technical-writer" in result.output.lower()
    assert "driven" in result.output.lower()
    assert "MISSING" not in result.output


def test_non_driven_dead_role_still_relaunched(env):
    """Regression: a loop/chat role with a dead registry still gets a
    start-agent relaunch (the driven skip must not leak to other modes)."""
    _write_coord_yaml(env.project_dir, windows=[
        {"name": "dev", "role": "DEVELOPER", "tool": "claude",
         "mode": "loop"},
        {"name": "writer", "role": "TECHNICAL-WRITER", "tool": "codex",
         "mode": "driven"},
    ])
    # dev has a dead pid → must be relaunched with a valid --mode loop
    _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    _run(env)
    text = _all_send_keys_text(env)
    assert "--mode loop" in text
    assert "--mode driven" not in text
    dev_sends = [c for c in env.sub.find("send-keys")
                 if "-t" in c and "test-session:dev" in c]
    assert dev_sends, "dead loop role must still be relaunched"
