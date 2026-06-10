"""Tests for task 0385: ``greatminds restart`` must not count a
``mode: staged`` role (coord.yaml, e.g. LIVE-DEVELOPER) absent from the
registry as a fleet failure, and must not auto-(re)start it.

A staged role is USER-paced: launch.py pre-types the start-agent command
into the pane but does NOT submit Enter — the USER starts/stops the live
session. So an absent registry for a staged role is the EXPECTED resting
state, not a dead agent. Pre-0385, restart saw the absent registry,
treated it as a dead agent, sent ``greatminds start-agent <ROLE> claude
--mode staged`` (start-agent only accepts loop|chat → it errored), and
flagged the role MISSING so restart exited non-zero on an otherwise
healthy fleet (the avatar 0379 repro). launch.py already skips
``mode == staged``; restart must mirror that, exactly as 0348 did for
``mode == driven``.
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


_STAGED_WINDOWS = [
    {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude",
     "mode": "chat"},
    {"name": "live", "role": "LIVE-DEVELOPER", "tool": "claude",
     "mode": "staged"},
]


def _all_send_keys_text(env_) -> str:
    return " ".join(" ".join(c) for c in env_.sub.find("send-keys"))


def test_staged_role_no_start_agent_emitted(env):
    """The reported bug: restart must NOT send start-agent --mode staged
    for the staged LIVE-DEVELOPER (start-agent rejects it)."""
    _write_coord_yaml(env.project_dir, windows=_STAGED_WINDOWS)
    # LIVE-DEVELOPER has no registry (USER-paced, not started).
    _run(env)
    text = _all_send_keys_text(env)
    assert "--mode staged" not in text, (
        "0385: restart must never emit start-agent --mode staged")
    # no start-agent launch targeting the live window at all
    live_sends = [c for c in env.sub.find("send-keys")
                  if "-t" in c and "test-session:live" in c
                  and any("start-agent" in part for part in c)]
    assert not live_sends, (
        "0385: staged role must not get a start-agent launch command")


def test_staged_role_not_reported_missing_exit_zero(env):
    """A healthy fleet whose only non-chat role is a staged LIVE-DEVELOPER
    (no persistent registry, USER hasn't started it) must verify exit 0,
    not MISSING/non-zero — the avatar 0379 repro."""
    _write_coord_yaml(env.project_dir, windows=_STAGED_WINDOWS)
    env.alive.add(101)
    _write_registry(env.project_dir, "architect-planner", pid=101,
                    with_sock=True)
    # live: intentionally NO registry (staged, USER hasn't started it)
    result = _run(env)
    assert result.exit_code == 0, result.output
    assert "live-developer" in result.output.lower()
    assert "staged" in result.output.lower()
    assert "MISSING" not in result.output


def test_staged_role_running_reported_uncounted(env):
    """When the USER HAS started the staged session, restart surfaces its
    live pid for visibility but still does NOT count it toward pass/fail."""
    _write_coord_yaml(env.project_dir, windows=_STAGED_WINDOWS)
    env.alive.update({101, 202})
    _write_registry(env.project_dir, "architect-planner", pid=101,
                    with_sock=True)
    # USER started the staged live session → it has a live registry entry,
    # but deliberately with NO input_sock to prove the staged branch never
    # reaches the input_sock/total accounting that would fail it.
    _write_registry(env.project_dir, "live-developer", pid=202,
                    with_sock=False)
    result = _run(env)
    assert result.exit_code == 0, result.output
    assert "staged (running" in result.output, result.output


def test_non_staged_dead_role_still_relaunched(env):
    """Regression: a loop/chat role with a dead registry still gets a
    start-agent relaunch (the staged skip must not leak to other modes)."""
    _write_coord_yaml(env.project_dir, windows=[
        {"name": "dev", "role": "DEVELOPER", "tool": "claude",
         "mode": "loop"},
        {"name": "live", "role": "LIVE-DEVELOPER", "tool": "claude",
         "mode": "staged"},
    ])
    # dev has a dead pid → must be relaunched with a valid --mode loop
    _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    _run(env)
    text = _all_send_keys_text(env)
    assert "--mode loop" in text
    assert "--mode staged" not in text
    dev_sends = [c for c in env.sub.find("send-keys")
                 if "-t" in c and "test-session:dev" in c]
    assert dev_sends, "dead loop role must still be relaunched"
