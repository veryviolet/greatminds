"""Tests for task 0308: launch + restart send the full
``greatminds start-agent`` command directly into each tmux pane
instead of installing the legacy bash wrapper-loop.

Pre-0308 ``_emit_tmux`` injected a one-liner that printed
``press Enter to (re)start <ROLE>...`` and blocked on
``read -r _`` — the operator had to press Enter once per pane to
actually start agents. ``restart`` then relied on bare Enter
nudging the wrapper into the next iteration. 0308 replaces both
with direct send-keys of the launch command + Enter, prefixed
with ``C-u`` to clear any stray bash input.

The legacy ``_wrapper_loop`` function + ``CIRCUIT_BREAKER_*``
constants are retained as dormant symbols so existing 0160/0164
tests + external scripts that imported them don't break; future
work moves circuit-breaker semantics to ``restart.py`` /
watchdog (per-role attempt-count tracking across invocations).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from greatminds.cli import launch as launch_mod
from greatminds.cli import restart as restart_mod


def _env_setup() -> launch_mod.gm_env.EnvSetup:
    return launch_mod.gm_env.EnvSetup(
        env_type=None, activation="", source="(test)")


@pytest.fixture
def fake_tmux(monkeypatch):
    """Trace every tmux send-keys / new-session / new-window /
    select-window the launcher issues."""
    calls: list[list[str]] = []

    def fake_run(args, capture_output=False, **kw):
        calls.append(list(args))
        # ``has-session`` returns nonzero (no existing session);
        # everything else succeeds.
        if "has-session" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(launch_mod.subprocess, "run", fake_run)
    return [c for c in calls if c and c[0] == "tmux"]


# ---------- launch: no wrapper, direct launch command ----------


def test_launch_sends_launch_command_directly(
    tmp_path: Path, monkeypatch,
) -> None:
    """``_emit_tmux`` must send ``greatminds start-agent DEVELOPER
    claude --mode loop`` + Enter directly. No wrapper one-liner."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )

    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    send_keys = [c for c in calls
                 if c and c[0] == "tmux" and c[1] == "send-keys"]
    launch_calls = [c for c in send_keys
                    if any("greatminds start-agent" in str(a)
                           for a in c)]
    assert len(launch_calls) == 1
    # The actual command string (not wrapped) appears as one arg.
    cmd_arg = next(
        (a for a in launch_calls[0]
         if isinstance(a, str) and "greatminds start-agent" in a),
        None,
    )
    assert cmd_arg == (
        "greatminds start-agent DEVELOPER claude --mode loop"
    ), f"0308: unexpected launch_cmd shape: {cmd_arg!r}"


def test_launch_sends_clear_line_before_command(
    tmp_path: Path, monkeypatch,
) -> None:
    """``C-u`` (clear line) must fire BEFORE the launch command so
    leftover bash input doesn't concatenate with the command."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )

    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)

    send_keys = [c for c in calls
                 if c and c[0] == "tmux" and c[1] == "send-keys"]
    cu_idx = next(
        (i for i, c in enumerate(send_keys) if "C-u" in c),
        None,
    )
    cmd_idx = next(
        (i for i, c in enumerate(send_keys)
         if any("greatminds start-agent" in str(a) for a in c)),
        None,
    )
    assert cu_idx is not None, "0308: missing C-u clear-line"
    assert cmd_idx is not None
    assert cu_idx < cmd_idx, (
        f"0308: C-u clear must precede launch command "
        f"(cu_idx={cu_idx}, cmd_idx={cmd_idx})"
    )


def test_launch_no_wrapper_in_send_keys(
    tmp_path: Path, monkeypatch,
) -> None:
    """Negative pin: no send-keys may carry the legacy
    ``while true; do … done`` wrapper after 0308."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )

    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    send_keys = [c for c in calls
                 if c and c[0] == "tmux" and c[1] == "send-keys"]
    for c in send_keys:
        for arg in c:
            if isinstance(arg, str):
                assert "while true" not in arg, (
                    f"0308: wrapper-loop must NOT appear in any "
                    f"send-keys arg. Got: {arg}"
                )


# ---------- dashboard window auto-runs `greatminds dashboard` ----------


def test_launch_dashboard_window_runs_dashboard(
    tmp_path: Path, monkeypatch,
) -> None:
    """A coord.yaml window with mode:dashboard (role-less bash pane)
    must auto-run `greatminds dashboard` + Enter — not sit as a plain
    shell."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )
    cfg = {
        "session": "test",
        "windows": [
            {"name": "dashboard", "role": "", "tool": "bash",
             "mode": "dashboard"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    send_keys = [c for c in calls
                 if c and c[0] == "tmux" and c[1] == "send-keys"]
    dash = [c for c in send_keys
            if any(a == "greatminds dashboard" for a in c
                   if isinstance(a, str))]
    assert len(dash) == 1, "dashboard window must run `greatminds dashboard`"
    assert "Enter" in dash[0], "dashboard command must be submitted (Enter)"
    # Role-less pane → no GREATMINDS_ROLE export.
    for c in send_keys:
        for a in c:
            if isinstance(a, str):
                assert not a.startswith("export GREATMINDS_ROLE"), \
                    "dashboard pane is role-less; must not export a role"


# ---------- status-line config (title fits, fleet colors) ----------


def test_launch_configures_status_line_length_and_colors(
    tmp_path: Path, monkeypatch,
) -> None:
    """Launch must set per-session tmux status options so the session
    title fits (no overlap with the window list) and the fleet colors
    are applied even without a global ~/.tmux.conf."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )
    cfg = {
        "session": "greatminds-dev",
        "windows": [
            {"name": "planner", "role": "ARCHITECT-PLANNER",
             "tool": "codex", "mode": "chat"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    set_opts = [c for c in calls
                if c and c[0] == "tmux" and c[1] == "set-option"]

    def opt(name):
        for c in set_opts:
            if name in c:
                return c[c.index(name) + 1]
        return None

    # length must fit "[greatminds-dev] " (17) → len(session)+4 = 18.
    assert opt("status-left-length") == str(len("greatminds-dev") + 4)
    assert opt("status-left") == "[#S] "
    assert opt("status-style") == "bg=colour54 fg=white"
    assert "bold,underscore" in (opt("window-status-current-style") or "")


def test_status_line_length_scales_with_session_name(
    tmp_path: Path, monkeypatch,
) -> None:
    """A longer session name gets a proportionally longer left-length so
    the title never truncates."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launch_mod.subprocess, "run",
        lambda args, **kw: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args,
                returncode=1 if "has-session" in args else 0,
                stdout="", stderr="")
        ),
    )
    cfg = {
        "session": "a-much-longer-fleet-name",
        "windows": [{"name": "p", "role": "ARCHITECT-PLANNER",
                     "tool": "codex", "mode": "chat"}],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _env_setup(), recreate=False)
    set_opts = [c for c in calls
                if c and c[0] == "tmux" and c[1] == "set-option"]
    length = next((c[c.index("status-left-length") + 1] for c in set_opts
                   if "status-left-length" in c), None)
    assert length == str(len("a-much-longer-fleet-name") + 4)


# ---------- restart sends full launch command, not bare Enter ----------


def test_launch_command_builder_is_stable() -> None:
    """``_launch_command`` is the shared builder used by both
    launch.py and restart.py. Pin the output shape so they stay in
    sync."""
    assert launch_mod._launch_command(
        "greatminds start-agent", "DEVELOPER", "claude", "loop",
    ) == "greatminds start-agent DEVELOPER claude --mode loop"
    # No --mode flag when mode is empty.
    assert launch_mod._launch_command(
        "greatminds start-agent", "PLANNER", "claude", "",
    ) == "greatminds start-agent PLANNER claude"


def test_legacy_wrapper_loop_function_still_importable() -> None:
    """0308 transition aid: the deprecated ``_wrapper_loop``
    function + CIRCUIT_BREAKER constants remain importable so
    legacy fixtures don't break. ``_emit_tmux`` no longer calls
    them."""
    assert hasattr(launch_mod, "_wrapper_loop")
    assert launch_mod.CIRCUIT_BREAKER_FAILS == 3
    assert launch_mod.CIRCUIT_BREAKER_WINDOW_SEC == 30
