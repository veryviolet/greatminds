import multiprocessing

import click
import pytest

from greatminds.cli import daemon


@pytest.mark.parametrize('text', ['{broken', '[]', '{"same":"/a","same":"/b"}',
    '{"relative":"path"}', '{"bad/name":"/root"}', '{"name":null}'])
def test_corrupt_registry_blocks_registration_without_overwrite(text, tmp_path):
    daemon.REGISTRY_PATH.parent.mkdir(parents=True)
    daemon.REGISTRY_PATH.write_text(text)
    with pytest.raises(click.ClickException, match='invalid project registry'):
        daemon.register_project('new', tmp_path/'new')
    assert daemon.REGISTRY_PATH.read_text() == text


def _register_at_once(start, results, name, root):
    start.wait(10)
    try:
        daemon.register_project(name, root)
    except click.ClickException:
        results.put(False)
    else:
        results.put(True)


@pytest.mark.parametrize('conflict', [False, True])
def test_parallel_registrations_do_not_lose_entries_or_redirect_identity(tmp_path, conflict):
    context = multiprocessing.get_context('fork')
    start = context.Event()
    results = context.Queue()
    processes = [context.Process(target=_register_at_once,
        args=(start, results, 'shared' if conflict else f'project-{i}', tmp_path/f'root-{i}'))
        for i in range(8)]
    for process in processes:
        process.start()
    start.set()
    try:
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        registry = daemon.load_registry()
        assert sum(outcomes) == (1 if conflict else 8)
        assert len(registry) == (1 if conflict else 8)
        if not conflict:
            assert registry == {f'project-{i}': str(tmp_path/f'root-{i}') for i in range(8)}
        assert daemon.REGISTRY_PATH.stat().st_mode & 0o777 == 0o600
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


def test_failed_publication_leaves_previous_registry_intact(tmp_path, monkeypatch):
    from greatminds.core import storage
    daemon.register_project('first', tmp_path/'first')
    before = daemon.REGISTRY_PATH.read_bytes()
    def fail(*args):
        raise OSError('synthetic replace failure')
    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(OSError, match='synthetic'):
        daemon.register_project('second', tmp_path/'second')
    assert daemon.REGISTRY_PATH.read_bytes() == before
    assert list(daemon.REGISTRY_PATH.parent.glob('.projects.json.*')) == []
