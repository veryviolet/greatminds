"""1.6.3: the daemon unit bakes a CLEAN minimal PATH, not the operator's
raw shell PATH (which dragged in cuda / flutter / plugin bins / another
project's .venv-coord)."""
from __future__ import annotations

from greatminds.cli import daemon


def test_clean_path_project_venv_first_and_no_junk():
    p = daemon._clean_daemon_path("/opt/area/nginarea/.venv/bin/greatminds")
    dirs = p.split(":")
    assert dirs[0] == "/opt/area/nginarea/.venv/bin", \
        "the project's own venv bin must be first (its ansible + greatminds)"
    assert str(daemon._current_user_home() / ".local" / "bin") in dirs
    assert ".venv-coord" not in p, "must NOT leak another project's venv"
    for junk in ("cuda", "flutter", "JetBrains", "plugins/cache", "reflex"):
        assert junk not in p, f"raw-shell junk {junk!r} leaked into the PATH"
    for sysd in ("/usr/bin", "/bin"):
        assert sysd in dirs


def test_clean_path_preserves_spaces_in_executable_path():
    p = daemon._clean_daemon_path("/x with spaces/.venv/bin/greatminds")
    assert p.split(":")[0] == "/x with spaces/.venv/bin"


def test_template_unit_sets_home_and_path():
    body = daemon._template_unit_body()
    assert f"Environment=HOME={daemon._current_user_home()}" in body
    assert "Environment=PATH=" in body


def test_path_construction_never_launches_vendor_or_login_shell(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('PATH construction must not run processes')
    monkeypatch.setattr(daemon.subprocess, 'run', forbidden)
    assert '/usr/bin' in daemon._clean_daemon_path('/runtime/bin/greatminds')


def test_systemd_accepts_literal_special_paths(tmp_path, monkeypatch):
    import subprocess
    import shutil
    import pytest
    analyzer = shutil.which('systemd-analyze')
    if not analyzer:
        pytest.skip('systemd-analyze unavailable')
    executable = tmp_path/'bin with space%h$value'/'greatminds'
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    monkeypatch.setattr(daemon, '_greatminds_argv', lambda: (str(executable),))
    monkeypatch.setattr(daemon, '_current_user_home', lambda: tmp_path/'home %h space')
    body = daemon._template_unit_body()
    assert '%%h' in body
    assert '\nEnvironment="HOME=' in body
    unit = tmp_path/'greatminds-daemon@.service'
    unit.write_text(body)
    cp = subprocess.run([analyzer, 'verify', '--man=no', str(unit)],
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stderr
