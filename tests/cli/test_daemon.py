"""Tests for `greatminds daemon` (template unit + per-project registry)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import daemon as daemon_mod


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect REGISTRY_PATH and SYSTEMD_USER_DIR to tmp_path so tests
    never touch the real user's ~/.config."""
    reg_dir = tmp_path / ".config" / "greatminds"
    sysd = tmp_path / ".config" / "systemd" / "user"
    monkeypatch.setattr(daemon_mod, "REGISTRY_DIR", reg_dir)
    monkeypatch.setattr(daemon_mod, "REGISTRY_PATH", reg_dir / "projects.json")
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", sysd)
    return tmp_path


@pytest.fixture
def fake_systemctl(monkeypatch):
    """Capture every `systemctl --user ...` call. Default returns rc=0;
    tests override per-call by appending to handlers."""
    calls: list[list[str]] = []
    handlers: list = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        for matcher, handler in reversed(handlers):
            if matcher(cmd):
                return handler(cmd)
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)

    def set_handler(prefix, rc=0, stdout="", stderr=""):
        prefix = tuple(prefix)

        def matcher(cmd, _pfx=prefix):
            return tuple(cmd[: len(_pfx)]) == _pfx

        cp = subprocess.CompletedProcess(list(prefix), rc, stdout, stderr)
        handlers.append((matcher, lambda _c, _cp=cp: _cp))

    ns = type("FakeSys", (), {})()
    ns.calls = calls
    ns.set = set_handler  # assign as instance attribute — no method binding
    return ns


def _invoke(args: list[str]):
    return CliRunner().invoke(
        daemon_mod.daemon, args, catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_writes_template_unit_and_registry_entry(_isolate_paths,
                                                          fake_systemctl,
                                                          tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "alpha", "windows": []}), encoding="utf-8")
    # no legacy coordd present
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=1)

    result = _invoke(["install", "--project-dir", str(project_dir)])
    assert result.exit_code == 0, result.output

    unit_path = daemon_mod.SYSTEMD_USER_DIR / daemon_mod.TEMPLATE_UNIT_NAME
    assert unit_path.is_file()
    body = unit_path.read_text(encoding="utf-8")
    assert "[Unit]" in body
    assert "%i" in body

    reg = json.loads(daemon_mod.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert reg == {"alpha": str(project_dir.resolve())}
    # daemon-reload should have been invoked for a new unit.
    assert any(c[:3] == ["systemctl", "--user", "daemon-reload"]
               for c in fake_systemctl.calls)


def test_install_is_idempotent(_isolate_paths, fake_systemctl, tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "alpha", "windows": []}), encoding="utf-8")
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=1)

    r1 = _invoke(["install", "--project-dir", str(project_dir)])
    r2 = _invoke(["install", "--project-dir", str(project_dir)])
    assert r1.exit_code == 0 == r2.exit_code
    # Registry has single entry.
    reg = json.loads(daemon_mod.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert reg == {"alpha": str(project_dir.resolve())}


def test_install_refuses_when_legacy_coordd_present(_isolate_paths,
                                                     fake_systemctl,
                                                     tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "x", "windows": []}), encoding="utf-8")
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=0)  # legacy present

    result = _invoke(["install", "--project-dir", str(project_dir)])
    assert result.exit_code == 2
    assert "legacy" in result.output.lower()
    # Registry NOT touched.
    assert not daemon_mod.REGISTRY_PATH.is_file()


def test_install_errors_if_name_unresolvable(_isolate_paths, fake_systemctl,
                                              tmp_path):
    """No --name and no coord.yaml → clear error, exit 2."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()  # no coord.yaml inside
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=1)

    result = _invoke(["install", "--project-dir", str(project_dir)])
    assert result.exit_code == 2
    assert "session" in result.output.lower()


# ---------------------------------------------------------------------------
# start/stop/restart with project resolution
# ---------------------------------------------------------------------------


def test_start_with_explicit_project_calls_systemctl(_isolate_paths,
                                                      fake_systemctl):
    result = _invoke(["start", "--project", "foo"])
    assert result.exit_code == 0
    starts = [c for c in fake_systemctl.calls
              if c[:3] == ["systemctl", "--user", "start"]]
    assert len(starts) == 1
    assert "greatminds-daemon@foo.service" in starts[0]


def test_start_reads_project_name_from_coord_yaml(_isolate_paths,
                                                   fake_systemctl,
                                                   tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "from-yaml", "windows": []}),
        encoding="utf-8")
    result = _invoke(["start", "--project-dir", str(project_dir)])
    assert result.exit_code == 0
    starts = [c for c in fake_systemctl.calls
              if c[:3] == ["systemctl", "--user", "start"]]
    assert any("greatminds-daemon@from-yaml.service" in c for c in starts)


def test_restart_invokes_systemctl_restart(_isolate_paths, fake_systemctl):
    result = _invoke(["restart", "--project", "alpha"])
    assert result.exit_code == 0
    assert any(
        c[:3] == ["systemctl", "--user", "restart"]
        and "greatminds-daemon@alpha.service" in c
        for c in fake_systemctl.calls
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_each_registered_project(_isolate_paths, fake_systemctl,
                                              tmp_path):
    daemon_mod.register_project("alpha", tmp_path / "a")
    daemon_mod.register_project("beta", tmp_path / "b")
    fake_systemctl.set(
        ("systemctl", "--user", "is-active",
         "greatminds-daemon@alpha.service"),
        rc=0, stdout="active\n",
    )
    fake_systemctl.set(
        ("systemctl", "--user", "is-active",
         "greatminds-daemon@beta.service"),
        rc=3, stdout="inactive\n",
    )
    result = _invoke(["list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "active" in result.output
    assert "inactive" in result.output


def test_list_when_empty_registry(_isolate_paths, fake_systemctl):
    result = _invoke(["list"])
    assert result.exit_code == 0
    assert "no projects registered" in result.output


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def test_migrate_without_yes_refuses(_isolate_paths, fake_systemctl):
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=0)
    result = _invoke(["migrate"])
    assert result.exit_code == 2
    assert "--yes" in result.output


def test_migrate_with_yes_disables_and_removes_legacy(_isolate_paths,
                                                       fake_systemctl):
    # Pretend legacy unit file exists on disk too.
    legacy_path = daemon_mod.SYSTEMD_USER_DIR / daemon_mod.LEGACY_UNIT_NAME
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("legacy stub\n", encoding="utf-8")
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=0)

    result = _invoke(["migrate", "--yes"])
    assert result.exit_code == 0, result.output
    # File removed.
    assert not legacy_path.is_file()
    # stop + disable + daemon-reload all issued.
    invoked = {tuple(c[:4]) for c in fake_systemctl.calls}
    assert ("systemctl", "--user", "stop", "coordd.service") in invoked
    assert ("systemctl", "--user", "disable", "coordd.service") in invoked


def test_migrate_when_no_legacy_present_short_circuits(_isolate_paths,
                                                       fake_systemctl):
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=1)
    result = _invoke(["migrate", "--yes"])
    assert result.exit_code == 0
    assert "nothing to migrate" in result.output


# ---------------------------------------------------------------------------
# Helpers (direct API)
# ---------------------------------------------------------------------------


def test_install_template_unit_returns_false_on_existing_file(_isolate_paths):
    daemon_mod.install_template_unit()
    # Second call: already present.
    assert daemon_mod.install_template_unit() is False


def test_register_and_lookup_project_roundtrip(_isolate_paths, tmp_path):
    daemon_mod.register_project("zeta", tmp_path / "z")
    assert daemon_mod.lookup_project_dir("zeta") == tmp_path / "z"
    assert daemon_mod.lookup_project_dir("missing") is None
