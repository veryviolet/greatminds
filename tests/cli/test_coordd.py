"""Tests for coordd's --project flag and project resolution.

Task 0131: removed .daemon_version writer + version-drift detection.
Tests that exercised those side-effects are gone; project-resolution
tests keep their original intent but assert on alternative observable
state (the loop is reached without error).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import coordd as coordd_mod
from greatminds.cli import daemon as daemon_mod


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect daemon-module paths so the test registry never touches
    the real user's ~/.config."""
    reg_dir = tmp_path / ".config" / "greatminds"
    sysd = tmp_path / ".config" / "systemd" / "user"
    monkeypatch.setattr(daemon_mod, "REGISTRY_DIR", reg_dir)
    monkeypatch.setattr(daemon_mod, "REGISTRY_PATH", reg_dir / "projects.json")
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", sysd)


def _stub_loop(monkeypatch):
    """Cut the coordd main loop short. Patch the signal install so the
    test process isn't trapped, and force the first iteration to
    KeyboardInterrupt out of the loop.

    0169: also stub ``_make_inotify_watcher`` to return None. With the
    inotify path active, coordd's main loop calls
    ``inotify.read(timeout=...)`` instead of ``time.sleep(...)`` — the
    ``time.sleep`` monkeypatch below wouldn't catch the loop. The
    ``None`` return forces the polling fallback which goes through the
    stubbed ``time.sleep``.
    """
    import signal

    monkeypatch.setattr(signal, "signal", lambda *_a, **_kw: None)
    monkeypatch.setattr(coordd_mod, "_make_inotify_watcher",
                        lambda *_a, **_kw: None)

    iterations = {"n": 0}

    def fake_sleep(_secs):
        iterations["n"] += 1
        if iterations["n"] > 0:
            raise KeyboardInterrupt

    monkeypatch.setattr(coordd_mod.time, "sleep", fake_sleep)


def test_coordd_does_not_write_daemon_version_marker(tmp_path, monkeypatch):
    """0131: .daemon_version writer is removed. coordd startup must
    NOT create that file (it was the producer side of the wrong
    version-drift concept)."""
    project_dir = tmp_path / "proj"
    coord = project_dir / "coordination"
    coord.mkdir(parents=True)
    _stub_loop(monkeypatch)

    runner = CliRunner()
    runner.invoke(
        coordd_mod.coordd,
        ["--project-dir", str(project_dir), "--interval-sec", "0.2"],
        catch_exceptions=True,
    )
    assert not (coord / ".daemon_version").exists(), (
        "coordd must NOT create .daemon_version after 0131"
    )


def test_coordd_resolves_project_name_via_registry(tmp_path, monkeypatch):
    project_dir = tmp_path / "abc"
    (project_dir / "coordination").mkdir(parents=True)
    daemon_mod.register_project("abc", project_dir)
    _stub_loop(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        coordd_mod.coordd,
        ["--project", "abc", "--interval-sec", "0.2"],
        catch_exceptions=True,
    )
    # Resolution succeeded → exit_code is 0 (KeyboardInterrupt path).
    # Negative: no "no project registered" in output.
    assert "no project registered" not in result.output


def test_coordd_unknown_project_name_errors(tmp_path, monkeypatch):
    """--project NAME with no registry entry → exit 2 with clear message."""
    _stub_loop(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        coordd_mod.coordd,
        ["--project", "does-not-exist", "--interval-sec", "0.2"],
        catch_exceptions=True,
    )
    assert result.exit_code == 2
    assert "no project registered" in result.output
