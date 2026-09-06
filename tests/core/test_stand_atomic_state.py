"""Crash-safe stand state publication and serialized concurrent writers."""
import multiprocessing
import os

import pytest

from greatminds.cli import stand_state as ss
from greatminds.core import storage


def _increment(coord, count):
    for _ in range(count):
        ss.update_stand_state(coord, lambda state: state.update(counter=state.get('counter', 0) + 1))


@pytest.mark.parametrize('stage', ['serialize', 'replace'])
def test_failed_publication_preserves_previous_lease(tmp_path, monkeypatch, stage):
    ss.update_stand_state(tmp_path, lambda state: state.update(active_lease={'lease_id': 'original'}))
    before = ss.state_file_path(tmp_path).read_bytes()
    def fail(*args, **kwargs):
        raise OSError('injected publication failure')
    monkeypatch.setattr(ss.yaml if stage == 'serialize' else storage.os,
                        'safe_dump' if stage == 'serialize' else 'replace', fail)
    with pytest.raises(OSError, match='injected'):
        ss.update_stand_state(tmp_path, lambda state: state.update(active_lease=None))
    assert ss.state_file_path(tmp_path).read_bytes() == before


def test_concurrent_writers_and_unlocked_readers_observe_complete_snapshots(tmp_path):
    ss.update_stand_state(tmp_path, lambda state: state.update(counter=0))
    lock_inode = (tmp_path / '.stand/state.lock').stat().st_ino
    processes = [multiprocessing.get_context('fork').Process(target=_increment, args=(tmp_path, 12)) for _ in range(3)]
    for process in processes:
        process.start()
    try:
        while any(process.is_alive() for process in processes):
            state = ss.read_stand_state(tmp_path)
            assert isinstance(state['counter'], int)
            assert state['state'] == 'free'
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
    assert ss.read_stand_state(tmp_path)['counter'] == 36
    assert (tmp_path / '.stand/state.lock').stat().st_ino == lock_inode


def _crash_before_replace(coord):
    def crash(*args, **kwargs):
        os._exit(72)
    storage.os.replace = crash
    ss.update_stand_state(coord, lambda state: state.update(active_lease=None))


def test_process_death_before_replace_preserves_lease_and_releases_lock(tmp_path):
    ss.update_stand_state(tmp_path, lambda state: state.update(active_lease={'lease_id': 'original'}))
    child = multiprocessing.get_context('fork').Process(target=_crash_before_replace, args=(tmp_path,))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.kill()
        child.join(timeout=5)
        pytest.fail('publication fault child did not exit')
    assert child.exitcode == 72
    assert ss.read_stand_state(tmp_path)['active_lease'] == {'lease_id': 'original'}
    ss.update_stand_state(tmp_path, lambda state: state.update(state='ready'))
    assert ss.read_stand_state(tmp_path)['state'] == 'ready'
