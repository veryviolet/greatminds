"""Tests for `greatminds update`."""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from greatminds import __version__ as GM_VERSION
from greatminds.cli import update as upd


# ---------------------------------------------------------------------------
# Fakes: PyPI fetch, subprocess.run, os.execv
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pypi(monkeypatch):
    """Mock PyPI fetch to return a configurable latest version."""
    state = {"latest": "1.99.0"}

    def fake_fetch():
        if state["latest"] is None:
            raise click_exit_2("PyPI mock returned no version")
        return state["latest"]

    def click_exit_2(msg):
        import click as _c
        raise _c.exceptions.Exit(2)

    monkeypatch.setattr(upd, "_fetch_latest_pypi_version", fake_fetch)
    return state


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Capture all subprocess.run calls; default rc=0."""
    calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def fake_execv(monkeypatch):
    """Replace os.execv with a recorder that raises a sentinel exception so
    the test can verify the args without ACTUALLY replacing the process."""
    class ExecvCalled(Exception):
        pass

    calls: list[tuple] = []

    def fake(path, argv):
        calls.append((path, list(argv)))
        raise ExecvCalled

    monkeypatch.setattr(upd.os, "execv", fake)
    return SimpleNamespace(calls=calls, sentinel=ExecvCalled)


def _invoke(args: list[str]):
    return CliRunner().invoke(upd.update, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# --check / --dry-run
# ---------------------------------------------------------------------------


def test_check_when_newer_pypi_version_available(fake_pypi):
    fake_pypi["latest"] = "1.99.0"
    result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "current: greatminds" in result.output
    assert "latest on PyPI: 1.99.0" in result.output
    assert "would upgrade" in result.output


def test_check_already_up_to_date(fake_pypi):
    fake_pypi["latest"] = GM_VERSION
    result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_dry_run_is_alias_of_check(fake_pypi):
    fake_pypi["latest"] = "1.99.0"
    a = _invoke(["--check"])
    b = _invoke(["--dry-run"])
    # Both report the would-be upgrade.
    assert "would upgrade" in a.output
    assert "would upgrade" in b.output


def test_check_major_bump_without_flag_warns(fake_pypi):
    # Force a major-version bump (e.g. 1.x → 9.x).
    fake_pypi["latest"] = "9.0.0"
    result = _invoke(["--check"])
    assert result.exit_code == 0
    assert "would refuse: major bump" in result.output
    assert "--major" in result.output


def test_check_major_bump_with_flag_proceeds(fake_pypi):
    fake_pypi["latest"] = "9.0.0"
    result = _invoke(["--check", "--major"])
    assert result.exit_code == 0
    assert "would upgrade" in result.output


# ---------------------------------------------------------------------------
# Full update: pip + self-replace
# ---------------------------------------------------------------------------


def test_full_update_calls_pip_then_self_replaces(fake_pypi, fake_subprocess,
                                                    fake_execv,
                                                    monkeypatch):
    fake_pypi["latest"] = "1.99.0"

    # 0299: stub the env detector to ``venv`` so the legacy pip
    # path is exercised here. The new env branching (uv/poetry/pixi
    # /conda) is covered exhaustively by
    # ``test_update_env_branching_0299.py``; this test pins the
    # pip happy path that survives 0299 as the no-lockfile
    # fallback.
    from greatminds.core.env import EnvSetup
    monkeypatch.setattr(
        "greatminds.cli.update.detect_env_setup",
        lambda *_a, **_k: EnvSetup(
            env_type="venv", activation="", source="(test stub)",
        ),
        raising=False,
    )
    # The import path inside ``_step_pip_upgrade`` is the actual
    # call site; patch there too.
    from greatminds.core import env as _env
    monkeypatch.setattr(
        _env, "detect",
        lambda *_a, **_k: EnvSetup(
            env_type="venv", activation="", source="(test stub)",
        ),
    )

    with pytest.raises(fake_execv.sentinel):
        # The fake execv raises; we let the exception escape so the test
        # can inspect what happened up to that point.
        upd.update.callback(  # type: ignore[misc]
            post_pip=False, check=False, dry_run=False,
            major=True,  # accept the major bump for this test
            project_name=None,
        )

    # pip install command was issued (env=venv → legacy pip path).
    pip_calls = [c for c in fake_subprocess if "pip" in c and "install" in c]
    assert len(pip_calls) == 1
    assert "--upgrade" in pip_calls[0]
    assert "greatminds" in pip_calls[0]

    # os.execv was called exactly once with the freshly-installed binary.
    assert len(fake_execv.calls) == 1
    path, argv = fake_execv.calls[0]
    assert argv[-2:] == ["update", "--post-pip"]


def test_full_update_refuses_major_without_flag(fake_pypi, fake_subprocess,
                                                  fake_execv):
    fake_pypi["latest"] = "9.0.0"
    result = _invoke([])
    assert result.exit_code == 2
    assert "major upgrade" in result.output
    # pip + execv never invoked.
    assert not any("pip" in c and "install" in c for c in fake_subprocess)
    assert fake_execv.calls == []


def test_already_up_to_date_still_reconciles_config(fake_pypi, fake_subprocess,
                                                    fake_execv, monkeypatch):
    """1.5.10: when the package is already current, `update` no longer
    exits early — it still runs the config-migration + restart phase, so
    a stale project config (old coord.yaml etc.) is reconciled even when
    the package needs no bump. No package bump → no self-replace execv."""
    fake_pypi["latest"] = GM_VERSION
    monkeypatch.setattr(
        "greatminds.cli.daemon.detect_legacy_coordd", lambda: False)
    from greatminds.cli import update as _update_mod
    monkeypatch.setattr(
        _update_mod, "_resolve_session_from_coord_yaml", lambda: "greatminds")
    monkeypatch.setattr(_update_mod, "_tmux_session_present", lambda _s: True)

    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output
    # No bump → no os.execv self-replace...
    assert fake_execv.calls == []
    # ...but the config-reconcile phase STILL ran: daemon restart fired.
    daemon_calls = [c for c in fake_subprocess
                    if "daemon" in c and "restart" in c]
    assert len(daemon_calls) == 1


# ---------------------------------------------------------------------------
# --post-pip: migration + daemon + agents
# ---------------------------------------------------------------------------


def test_post_pip_invokes_daemon_restart_and_agent_restart(monkeypatch,
                                                            fake_subprocess):
    # No legacy coordd present.
    monkeypatch.setattr(
        "greatminds.cli.daemon.detect_legacy_coordd", lambda: False,
    )
    # 0299: agent restart now gates on tmux session presence.
    # Stub both helpers so the legacy assertion (restart subprocess
    # fires) continues to hold.
    from greatminds.cli import update as _update_mod
    monkeypatch.setattr(
        _update_mod, "_resolve_session_from_coord_yaml",
        lambda: "greatminds",
    )
    monkeypatch.setattr(
        _update_mod, "_tmux_session_present", lambda _s: True,
    )

    result = _invoke(["--post-pip"])
    assert result.exit_code == 0, result.output

    # daemon restart was called.
    daemon_calls = [c for c in fake_subprocess
                    if "daemon" in c and "restart" in c]
    assert len(daemon_calls) == 1

    # tmux restart (top-level `greatminds restart`) was called.
    restart_calls = [c for c in fake_subprocess
                     if c and c[-1] == "restart"
                     and "daemon" not in c]
    assert len(restart_calls) == 1


def test_post_pip_migrates_legacy_coordd_when_detected(monkeypatch,
                                                       fake_subprocess):
    monkeypatch.setattr(
        "greatminds.cli.daemon.detect_legacy_coordd", lambda: True,
    )
    result = _invoke(["--post-pip"])
    assert result.exit_code == 0, result.output
    # systemctl stop/disable issued against coordd.service.
    flat = [tuple(c) for c in fake_subprocess]
    assert any("coordd.service" in c and "stop" in c for c in flat)
    assert any("coordd.service" in c and "disable" in c for c in flat)


def test_post_pip_with_explicit_project_name_passes_flag(monkeypatch,
                                                          fake_subprocess):
    monkeypatch.setattr(
        "greatminds.cli.daemon.detect_legacy_coordd", lambda: False,
    )
    result = _invoke(["--post-pip", "--project", "myproj"])
    assert result.exit_code == 0
    daemon_calls = [c for c in fake_subprocess
                    if "daemon" in c and "restart" in c]
    assert len(daemon_calls) == 1
    assert "--project" in daemon_calls[0]
    assert "myproj" in daemon_calls[0]


def test_post_pip_daemon_restart_failure_exits_nonzero(monkeypatch,
                                                        fake_subprocess):
    """If `daemon restart` fails, update propagates the rc with a recovery hint."""
    monkeypatch.setattr(
        "greatminds.cli.daemon.detect_legacy_coordd", lambda: False,
    )

    real_runs: list[list[str]] = fake_subprocess

    def selective_run(cmd, *_a, **_kw):
        real_runs.append(list(cmd))
        # First daemon restart fails; everything else succeeds.
        if "daemon" in cmd and "restart" in cmd:
            return subprocess.CompletedProcess(list(cmd), 3, "", "")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(upd.subprocess, "run", selective_run)
    result = _invoke(["--post-pip"])
    assert result.exit_code == 3
    assert "daemon restart failed" in result.output


# ---------------------------------------------------------------------------
# PyPI unreachable
# ---------------------------------------------------------------------------


def test_check_pypi_unreachable_clean_error(monkeypatch):
    def boom():
        import click as _c
        # Simulate _fetch_latest_pypi_version's own error handling path.
        import greatminds.cli._colors as _co
        _co.err("could not reach PyPI: simulated network failure")
        raise _c.exceptions.Exit(2)

    monkeypatch.setattr(upd, "_fetch_latest_pypi_version", boom)
    result = _invoke(["--check"])
    assert result.exit_code == 2
    assert "could not reach PyPI" in result.output


# ---------------------------------------------------------------------------
# _parse_semver corner cases (internal helper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("v,expected", [
    ("1.2.3", (1, 2, 3)),
    ("1.2.3-rc1", (1, 2, 3)),
    ("1.2.3+build.5", (1, 2, 3)),
    ("2.0", (2, 0, 0)),
    ("9", (9, 0, 0)),
    ("garbage", (0, 0, 0)),
])
def test_parse_semver(v, expected):
    assert upd._parse_semver(v) == expected
