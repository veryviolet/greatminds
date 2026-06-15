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
    (project_dir / ".greatminds" / ".agent_registry").mkdir(parents=True, exist_ok=True)
    return p


def _write_registry(
    project_dir: Path,
    role_lower: str,
    pid: int,
    with_sock: bool = True,
) -> Path:
    reg_dir = project_dir / ".greatminds" / ".agent_registry"
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


def _launch_calls(env_) -> list[list[str]]:
    """Find ``greatminds launch --target tmux`` subprocess calls.

    0384: argv[0] is no longer the bare string ``greatminds`` — it is the
    PATH-independent prefix from ``_greatminds_cmd()`` (an absolute sibling
    executable path, or ``[sys.executable, "-m", "greatminds.cli.main"]``).
    Match on the stable ``launch --target tmux`` tail instead.
    """
    return [c for c in env_.sub.calls
            if "launch" in c and "--target" in c and "tmux" in c]


def test_tmux_session_missing_calls_launch(env):
    _write_coord_yaml(env.project_dir)
    env.sub.set(("tmux", "has-session"), rc=1)
    _run(env)
    launches = _launch_calls(env)
    assert len(launches) == 1
    # 0384: argv[0] must NOT be the bare ``greatminds`` (PATH-dependent).
    assert launches[0][0] != "greatminds", launches[0]


def test_tmux_session_present_does_not_call_launch(env):
    _write_coord_yaml(env.project_dir)
    env.sub.set(("tmux", "has-session"), rc=0)
    _run(env)
    assert not _launch_calls(env)


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


def test_pid_dead_clears_volatile_but_preserves_session_id(env):
    _write_coord_yaml(env.project_dir)
    # Dead pid (NOT in env.alive) + a recorded session_id (driven-claude
    # stores its session UUID in the .json). Default restart must clear the
    # stale pid/input_sock but PRESERVE session_id — destroying a session
    # is reserved for --reset, never a side effect of a plain restart.
    reg = _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    import json as _json
    data = _json.loads(reg.read_text())
    data["session_id"] = "sess-keep-me"
    reg.write_text(_json.dumps(data))

    _run(env)

    assert reg.is_file(), "registry kept (only volatile fields cleared)"
    after = _json.loads(reg.read_text())
    assert after.get("session_id") == "sess-keep-me", (
        "default restart must PRESERVE session_id (no flagless session kill)")
    assert "pid" not in after and "input_sock" not in after, (
        "volatile pid/input_sock must be cleared for the dead agent")
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys") if "-t" in c}
    assert "test-session:dev" in targets


def test_default_restart_preserves_sidecar_session_files_dead_pid(env):
    """Default restart (NO --reset) must preserve the claude/codex
    session-id sidecar files for a dead agent — session destruction is
    reserved for the explicit --reset flag."""
    _write_coord_yaml(env.project_dir)
    _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    reg_dir = env.project_dir / ".greatminds" / ".agent_registry"
    (reg_dir / "developer.session-id").write_text("claude-sid", encoding="utf-8")
    (reg_dir / "developer.codex-session-id").write_text("codex-sid", encoding="utf-8")

    _run(env)

    assert (reg_dir / "developer.session-id").is_file(), \
        "default restart must NOT drop claude session-id (dead pid)"
    assert (reg_dir / "developer.codex-session-id").is_file(), \
        "default restart must NOT drop codex session-id (dead pid)"


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
# task 0147: --bootstrap (soft canon refresh) + --reset (destructive)
#
# 0137 shipped --bootstrap as destructive (SIGTERM + clear session files +
# fresh re-launch). MAINTAINER's first 1.2.5 upgrade attempt caught that
# this loses agent context — useless as a default upgrade procedure. 0147
# splits the flag:
#
#   --bootstrap  soft: tmux-paste render-role into the live pane via
#                bracketed paste, submit with Enter; agent's next reply
#                reads the new canon. Session-id files preserved, pid
#                unchanged, claude --resume / codex resume continuity
#                kept. The canonical post-PyPI-upgrade procedure.
#   --reset      destructive: the formerly-0137 behavior. SIGTERMs the
#                alive pid, clears claude/codex session-id files, fresh
#                re-launch. The nuclear option — explicit operator
#                choice only, not the default.
# ---------------------------------------------------------------------------


def _run_bootstrap(env_) -> "CliRunner.invoke":
    """Like _run but appends --bootstrap (soft canon refresh)."""
    coord_yaml = env_.project_dir / "coord.yaml"
    if not coord_yaml.is_file():
        _write_coord_yaml(env_.project_dir)
    return CliRunner().invoke(
        restart_mod.restart,
        ["--config", str(coord_yaml), "--bootstrap"],
        catch_exceptions=False,
    )


def _run_reset(env_) -> "CliRunner.invoke":
    """Like _run but appends --reset (destructive re-launch)."""
    coord_yaml = env_.project_dir / "coord.yaml"
    if not coord_yaml.is_file():
        _write_coord_yaml(env_.project_dir)
    return CliRunner().invoke(
        restart_mod.restart,
        ["--config", str(coord_yaml), "--reset"],
        catch_exceptions=False,
    )


def _seed_session_files(project_dir: Path, role_lower: str) -> tuple[Path, Path]:
    """Drop both claude and codex session-id files for ``role_lower``."""
    reg_dir = project_dir / ".greatminds" / ".agent_registry"
    claude_sid = reg_dir / f"{role_lower}.session-id"
    codex_sid = reg_dir / f"{role_lower}.codex-session-id"
    claude_sid.write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")
    codex_sid.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="utf-8")
    return claude_sid, codex_sid


# ---------- --bootstrap soft path (0147) ----------


def test_bootstrap_soft_preserves_session_files_for_alive_agent(env, monkeypatch):
    """0147 contract: --bootstrap (soft) does NOT touch claude/codex
    session-id files for alive agents. claude --resume continuity
    must survive the canon refresh."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(7777)
    _write_registry(env.project_dir, "developer", pid=7777, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    # Stub the soft-inject helper so the test doesn't shell out to
    # `greatminds render-role` or tmux paste-buffer.
    monkeypatch.setattr(
        restart_mod, "_soft_inject_bootstrap",
        lambda session, name, role, coord_dir: (True, "stubbed bootstrap inject"),
    )

    _run_bootstrap(env)

    assert claude_sid.is_file(), \
        "0147: --bootstrap (soft) must preserve claude session-id"
    assert codex_sid.is_file(), \
        "0147: --bootstrap (soft) must preserve codex session-id"


def test_bootstrap_soft_does_not_sigterm_alive_agent(env, monkeypatch):
    """0147: --bootstrap must NOT kill alive agents. The agent
    pid is unchanged after the operation; its in-memory state is
    preserved by design."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(7777)
    _write_registry(env.project_dir, "developer", pid=7777, with_sock=True)

    kill_calls: list[tuple[int, int]] = []

    def recording_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0 and pid in env.alive:
            return
        if sig == 0:
            raise ProcessLookupError(pid)
        env.alive.discard(pid)
    monkeypatch.setattr(restart_mod.os, "kill", recording_kill)
    monkeypatch.setattr(
        restart_mod, "_soft_inject_bootstrap",
        lambda *_a, **_kw: (True, "stubbed"),
    )

    _run_bootstrap(env)

    import signal
    assert not any(sig == signal.SIGTERM for _pid, sig in kill_calls), (
        "0147: --bootstrap (soft) must NOT SIGTERM alive agents"
    )


def test_bootstrap_soft_calls_render_role_inject_for_alive_agent(env, monkeypatch):
    """0147 contract: --bootstrap drives _soft_inject_bootstrap for
    every alive role. This is the canon-refresh mechanism — pin the
    invocation so it can't silently no-op in a future refactor."""
    _write_coord_yaml(
        env.project_dir,
        windows=[
            {"name": "dev", "role": "DEVELOPER", "tool": "claude"},
            {"name": "planner", "role": "ARCHITECT-PLANNER", "tool": "claude"},
        ],
    )
    env.alive.update({4001, 4002})
    _write_registry(env.project_dir, "developer", pid=4001, with_sock=True)
    _write_registry(env.project_dir, "architect-planner", pid=4002,
                    with_sock=True)

    inject_calls: list[tuple[str, str, str]] = []

    def recording_inject(session: str, name: str, role: str, coord_dir):
        inject_calls.append((session, name, role))
        return True, "stubbed"
    monkeypatch.setattr(restart_mod, "_soft_inject_bootstrap",
                        recording_inject)

    _run_bootstrap(env)

    assert ("test-session", "dev", "DEVELOPER") in inject_calls
    assert ("test-session", "planner", "ARCHITECT-PLANNER") in inject_calls


def test_bootstrap_soft_skips_inject_for_dead_pid(env, monkeypatch):
    """Dead-pid role goes through the existing dead-pid relaunch path
    (which always uses fresh prompt anyway). The soft-inject helper
    is for alive panes only — calling it on a dead pid would paste
    canon into a shell prompt, not an agent."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    # Pid 6666 NOT in env.alive → dead.
    _write_registry(env.project_dir, "developer", pid=6666, with_sock=True)

    inject_calls: list = []
    monkeypatch.setattr(
        restart_mod, "_soft_inject_bootstrap",
        lambda *a, **kw: (inject_calls.append(a) or (True, "stubbed")),
    )

    _run_bootstrap(env)

    assert inject_calls == [], (
        "--bootstrap must NOT call _soft_inject_bootstrap for dead pids"
    )


def test_bootstrap_soft_does_not_clear_session_files_dead_pid(env):
    """Dead-pid + --bootstrap (soft): session-id files are preserved.
    The next start-agent will --resume / codex resume into the prior
    conversation, which is what we want when the agent simply
    crashed. Use --reset if you want a genuine state-bust."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    _write_registry(env.project_dir, "developer", pid=6666, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    _run_bootstrap(env)

    assert claude_sid.is_file()
    assert codex_sid.is_file()


# ---------- --reset destructive path (0147, formerly 0137 --bootstrap) ----------


def test_reset_sigterms_alive_pid_and_sends_enter(env, monkeypatch):
    """--reset (formerly 0137 --bootstrap) SIGTERMs alive agents and
    re-launches via the dead-pid Enter path. Same destructive
    semantics as 0137 shipped — just under a different flag name."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(7777)
    _write_registry(env.project_dir, "developer", pid=7777, with_sock=True)

    kill_calls: list[tuple[int, int]] = []

    def recording_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0 and pid in env.alive:
            return
        if sig == 0:
            raise ProcessLookupError(pid)
        env.alive.discard(pid)
    monkeypatch.setattr(restart_mod.os, "kill", recording_kill)

    _run_reset(env)

    import signal
    assert (7777, signal.SIGTERM) in kill_calls
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys")
               if "-t" in c}
    assert "test-session:dev" in targets


def test_reset_unlinks_session_id_files_for_alive(env, monkeypatch):
    """--reset clears claude/codex session-id files for alive roles,
    so the post-kill re-launch goes through the fresh-session path."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(8888)
    _write_registry(env.project_dir, "developer", pid=8888, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    monkeypatch.setattr(restart_mod.os, "kill", lambda *_: None)
    _run_reset(env)

    assert not claude_sid.is_file()
    assert not codex_sid.is_file()


def test_reset_clears_session_files_for_dead_pid_too(env, monkeypatch):
    """--reset preserves the 0137-iter-2 semantics: dead-pid + --reset
    also clears session-id files so the relaunch is genuinely fresh
    (not a --resume into the stale conversation). This is the
    'fresh session for every role being relaunched' invariant from
    0137 iter-2 — moved under --reset by 0147."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    _write_registry(env.project_dir, "developer", pid=6666, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    monkeypatch.setattr(restart_mod.os, "kill",
                        lambda pid, sig: None if pid in env.alive
                        else (_ for _ in ()).throw(ProcessLookupError(pid)))
    _run_reset(env)

    assert not claude_sid.is_file()
    assert not codex_sid.is_file()


# ---------- mutual exclusion ----------


def test_bootstrap_and_reset_mutually_exclusive(env):
    """0147: --bootstrap and --reset name opposite intents (preserve
    vs drop session). Combining them is operator confusion; reject
    at click level with a clear UsageError so MAINTAINER picks one
    explicitly."""
    coord_yaml = env.project_dir / "coord.yaml"
    _write_coord_yaml(env.project_dir)
    result = CliRunner().invoke(
        restart_mod.restart,
        ["--config", str(coord_yaml), "--bootstrap", "--reset"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ---------- default-restart untouched-by-bootstrap pin ----------


def test_default_restart_does_not_touch_alive_session_files(env):
    """Negative pin: without --bootstrap or --reset, alive agents
    are skipped entirely — no SIGTERM, no session-id unlink, no
    send-keys, no soft inject. The default is idempotent."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.alive.add(9999)
    _write_registry(env.project_dir, "developer", pid=9999, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    _run(env)

    assert claude_sid.is_file()
    assert codex_sid.is_file()
    targets = {c[c.index("-t") + 1] for c in env.sub.find("send-keys")
               if "-t" in c}
    assert "test-session:dev" not in targets


def test_default_restart_preserves_session_files_for_dead_pid(env):
    """Negative pin: without --bootstrap/--reset, dead-pid path
    preserves session-id files. Crash recovery keeps claude --resume
    continuity — only --reset rotates the session."""
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    _write_registry(env.project_dir, "developer", pid=5555, with_sock=True)
    claude_sid, codex_sid = _seed_session_files(env.project_dir, "developer")

    _run(env)

    assert claude_sid.is_file()
    assert codex_sid.is_file()


# ---------- help text discoverability ----------


def test_bootstrap_help_mentions_soft_and_session_preservation(env):
    """The --bootstrap help text must surface (a) it's the post-PyPI-
    upgrade procedure, (b) it preserves sessions. MAINTAINER discovers
    the flag from `--help`, not from src."""
    result = CliRunner().invoke(restart_mod.restart, ["--help"])
    assert "--bootstrap" in result.output
    assert "upgrade" in result.output.lower() or "pip install -U" in result.output


def test_reset_help_mentions_destructive_and_session_drop(env):
    """The --reset help text must surface (a) it's destructive, (b)
    it's NOT the default upgrade procedure. Avoids MAINTAINER
    accidentally reaching for it."""
    result = CliRunner().invoke(restart_mod.restart, ["--help"])
    assert "--reset" in result.output
    out = result.output.lower()
    assert "destructive" in out or "fresh" in out


# ---------- _soft_inject_bootstrap helper ----------


def test_soft_inject_loads_buffer_pastes_and_submits(tmp_path, monkeypatch):
    """Pin the soft-inject mechanism: reads the static
    coordination/bootstrap.md, loads it into a tmux buffer,
    paste-buffer -p, then send-keys Enter. The -p (bracketed paste)
    flag is load-bearing — without it the multi-line prompt would
    submit fragments on every newline."""
    import subprocess as _sub

    coord = tmp_path / "coordination"
    coord.mkdir()
    (coord / "bootstrap.md").write_text(
        "BOOTSTRAP LINE 1\nBOOTSTRAP LINE 2\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _sub.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(restart_mod.subprocess, "run", fake_run)
    ok, diag = restart_mod._soft_inject_bootstrap(
        "test-session", "dev", "DEVELOPER", coord,
    )
    assert ok, diag
    # No render-role shell-out — the prompt comes from bootstrap.md.
    assert not any(c[:2] == ["greatminds", "render-role"] for c in calls)
    assert calls[0][:2] == ["tmux", "load-buffer"]
    paste_call = [c for c in calls if c[:2] == ["tmux", "paste-buffer"]][0]
    assert "-p" in paste_call, "bracketed paste -p flag required"
    assert "test-session:dev" in paste_call
    submit = [c for c in calls if c[:2] == ["tmux", "send-keys"]][0]
    assert submit[-1] == "Enter"


def test_soft_inject_returns_false_when_bootstrap_missing(tmp_path, monkeypatch):
    """A missing coordination/bootstrap.md must be non-fatal — caller
    logs the diag and continues. Don't crash --bootstrap on it."""
    import subprocess as _sub
    monkeypatch.setattr(
        restart_mod.subprocess, "run",
        lambda *a, **k: _sub.CompletedProcess([], 0, "", ""))
    coord = tmp_path / "coordination"
    coord.mkdir()  # no bootstrap.md inside
    ok, diag = restart_mod._soft_inject_bootstrap(
        "test-session", "dev", "DEVELOPER", coord,
    )
    assert ok is False
    assert "bootstrap.md unreadable" in diag
