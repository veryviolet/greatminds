"""Unit tests for ``greatminds restart``.

External effects (``systemctl``, ``tmux``, ``greatminds launch``,
``os.kill``, ``time.sleep``) are mocked. We exercise the click command
through ``CliRunner`` and assert on the recorded ``subprocess.run``
call list plus exit codes.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import restart as restart_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DEFAULT_WINDOWS = [
    {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude", "mode": "loop"},
    {"name": "dev", "role": "DEVELOPER", "tool": "claude", "mode": "loop"},
    {"name": "ui", "role": "UI-DEVELOPER", "tool": "claude", "mode": "loop"},
    {"name": "ops", "role": "", "tool": "bash"},
]


def _write_coord_yaml(
    project_dir: Path,
    windows: list[dict] | None = None,
    session: str = "test-session",
) -> Path:
    cfg = {
        "session": session,
        "project_dir": str(project_dir),
        "windows": windows if windows is not None else DEFAULT_WINDOWS,
    }
    p = project_dir / "coord.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (project_dir / "coordination" / ".agent_registry").mkdir(parents=True, exist_ok=True)
    return p


def _write_registry(
    project_dir: Path,
    role_lower: str,
    pid: int,
    with_sock: bool = True,
) -> Path:
    reg_dir = project_dir / "coordination" / ".agent_registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "role": role_lower.upper(),
        "tool": "claude",
        "pid": pid,
        "tty": "/dev/pts/0",
        "started_at": "2026-05-24T00:00:00Z",
    }
    if with_sock:
        payload["input_sock"] = str(reg_dir / f"{role_lower}.sock")
    path = reg_dir / f"{role_lower}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeSubprocess:
    """Records subprocess.run calls; returns rc/stdout/stderr per matcher."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.handlers: list = []  # list of (matcher, handler)
        self.default = subprocess.CompletedProcess([], 0, "", "")

    def set(self, prefix, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        prefix_t = tuple(prefix)
        cp = subprocess.CompletedProcess(list(prefix_t), rc, stdout, stderr)

        def matcher(cmd, _pfx=prefix_t):
            return tuple(cmd[: len(_pfx)]) == _pfx

        def handler(_cmd, _cp=cp):
            return _cp

        self.handlers.append((matcher, handler))

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        # Most-recently-added handler wins (so tests can override defaults).
        for matcher, handler in reversed(self.handlers):
            if matcher(cmd):
                return handler(cmd)
        return self.default

    def find(self, *needles: str) -> list[list[str]]:
        return [c for c in self.calls if all(n in c for n in needles)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeSubprocess()
    # tmux/systemctl/greatminds all reach subprocess.run.
    monkeypatch.setattr(restart_mod.subprocess, "run", fake)
    monkeypatch.setattr(restart_mod.time, "sleep", lambda *_a, **_kw: None)

    alive: set[int] = set()

    def fake_kill(pid: int, sig: int) -> None:
        if pid in alive:
            return None
        raise ProcessLookupError(pid)

    monkeypatch.setattr(restart_mod.os, "kill", fake_kill)

    # Sensible defaults: tmux session exists, coordd active; tests override.
    fake.set(("tmux", "has-session"), rc=0)
    fake.set(("systemctl", "--user", "is-active"), rc=0)
    fake.set(("systemctl", "--user", "show"), rc=0, stdout="123\n")
    fake.set(("tmux", "send-keys"), rc=0)
    fake.set(("greatminds", "launch"), rc=0)

    return SimpleNamespace(sub=fake, alive=alive, project_dir=tmp_path)


def _run(env_, **kwargs) -> "CliRunner.invoke":
    coord_yaml = env_.project_dir / "coord.yaml"
    if not coord_yaml.is_file():
        _write_coord_yaml(env_.project_dir, **kwargs)
    return CliRunner().invoke(
        restart_mod.restart,
        ["--config", str(coord_yaml)],
        catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# coordd paths
# ---------------------------------------------------------------------------


def test_coordd_already_active_no_start_call(env):
    _write_coord_yaml(env.project_dir)
    env.sub.set(("systemctl", "--user", "is-active"), rc=0)
    _run(env)
    assert env.sub.find("systemctl", "is-active")
    assert not env.sub.find("systemctl", "start"), \
        "systemctl --user start must NOT be called when a unit is active"


def test_coordd_per_project_template_present_starts_instance(env):
    """task 0055 (b) — REVIEWER catch: systemctl installs the TEMPLATE
    unit ``greatminds-daemon@.service``; instances are created on-the-fly
    at start time. The detector must check for the template, not for
    the instance, and start the instance from it."""
    _write_coord_yaml(env.project_dir, session="myproj")

    # Both is-active probes fail initially (no instance currently active).
    env.sub.set(("systemctl", "--user", "is-active"), rc=3)

    # list-unit-files: TEMPLATE unit file exists; legacy coordd does NOT.
    def list_unit_files(cmd):
        unit = cmd[-1]
        if unit == "greatminds-daemon@.service":
            return subprocess.CompletedProcess(
                list(cmd), 0,
                "greatminds-daemon@.service enabled\n", "",
            )
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    env.sub.handlers.append((
        lambda cmd: tuple(cmd[:4]) == (
            "systemctl", "--user", "list-unit-files", "--no-legend",
        ),
        list_unit_files,
    ))
    # After "start" the next is-active should report active.
    calls = {"n": 0}
    def is_active_after_start(cmd):
        calls["n"] += 1
        rc = 0 if calls["n"] > 2 else 3  # first two pre-start probes fail
        return subprocess.CompletedProcess(list(cmd), rc, "", "")
    env.sub.handlers.append((
        lambda cmd: tuple(cmd[:3]) == ("systemctl", "--user", "is-active"),
        is_active_after_start,
    ))
    env.sub.set(("systemctl", "--user", "start",
                 "greatminds-daemon@myproj.service"), rc=0)

    result = _run(env)
    # The INSTANCE was started exactly once (systemd creates the instance
    # from the template at start time).
    starts = env.sub.find("systemctl", "start",
                          "greatminds-daemon@myproj.service")
    assert len(starts) == 1, env.sub.calls
    # Legacy coordd NOT started.
    assert not env.sub.find("systemctl", "start", "coordd.service")
    # list-unit-files was queried with the TEMPLATE name, not the instance.
    list_calls = env.sub.find("systemctl", "list-unit-files")
    template_queries = [c for c in list_calls
                        if "greatminds-daemon@.service" in c]
    instance_queries = [c for c in list_calls
                        if "greatminds-daemon@myproj.service" in c]
    assert template_queries, "must query template unit file existence"
    assert not instance_queries, \
        "must NOT query for the instance unit file (it doesn't exist on disk)"


def test_coordd_no_daemon_unit_at_all_warns_and_continues(env):
    """task 0055 (c) — the actual regression: no daemon unit installed
    must NOT cause exit 1. restart's tmux Enter recovery is the
    user-visible value and must still fire."""
    _write_coord_yaml(env.project_dir, session="myproj",
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(701)
    _write_registry(env.project_dir, "developer", pid=701, with_sock=True)

    # No unit ever active; list-unit-files returns empty for every unit.
    env.sub.set(("systemctl", "--user", "is-active"), rc=3)
    env.sub.set(("systemctl", "--user", "list-unit-files"), rc=0, stdout="")

    result = _run(env)
    # Must NOT exit 1 on missing daemon — and any non-zero must NOT be
    # because "coordd not found" / "failed to start".
    assert "WARN: no coordd unit installed" in result.output
    # No systemctl start was attempted at all.
    assert not env.sub.find("systemctl", "start"), \
        "no start call when no unit exists"


def test_coordd_legacy_unit_fallback_when_per_project_missing(env):
    """task 0055 (d): only legacy coordd.service is installed → it
    gets used as the fallback (existing 1.0.x behavior preserved)."""
    _write_coord_yaml(env.project_dir, session="myproj")

    env.sub.set(("systemctl", "--user", "is-active"), rc=3)
    def list_unit_files(cmd):
        unit = cmd[-1]
        if unit == "coordd.service":
            return subprocess.CompletedProcess(
                list(cmd), 0, "coordd.service enabled\n", "",
            )
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    env.sub.handlers.append((
        lambda cmd: tuple(cmd[:4]) == (
            "systemctl", "--user", "list-unit-files", "--no-legend",
        ),
        list_unit_files,
    ))
    calls = {"n": 0}
    def is_active(cmd):
        calls["n"] += 1
        # First two pre-start probes fail, post-start active.
        rc = 0 if calls["n"] > 2 else 3
        return subprocess.CompletedProcess(list(cmd), rc, "", "")
    env.sub.handlers.append((
        lambda cmd: tuple(cmd[:3]) == ("systemctl", "--user", "is-active"),
        is_active,
    ))
    env.sub.set(("systemctl", "--user", "start", "coordd.service"), rc=0)

    result = _run(env)
    starts_legacy = env.sub.find("systemctl", "start", "coordd.service")
    starts_perproj = env.sub.find("systemctl", "start",
                                   "greatminds-daemon@myproj.service")
    assert len(starts_legacy) == 1, env.sub.calls
    # Per-project unit start NOT attempted (it didn't exist).
    assert not starts_perproj


# ---------------------------------------------------------------------------
# tmux session paths
# ---------------------------------------------------------------------------


def test_tmux_session_missing_calls_launch(env):
    _write_coord_yaml(env.project_dir)
    env.sub.set(("tmux", "has-session"), rc=1)
    _run(env)
    launches = env.sub.find("greatminds", "launch", "--target", "tmux")
    assert len(launches) == 1


def test_tmux_session_present_does_not_call_launch(env):
    _write_coord_yaml(env.project_dir)
    env.sub.set(("tmux", "has-session"), rc=0)
    _run(env)
    assert not env.sub.find("greatminds", "launch")


# ---------------------------------------------------------------------------
# Agent (re)start decisions
# ---------------------------------------------------------------------------


def test_registry_missing_sends_enter(env):
    _write_coord_yaml(env.project_dir)
    # No registry files at all.
    _run(env)
    send_keys_calls = env.sub.find("send-keys")
    targets = {c[c.index("-t") + 1] for c in send_keys_calls if "-t" in c}
    # Both planner and dev windows should get send-keys (ops skipped).
    assert "test-session:planner" in targets
    assert "test-session:dev" in targets
    assert "test-session:ui" in targets
    assert "test-session:ops" not in targets


def test_pid_dead_sends_enter_and_unlinks_stale_registry(env):
    _write_coord_yaml(env.project_dir)
    # Dead pid (NOT in env.alive) → registry should be removed and Enter sent.
    reg = _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    _run(env)
    assert not reg.is_file(), "stale registry must be unlinked"
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys") if "-t" in c}
    assert "test-session:dev" in targets


def test_pid_alive_does_not_send_enter_for_that_window(env):
    _write_coord_yaml(env.project_dir)
    env.alive.add(4242)
    _write_registry(env.project_dir, "developer", pid=4242, with_sock=True)

    _run(env)
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys") if "-t" in c}
    assert "test-session:dev" not in targets, \
        "live agent must NOT be re-poked"
    # Other roles (no registry) still get Enter.
    assert "test-session:planner" in targets


def test_ops_window_role_empty_is_skipped(env):
    _write_coord_yaml(env.project_dir)
    _run(env)
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys") if "-t" in c}
    assert "test-session:ops" not in targets


# ---------------------------------------------------------------------------
# Final verify
# ---------------------------------------------------------------------------


def test_verify_happy_path_exits_zero(env):
    """All windows have live pids + input_sock → exit 0."""
    _write_coord_yaml(
        env.project_dir,
        windows=[
            {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude"},
            {"name": "dev", "role": "DEVELOPER", "tool": "claude"},
        ],
    )
    env.alive.update({101, 102})
    _write_registry(env.project_dir, "architect-planner", pid=101, with_sock=True)
    _write_registry(env.project_dir, "developer", pid=102, with_sock=True)

    result = _run(env)
    assert result.exit_code == 0, result.output
    assert "ALL 2 agents up with input_sock bound" in result.output
    # No send-keys should fire (both alive).
    assert not env.sub.find("send-keys")


def test_verify_partial_fail_missing_input_sock(env):
    """Two roles; one is missing input_sock → exit 1."""
    _write_coord_yaml(
        env.project_dir,
        windows=[
            {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude"},
            {"name": "dev", "role": "DEVELOPER", "tool": "claude"},
        ],
    )
    env.alive.update({201, 202})
    _write_registry(env.project_dir, "architect-planner", pid=201, with_sock=True)
    _write_registry(env.project_dir, "developer", pid=202, with_sock=False)

    result = _run(env)
    assert result.exit_code == 1
    assert "role(s) failed to come up clean" in result.output
    assert "input_sock=NO" in result.output


# ---------------------------------------------------------------------------
# _resolve_session_default — generic, project-derived fallback
# ---------------------------------------------------------------------------


def test_resolve_session_default_uses_basename_of_project_dir(tmp_path):
    """basename(project_dir) wins as the fallback session name."""
    p = tmp_path / "foo-bar"
    p.mkdir()
    assert restart_mod._resolve_session_default(p) == "foo-bar"


def test_resolve_session_default_degrades_to_agents_for_root(tmp_path):
    """Path resolving to an empty `.name` (e.g. `/`) falls back to 'agents'."""
    assert restart_mod._resolve_session_default(Path("/")) == "agents"


def test_resolve_session_default_handles_nonexistent_dir(tmp_path):
    """Missing path: resolve(strict=False) still returns a name; no crash."""
    p = tmp_path / "never-created"
    # The dir does NOT exist; resolve() in non-strict mode returns it anyway.
    name = restart_mod._resolve_session_default(p)
    # Result must be a non-empty string, never the fleet-specific "greatminds-dev".
    assert name
    assert name != "greatminds-dev"
    assert name == "never-created"


def test_session_default_does_not_leak_greatminds_dev_into_session_targets(env):
    """End-to-end: a coord.yaml without `session:` must NOT result in any
    `greatminds-dev:` send-keys target. The fallback must be derived from
    project_dir (tmp_path basename) instead."""
    # coord.yaml with explicit `session: ""` to exercise the fallback path.
    cfg = {
        "project_dir": str(env.project_dir),
        # session deliberately omitted
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude"},
        ],
    }
    coord_yaml = env.project_dir / "coord.yaml"
    coord_yaml.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    runner = CliRunner()
    runner.invoke(
        restart_mod.restart,
        ["--config", str(coord_yaml)],
        catch_exceptions=False,
    )
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys") if "-t" in c}
    # No greatminds-dev anywhere.
    for t in targets:
        assert not t.startswith("greatminds-dev:"), \
            f"fallback leaked 'greatminds-dev' into send-keys target: {t!r}"
    # Falls back to basename(project_dir) = tmp_path.name.
    expected_session = env.project_dir.resolve().name
    assert any(t.startswith(f"{expected_session}:") for t in targets), \
        f"expected session prefix {expected_session!r} not in {targets!r}"


# ---------------------------------------------------------------------------
# task 0048: trust-prompt detection. _verify reads each pane via tmux
# capture-pane and surfaces "pending-trust" state — registry pid+sock can
# be present while the tool process is actually parked at the
# "Do you trust this folder?" dialog before the role contract starts.
# ---------------------------------------------------------------------------


def test_detect_trust_state_recognizes_claude_prompt():
    pane = (
        "...\n"
        "Do you trust the files in this folder?\n"
        "  Yes, proceed   No, exit\n"
    )
    assert restart_mod._detect_trust_state(pane) == "pending-trust"


def test_detect_trust_state_recognizes_codex_prompt():
    pane = "Allow Codex to run commands in this directory? [y/N]"
    assert restart_mod._detect_trust_state(pane) == "pending-trust"


def test_detect_trust_state_returns_ready_for_normal_output():
    pane = "[02:34:56Z] DEVELOPER tick: feature_dev empty, idle 60s"
    assert restart_mod._detect_trust_state(pane) == "ready"


def test_detect_trust_state_empty_pane_is_ready():
    """A pane we couldn't capture (empty string) defaults to ready —
    we don't fabricate a pending-trust state on missing evidence."""
    assert restart_mod._detect_trust_state("") == "ready"


def test_verify_flags_pending_trust_role_as_fail(env, monkeypatch):
    """A role whose pane is parked at the trust dialog must count as
    failed and surface a remediation hint, even with pid+sock present."""
    _write_coord_yaml(
        env.project_dir,
        windows=[
            {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude"},
            {"name": "dev", "role": "DEVELOPER", "tool": "claude"},
        ],
    )
    env.alive.update({501, 502})
    _write_registry(env.project_dir, "architect-planner", pid=501, with_sock=True)
    _write_registry(env.project_dir, "developer", pid=502, with_sock=True)

    # Stub _capture_pane directly — cleaner than re-routing subprocess.run.
    captured: dict[str, str] = {
        "test-session:planner": "DEVELOPER continuing tick — feature_dev empty",
        "test-session:dev": "Do you trust this folder?\n  > Yes",
    }
    monkeypatch.setattr(
        restart_mod, "_capture_pane",
        lambda session, window: captured.get(f"{session}:{window}", ""),
    )

    result = _run(env)
    assert result.exit_code == 1, result.output
    assert "trust=TRUST?" in result.output
    assert "stuck at tool-trust prompt" in result.output
    assert "test-session:dev" in result.output
    # The planner row should NOT be marked TRUST? (its pane is normal).
    planner_line = [l for l in result.output.splitlines()
                    if "architect-planner" in l and "trust=" in l]
    assert planner_line, result.output
    assert "trust=ok" in planner_line[0]


def test_verify_clean_when_no_pending_trust(env):
    """Normal pane content → trust=ok column, no spurious failures."""
    _write_coord_yaml(
        env.project_dir,
        windows=[{"name": "dev", "role": "DEVELOPER", "tool": "claude"}],
    )
    env.alive.update({601})
    _write_registry(env.project_dir, "developer", pid=601, with_sock=True)
    # Default FakeSubprocess returns stdout="" for capture-pane → "ready".

    result = _run(env)
    assert result.exit_code == 0, result.output
    assert "trust=ok" in result.output
    assert "stuck at tool-trust prompt" not in result.output


# ---------------------------------------------------------------------------
# task 0137: --bootstrap forces re-launch of alive agents
# ---------------------------------------------------------------------------


def _run_bootstrap(env_) -> "CliRunner.invoke":
    """Like _run but appends --bootstrap. Inlined here so the existing
    _run helper stays signature-stable for the non-bootstrap tests."""
    coord_yaml = env_.project_dir / "coord.yaml"
    if not coord_yaml.is_file():
        _write_coord_yaml(env_.project_dir)
    return CliRunner().invoke(
        restart_mod.restart,
        ["--config", str(coord_yaml), "--bootstrap"],
        catch_exceptions=False,
    )


def _seed_session_files(project_dir: Path, role_lower: str) -> tuple[Path, Path]:
    """Drop both claude and codex session-id files for ``role_lower`` so
    we can assert --bootstrap unlinks them."""
    reg_dir = project_dir / "coordination" / ".agent_registry"
    claude_sid = reg_dir / f"{role_lower}.session-id"
    codex_sid = reg_dir / f"{role_lower}.codex-session-id"
    claude_sid.write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")
    codex_sid.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="utf-8")
    return claude_sid, codex_sid


def test_bootstrap_kills_alive_pid_and_sends_enter(env, monkeypatch):
    """Default restart skips alive agents (idempotent). --bootstrap
    SIGTERMs them and re-launches via the same Enter path the dead-pid
    case uses. Closes 0137: post-upgrade refresh of a running fleet."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(7777)
    _write_registry(env.project_dir, "developer", pid=7777, with_sock=True)

    kill_calls: list[tuple[int, int]] = []

    def recording_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0 and pid in env.alive:
            return  # _pid_alive probe
        if sig == 0:
            raise ProcessLookupError(pid)
        # SIGTERM: drop the pid from alive so the verify step sees
        # it as dead (we don't go through the verify happy path
        # here; tests assert before the re-launch completes).
        env.alive.discard(pid)
    monkeypatch.setattr(restart_mod.os, "kill", recording_kill)

    _run_bootstrap(env)

    # SIGTERM was sent to the alive agent (sig 15).
    import signal
    assert (7777, signal.SIGTERM) in kill_calls, kill_calls

    # send-keys Enter was sent to dev's window — same wake path as the
    # dead-pid case (start-agent line waiting in the tmux pane).
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys")
               if "-t" in c}
    assert "test-session:dev" in targets


def test_bootstrap_unlinks_session_id_files(env, monkeypatch):
    """0137: --bootstrap must clear BOTH claude and codex session-id
    files so the next start-agent reads no prior session — it picks
    the fresh path (no claude --resume, no codex resume <sid>). Without
    this, the new wheel's code path runs but the LLM context is still
    the resumed one, defeating the bootstrap purpose."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(8888)
    reg = _write_registry(env.project_dir, "developer", pid=8888,
                          with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")
    assert claude_sid.is_file() and codex_sid.is_file()

    # os.kill: accept anything (don't actually kill).
    monkeypatch.setattr(restart_mod.os, "kill", lambda *_: None)

    _run_bootstrap(env)

    # Both session files removed; registry json removed too.
    assert not claude_sid.is_file(), "claude session-id must be unlinked"
    assert not codex_sid.is_file(), "codex session-id must be unlinked"
    assert not reg.is_file(), "registry .json must be unlinked"


def test_default_restart_does_not_unlink_alive_session_files(env):
    """Negative pin: without --bootstrap, alive agents are left
    completely untouched — no SIGTERM, no session-id unlink. The
    bootstrap branch must NOT bleed into the default path."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(9999)
    _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    _run(env)  # no --bootstrap

    assert claude_sid.is_file(), \
        "alive agent's claude session-id must be preserved without --bootstrap"
    assert codex_sid.is_file(), \
        "alive agent's codex session-id must be preserved without --bootstrap"
    # And no send-keys to the alive window.
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys")
               if "-t" in c}
    assert "test-session:dev" not in targets


def test_bootstrap_clears_session_files_for_dead_pid_too(env, monkeypatch):
    """0137 iter-2 (REVIEWER ask): with --bootstrap, the promise is
    'fresh session for every role being relaunched' — not just alive
    ones. A dead-pid role + --bootstrap must ALSO clear the claude
    and codex session-id files, otherwise the next launch silently
    resumes the old conversation on the new wheel. Iter-1 left dead
    agents resuming, contradicting the task contract.

    SIGTERM is still NOT sent for dead pids (no-op).
    """
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    # Pid 6666 NOT in env.alive → dead per the fixture's fake_kill.
    reg = _write_registry(env.project_dir, "developer", pid=6666,
                          with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    kill_calls: list[tuple[int, int]] = []

    def recording_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if pid in env.alive:
            return
        raise ProcessLookupError(pid)
    monkeypatch.setattr(restart_mod.os, "kill", recording_kill)

    _run_bootstrap(env)

    assert not reg.is_file()
    # SIGTERM is NOT sent to a dead pid (alive-only branch).
    import signal
    assert not any(sig == signal.SIGTERM for _pid, sig in kill_calls)
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys")
               if "-t" in c}
    assert "test-session:dev" in targets
    # Iter-2: session-id files must be cleared on bootstrap even for
    # dead pids. Otherwise start_agent's next call would read the old
    # claude session UUID and codex rollout SID, and the new launch
    # would --resume / codex resume <sid> into the stale conversation.
    assert not claude_sid.is_file(), (
        "0137 iter-2: bootstrap must clear claude session-id even for dead pids"
    )
    assert not codex_sid.is_file(), (
        "0137 iter-2: bootstrap must clear codex session-id even for dead pids"
    )


def test_bootstrap_clears_session_files_for_missing_registry(env, monkeypatch):
    """0137 iter-2: when a role has NO registry entry (first start, or
    registry was wiped) AND --bootstrap is set, any pre-existing
    session-id files from a prior launch must be cleared so the new
    launch is genuinely fresh. The missing-registry path also goes
    through the 'needs_start' bootstrap branch."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    # No registry json at all, but session-id files left over from a
    # prior session.
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    monkeypatch.setattr(restart_mod.os, "kill", lambda *_: None)
    _run_bootstrap(env)

    assert not claude_sid.is_file()
    assert not codex_sid.is_file()
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys")
               if "-t" in c}
    assert "test-session:dev" in targets


def test_default_restart_preserves_session_files_for_dead_pid(env):
    """Negative pin: without --bootstrap, dead-pid path preserves
    session-id files. Crash recovery should keep claude --resume
    semantics — only --bootstrap rotates the session."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    _write_registry(env.project_dir, "developer", pid=5555, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    _run(env)  # no --bootstrap

    assert claude_sid.is_file(), (
        "dead-pid recovery (no --bootstrap) must keep claude session "
        "continuity"
    )
    assert codex_sid.is_file()


def test_bootstrap_flag_help_mentions_post_upgrade_use(env):
    """The --bootstrap help text must point operators at the upgrade
    use case so the flag is discoverable from `greatminds restart
    --help`. Without this hint, MAINTAINER might leave a running fleet
    on the old code after pip install -U."""
    result = CliRunner().invoke(restart_mod.restart, ["--help"])
    assert "--bootstrap" in result.output
    assert "alive" in result.output.lower()
    assert "fresh" in result.output.lower() or "no claude --resume" in result.output
