"""Real process lifecycle independent of the launching CLI/browser."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from greatminds.runtime.background import control, directory, status
from greatminds.runtime.bootstrap import bootstrap
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.processes import process_identity, signal_member
from greatminds.core.storage import atomic_json, file_lock


@pytest.fixture
def project(tmp_path):
    bootstrap(tmp_path)
    yield tmp_path
    control(tmp_path, 'stop')


def cli(project, *args):
    return subprocess.run([sys.executable, '-I', '-m', 'greatminds.cli.main', *args,
                           '--project-dir', str(project)], cwd=project,
                          capture_output=True, text=True, timeout=40)


def test_cli_exit_concurrent_start_symlink_and_restart(project):
    alias = project.parent / (project.name + '-alias')
    alias.symlink_to(project, target_is_directory=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda root: cli(root, 'daemon', 'start'), [project, alias]))
    assert all(result.returncode == 0 for result in results), results
    identities = [json.loads(result.stdout)['process'] for result in results]
    assert identities[0] == identities[1]
    assert status(project)['responsive']
    assert identities[0]['session'] == identities[0]['pid']
    assert (directory(project) / 'daemon.log').exists()
    assert (directory(project) / 'daemon.log').stat().st_mode & 0o777 == 0o600
    restarted = control(project, 'restart')
    assert restarted['process'] != identities[0]
    assert process_identity(identities[0]['pid']) != identities[0]
    result = cli(project, 'daemon', 'stop')
    assert result.returncode == 0, result.stderr
    assert not status(project)['running']


def test_web_exit_keeps_daemon_and_another_client_can_stop(project):
    from greatminds.web.service import WebService
    process = subprocess.Popen([sys.executable, '-I', '-m', 'greatminds.cli.main', 'web',
                                '--project-dir', str(project), '--port', '0'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not status(project)['responsive']:
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.05)
        identity = status(project)['process']
        process.terminate()
        process.communicate(timeout=15)
        assert process.returncode == 0
        assert status(project)['process'] == identity
        assert status(project)['responsive']
        other = WebService(project)
        assert other.start_daemon()['process'] == identity
        other.stop_daemon()
        assert not status(project)['running']
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)


def test_crash_releases_lease_and_stale_metadata_cannot_signal(project):
    first = control(project, 'start')['process']
    signal_member(first, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while status(project)['running']:
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert control(project, 'stop')['state'] == 'stopped'
    assert control(project, 'start')['process'] != first


def test_unknown_owner_is_not_signalled(project):
    with file_lock(directory(project) / 'supervisor.lock', label='fixture'):
        atomic_json(directory(project) / 'daemon.json', {'project': str(project),
                    'process': process_identity(os.getpid()) | {'start_ticks': -1},
                    'heartbeat': time.monotonic(), 'state': 'ready'})
        assert status(project)['running'] and not status(project)['managed']
        with pytest.raises(GreatMindsError, match='unknown process'):
            control(project, 'stop')


def test_invalid_start_is_reported_without_process(project):
    (project / 'coordination/execution.yaml').write_text('version: broken\n')
    with pytest.raises(GreatMindsError):
        control(project, 'start')
    assert not status(project)['running']


def test_foreground_is_same_owner_and_broken_config_can_be_stopped(project):
    foreground = subprocess.Popen([sys.executable, '-I', '-m', 'greatminds.cli.main',
        'coordd', '--foreground', '--project-dir', str(project)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not status(project)['responsive']:
            assert foreground.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.05)
        identity = control(project, 'start')['process']
        assert identity['pid'] == foreground.pid
        duplicate = cli(project, 'coordd', '--foreground')
        assert duplicate.returncode != 0
        (project / 'coordination/execution.yaml').write_text('invalid')
        assert status(project)['restart_required']
        with pytest.raises(GreatMindsError):
            control(project, 'restart')
        assert foreground.poll() is None
        control(project, 'stop')
        foreground.wait(timeout=10)
        assert foreground.returncode == 0
    finally:
        if foreground.poll() is None:
            foreground.terminate()
            foreground.wait(timeout=10)
