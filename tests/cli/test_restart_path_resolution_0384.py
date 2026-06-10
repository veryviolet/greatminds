"""Tests for task 0384: ``greatminds restart`` must not shell out to a
bare ``greatminds`` that depends on ambient PATH.

Repro (avatar, non-interactive SSH): ``restart`` was invoked through an
explicit venv binary (``.venv-coord/bin/greatminds``) whose bin dir was
NOT on PATH. When the tmux session was missing it subprocessed bare
``["greatminds", "launch", ...]`` → ``FileNotFoundError: 'greatminds'``,
breaking the very recovery path restart is supposed to provide.

Fix: ``_greatminds_cmd()`` resolves the sibling executable next to the
running interpreter (``sys.executable``'s bin dir); failing that it falls
back to ``[sys.executable, "-m", "greatminds.cli.main"]`` — an absolute
interpreter path that never depends on PATH.
"""
from __future__ import annotations

import os
import sys

import pytest
from click.testing import CliRunner

from test_restart import _write_coord_yaml  # noqa: E402  (sibling on sys.path)
from greatminds.cli import restart as restart_mod


# ---------------------------------------------------------------------------
# _greatminds_cmd unit behavior
# ---------------------------------------------------------------------------


def test_greatminds_cmd_prefers_executable_sibling(tmp_path, monkeypatch):
    """When a ``greatminds`` exists next to the running interpreter and is
    executable, use that absolute path (no PATH lookup)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python"
    py.write_text("#!/bin/sh\n")
    sibling = bindir / "greatminds"
    sibling.write_text("#!/bin/sh\n")
    os.chmod(sibling, 0o755)
    monkeypatch.setattr(restart_mod.sys, "executable", str(py))

    assert restart_mod._greatminds_cmd() == [str(sibling)]


def test_greatminds_cmd_falls_back_to_module_when_sibling_absent(
    tmp_path, monkeypatch,
):
    """No sibling executable → fall back to ``python -m greatminds.cli.main``
    using the absolute interpreter path. This never depends on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python"
    py.write_text("#!/bin/sh\n")
    # NOTE: no `greatminds` file created next to it.
    monkeypatch.setattr(restart_mod.sys, "executable", str(py))

    assert restart_mod._greatminds_cmd() == [
        str(py), "-m", "greatminds.cli.main",
    ]


def test_greatminds_cmd_ignores_non_executable_sibling(tmp_path, monkeypatch):
    """A sibling file that is NOT executable must be ignored (could be a
    stray non-script); fall through to the module form."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python"
    py.write_text("#!/bin/sh\n")
    sibling = bindir / "greatminds"
    sibling.write_text("not a program\n")
    os.chmod(sibling, 0o644)  # readable, NOT executable
    monkeypatch.setattr(restart_mod.sys, "executable", str(py))

    assert restart_mod._greatminds_cmd() == [
        str(py), "-m", "greatminds.cli.main",
    ]


# ---------------------------------------------------------------------------
# Integration: the launch subprocess never invokes a bare "greatminds".
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    from test_restart import FakeSubprocess
    fake = FakeSubprocess()
    monkeypatch.setattr(restart_mod.subprocess, "run", fake)
    monkeypatch.setattr(restart_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(restart_mod.os, "kill",
                        lambda pid, sig: None)
    fake.set(("systemctl", "--user", "is-active"), rc=0)
    fake.set(("systemctl", "--user", "show"), rc=0, stdout="123\n")
    fake.set(("tmux", "send-keys"), rc=0)

    class E:
        pass
    e = E()
    e.sub = fake
    e.project_dir = tmp_path
    return e


def _run(env_):
    coord_yaml = env_.project_dir / "coord.yaml"
    return CliRunner().invoke(
        restart_mod.restart, ["--config", str(coord_yaml)],
        catch_exceptions=False,
    )


def test_launch_subprocess_uses_path_independent_argv(env, monkeypatch):
    """When the tmux session is missing, restart subprocesses launch with
    the PATH-independent prefix — argv[0] is never the bare ``greatminds``.

    Simulate the PATH-stripped avatar case: no sibling executable resolves,
    so the command must be ``[sys.executable, "-m", "greatminds.cli.main"]``.
    """
    _write_coord_yaml(env.project_dir,
                      windows=[{"name": "dev", "role": "DEVELOPER",
                                "tool": "claude"}])
    env.sub.set(("tmux", "has-session"), rc=1)  # session missing → launch
    # Force the module fallback: point sys.executable at a bin dir with no
    # greatminds sibling (mirrors a venv whose bin isn't on PATH but whose
    # python is the running interpreter).
    nopath_bin = env.project_dir / "nopath_bin"
    nopath_bin.mkdir()
    py = nopath_bin / "python"
    py.write_text("#!/bin/sh\n")
    monkeypatch.setattr(restart_mod.sys, "executable", str(py))

    _run(env)

    launches = [c for c in env.sub.calls
                if "launch" in c and "--target" in c and "tmux" in c]
    assert len(launches) == 1, env.sub.calls
    cmd = launches[0]
    # argv[0] is the absolute interpreter, NOT bare "greatminds".
    assert cmd[0] == str(py)
    assert cmd[:3] == [str(py), "-m", "greatminds.cli.main"]
    assert "greatminds" not in cmd[:1], (
        "0384: launch must not depend on a bare `greatminds` on PATH")
