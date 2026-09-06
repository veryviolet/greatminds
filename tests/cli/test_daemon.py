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
    _configured(project_dir)
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
    assert reg == {"proj": str(project_dir.resolve())}
    # daemon-reload should have been invoked for a new unit.
    assert any(c[:3] == ["systemctl", "--user", "daemon-reload"]
               for c in fake_systemctl.calls)


def test_install_is_idempotent(_isolate_paths, fake_systemctl, tmp_path):
    project_dir = tmp_path / "proj"
    _configured(project_dir)
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "alpha", "windows": []}), encoding="utf-8")
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=1)

    r1 = _invoke(["install", "--project-dir", str(project_dir)])
    r2 = _invoke(["install", "--project-dir", str(project_dir)])
    assert r1.exit_code == 0 == r2.exit_code
    # Registry has single entry.
    reg = json.loads(daemon_mod.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert reg == {"proj": str(project_dir.resolve())}




def test_install_uses_directory_without_coord_yaml(_isolate_paths, fake_systemctl,
                                              tmp_path):
    """A fresh project does not need the deleted native window contract."""
    project_dir = tmp_path / "proj"
    _configured(project_dir)  # no coord.yaml inside
    fake_systemctl.set(("systemctl", "--user", "is-enabled", "coordd.service"),
                       rc=1)

    result = _invoke(["install", "--project-dir", str(project_dir)])
    assert result.exit_code == 0
    assert daemon_mod.lookup_project_dir("proj") == project_dir


# ---------------------------------------------------------------------------
# start/stop/restart with project resolution
# ---------------------------------------------------------------------------


def test_start_with_explicit_project_calls_systemctl(_isolate_paths,
                                                      fake_systemctl, tmp_path):
    _configured(tmp_path, "foo")
    result = _invoke(["start", "--project", "foo"])
    assert result.exit_code == 0
    starts = [c for c in fake_systemctl.calls
              if c[:3] == ["systemctl", "--user", "start"]]
    assert len(starts) == 1
    assert "greatminds-daemon@foo.service" in starts[0]


def test_start_ignores_native_coord_yaml_session(_isolate_paths,
                                                   fake_systemctl,
                                                   tmp_path):
    project_dir = tmp_path / "proj"
    _configured(project_dir)
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "from-yaml", "windows": []}),
        encoding="utf-8")
    daemon_mod.register_project("proj", project_dir)
    result = _invoke(["start", "--project-dir", str(project_dir)])
    assert result.exit_code == 0
    starts = [c for c in fake_systemctl.calls
              if c[:3] == ["systemctl", "--user", "start"]]
    assert any("greatminds-daemon@proj.service" in c for c in starts)


def test_restart_invokes_systemctl_restart(_isolate_paths, fake_systemctl, tmp_path):
    _configured(tmp_path, "alpha")
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


# ---------------------------------------------------------------------------
# task 0030: install resolves greatminds binary from the running env
# ---------------------------------------------------------------------------


def test_install_template_unit_uses_resolved_greatminds_path(_isolate_paths,
                                                              monkeypatch):
    """Reviewer-flagged scenario: a per-project venv install puts greatminds
    at <project>/.venv/bin/greatminds, NOT at ~/.local/bin/greatminds.
    The unit file ExecStart must match whatever shutil.which returns at
    install time, not a hardcoded canon path."""
    fake_path = "/tmp/some-toy-project/.venv/bin/greatminds"
    monkeypatch.setattr(
        daemon_mod.shutil, "which",
        lambda name: fake_path if name == "greatminds" else None,
    )

    daemon_mod.install_template_unit()

    body = (daemon_mod.SYSTEMD_USER_DIR / daemon_mod.TEMPLATE_UNIT_NAME).read_text(encoding="utf-8")
    assert "__GREATMINDS_BIN__" not in body, "placeholder must be substituted"
    assert f"ExecStart={fake_path} coordd --project %i" in body
    assert "%h/.local/bin/greatminds" not in body, \
        "stale hardcoded path must be gone"


def test_install_template_unit_falls_back_to_python_module(_isolate_paths,
                                                            monkeypatch):
    """When `greatminds` is not on PATH, fall back to `<py> -m greatminds.cli.main`."""
    monkeypatch.setattr(daemon_mod.shutil, "which", lambda name: None)
    fake_py = "/opt/some/bin/python"
    monkeypatch.setattr(daemon_mod.sys, "executable", fake_py)

    daemon_mod.install_template_unit()

    body = (daemon_mod.SYSTEMD_USER_DIR / daemon_mod.TEMPLATE_UNIT_NAME).read_text(encoding="utf-8")
    assert f"ExecStart={fake_py} -m greatminds.cli.main coordd --project %i" in body


def test_install_template_unit_overwrites_stale_path(_isolate_paths, monkeypatch):
    """Re-running install from a NEW venv should refresh the unit body —
    otherwise the daemon keeps pointing at the old (now-missing) binary."""
    # First install from venv A
    monkeypatch.setattr(daemon_mod.shutil, "which",
                        lambda name: "/old/venv/bin/greatminds")
    daemon_mod.install_template_unit()
    unit_path = daemon_mod.SYSTEMD_USER_DIR / daemon_mod.TEMPLATE_UNIT_NAME
    assert "/old/venv/bin/greatminds" in unit_path.read_text()

    # Second install from venv B
    monkeypatch.setattr(daemon_mod.shutil, "which",
                        lambda name: "/new/venv/bin/greatminds")
    wrote = daemon_mod.install_template_unit()
    assert wrote is True, "second install must rewrite when ExecStart differs"
    body = unit_path.read_text()
    assert "/new/venv/bin/greatminds" in body
    assert "/old/venv/bin/greatminds" not in body


def test_install_template_unit_idempotent_same_path(_isolate_paths, monkeypatch):
    """Same-venv re-install must NOT rewrite (no daemon-reload churn)."""
    monkeypatch.setattr(daemon_mod.shutil, "which",
                        lambda name: "/same/venv/bin/greatminds")
    assert daemon_mod.install_template_unit() is True   # first call writes
    assert daemon_mod.install_template_unit() is False  # second is no-op


def test_install_manages_only_common_daemon_even_with_old_vendor_config(fake_systemctl, tmp_path):
    project = tmp_path/'project'
    _configured(project)
    (project/'coord.yaml').write_text(yaml.safe_dump({'session': 'acp-only', 'windows': [
        {'role': 'DEVELOPER', 'tool': 'codex', 'mode': 'driven'}]}))
    result = _invoke(['install', '--project-dir', str(project)])
    assert result.exit_code == 0, result.output
    service_calls = [c for c in fake_systemctl.calls if c[:2] == ['systemctl', '--user']]
    assert service_calls == [
        ['systemctl', '--user', 'daemon-reload'],
        ['systemctl', '--user', 'enable', 'greatminds-daemon@project.service'],
    ]
    units = list(daemon_mod.SYSTEMD_USER_DIR.glob('*.service'))
    assert [p.name for p in units] == ['greatminds-daemon@.service']
    assert ' coordd --project %i' in units[0].read_text()
    assert 'migrate' not in daemon_mod.daemon.commands
    assert not hasattr(daemon_mod, 'install_appserver_unit')


def test_registered_identity_survives_directory_basename_and_nested_cwd(tmp_path, monkeypatch):
    project = tmp_path/'renamed'
    (project/'coordination').mkdir(parents=True)
    nested = project/'src'/'nested'
    nested.mkdir(parents=True)
    daemon_mod.register_project('stable', project)
    monkeypatch.chdir(nested)
    assert daemon_mod._resolve_project_name(None, None) == 'stable'
    assert daemon_mod._resolve_project_name(None, project) == 'stable'


@pytest.mark.parametrize('name', ['../escape', '-option', 'x/y', 'x%h', 'x\nEnvironment=bad', 'a'*81, 'two words'])
def test_invalid_service_names_fail_before_writes(name, fake_systemctl, tmp_path):
    result = _invoke(['install', '--name', name, '--project-dir', str(tmp_path)])
    assert result.exit_code != 0
    assert not daemon_mod.REGISTRY_PATH.exists()
    assert not daemon_mod.SYSTEMD_USER_DIR.exists()
    assert not fake_systemctl.calls


def test_duplicate_basename_and_explicit_identity_cannot_redirect_service(tmp_path, fake_systemctl):
    first, second = tmp_path/'a'/'same', tmp_path/'b'/'same'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    daemon_mod.register_project('same', first)
    before = daemon_mod.REGISTRY_PATH.read_bytes()
    for flags in ([], ['--name', 'same']):
        result = _invoke(['install', '--project-dir', str(second), *flags])
        assert result.exit_code != 0
        assert 'registered' in result.output
    with pytest.raises(Exception, match='different directory'):
        daemon_mod.register_project('same', second)
    assert daemon_mod.REGISTRY_PATH.read_bytes() == before
    assert not fake_systemctl.calls
    assert not daemon_mod.SYSTEMD_USER_DIR.exists()


def test_ambiguous_aliases_require_explicit_selection(tmp_path):
    daemon_mod.register_project('one', tmp_path)
    daemon_mod.register_project('two', tmp_path)
    with pytest.raises(Exception, match='multiple registered names'):
        daemon_mod._resolve_project_name(None, tmp_path)
    assert daemon_mod._resolve_project_name('two', tmp_path) == 'two'


def test_fresh_acp_setup_installs_and_starts_by_registered_identity(tmp_path, fake_systemctl, monkeypatch):
    from greatminds.cli.main import cli
    project = tmp_path/'fresh-acp'
    project.mkdir()
    runner = CliRunner()
    setup = runner.invoke(cli, ['setup', '--project-dir', str(project)])
    assert setup.exit_code == 0, setup.output
    assert not (project/'coord.yaml').exists()
    installed = _invoke(['install', '--name', 'shared-acp', '--project-dir', str(project)])
    assert installed.exit_code == 0, installed.output
    nested = project/'src'
    nested.mkdir()
    monkeypatch.chdir(nested)
    started = _invoke(['start'])
    assert started.exit_code == 0, started.output
    assert fake_systemctl.calls[-1] == ['systemctl', '--user', 'start', 'greatminds-daemon@shared-acp.service']
    assert daemon_mod.lookup_project_dir('shared-acp') == project


def test_failed_reload_is_retried_before_enable_even_when_files_are_unchanged(fake_systemctl, tmp_path):
    _configured(tmp_path)
    fake_systemctl.set(('systemctl', '--user', 'daemon-reload'), rc=1, stderr='manager unavailable')
    args = ['install', '--name', 'reload-test', '--project-dir', str(tmp_path)]
    for _ in range(2):
        result = _invoke(args)
        assert result.exit_code != 0
        assert 'manager unavailable' in result.output
    assert not any('enable' in call for call in fake_systemctl.calls)
    assert sum('daemon-reload' in call for call in fake_systemctl.calls) == 2
    fake_systemctl.set(('systemctl', '--user', 'daemon-reload'), rc=0)
    result = _invoke(args)
    assert result.exit_code == 0, result.output
    assert fake_systemctl.calls[-1] == ['systemctl', '--user', 'enable', 'greatminds-daemon@reload-test.service']


def test_restart_does_not_run_after_failed_reload(fake_systemctl, tmp_path):
    _configured(tmp_path, "reload-test")
    fake_systemctl.set(('systemctl', '--user', 'daemon-reload'), rc=1)
    for _ in range(2):
        result = _invoke(['restart', '--project', 'reload-test'])
        assert result.exit_code != 0
    assert not any('restart' in call for call in fake_systemctl.calls)


@pytest.mark.parametrize('failure', ['timeout', 'missing'])
def test_systemctl_errors_are_bounded_and_actionable(monkeypatch, failure):
    def fail(argv, **kwargs):
        assert kwargs['timeout'] == 30
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 30)
        raise FileNotFoundError('systemctl unavailable')
    monkeypatch.setattr(daemon_mod.subprocess, 'run', fail)
    result = _invoke(['status', '--project', 'test'])
    assert result.exit_code != 0
    assert ('inspect service state' if failure == 'timeout' else 'cannot run systemctl') in result.output


def _configured(root, name=None):
    (root/'coordination').mkdir(parents=True, exist_ok=True)
    (root/'coordination/execution.yaml').write_text('version: 1\nagents: {}\nbindings: {}\n')
    if name:
        daemon_mod.register_project(name, root)
