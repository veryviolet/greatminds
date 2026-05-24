"""Tests for coordd's new --project flag and .daemon_version marker."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds import __version__ as GM_VERSION
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
    """Cut the coordd main loop short — we only care about start-up effects
    (.daemon_version write + project resolution). Patch the signal install
    so the test process isn't trapped, and force the first iteration to set
    the stop flag."""
    import signal

    monkeypatch.setattr(signal, "signal", lambda *_a, **_kw: None)

    real_sleep = coordd_mod.time.sleep

    iterations = {"n": 0}

    def fake_sleep(_secs):
        iterations["n"] += 1
        if iterations["n"] > 0:
            # First sleep call → flip the stop flag set by the SIGINT handler
            # closure in the coordd loop. We achieve that by raising
            # KeyboardInterrupt, which click catches and turns into a clean
            # exit. We DO want the coordd function to have run far enough to
            # write the .daemon_version marker by this point.
            raise KeyboardInterrupt

    monkeypatch.setattr(coordd_mod.time, "sleep", fake_sleep)


def test_coordd_writes_daemon_version_on_startup(tmp_path, monkeypatch):
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
    version_file = coord / ".daemon_version"
    assert version_file.is_file(), "coordd must write .daemon_version on startup"
    assert version_file.read_text(encoding="utf-8").strip() == GM_VERSION


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
    # Resolution succeeded → .daemon_version landed in the registered dir.
    version_file = project_dir / "coordination" / ".daemon_version"
    assert version_file.is_file()
    assert version_file.read_text(encoding="utf-8").strip() == GM_VERSION


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
