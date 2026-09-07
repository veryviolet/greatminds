import os
from pathlib import Path

import pytest

from greatminds.domain.filesystem_health import orphan_intents, orphan_worktrees, stale_tasks


def touch(root, relative, timestamp=10):
    path = root/relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('id: fixture\n')
    os.utime(path, (timestamp, timestamp))
    return path


def test_stale_checks_use_queue_kinds_thresholds_and_task_files(tmp_path):
    schema = {'queues': {'active': {'kind': 'active'}, 'done': {'kind': 'terminal'},
                         'waiting': {'kind': 'parking'}, 'feature_review': {'kind': 'active'}},
              'watchdog': {'task_stale_in_active_queue_seconds': 20,
                           'task_stale_in_review_queue_seconds': 5, 'intent_orphan_seconds': 10}}
    touch(tmp_path, 'active/0001.yaml')
    touch(tmp_path, 'active/_TEMPLATE.md')
    touch(tmp_path, 'done/0002.yaml')
    touch(tmp_path, 'waiting/0003.yaml')
    touch(tmp_path, 'feature_review/0004.md')
    touch(tmp_path, 'intent/operation.json')
    (tmp_path/'active/not-a-task.yaml').mkdir()
    assert stale_tasks(tmp_path, schema, now=20) == [
        {'queue': 'feature_review', 'name': '0004.md', 'task_id': '0004',
         'age_seconds': 10, 'threshold_seconds': 5}]
    assert orphan_intents(tmp_path, schema, now=20) == []
    assert orphan_intents(tmp_path, schema, now=21)[0]['age_seconds'] == 11


def test_worktrees_respect_custom_terminal_and_parking_queues(tmp_path):
    runtime = tmp_path/'.greatminds'
    schema = {'queues': {'custom_done': {'kind': 'terminal'}, 'custom_wait': {'kind': 'parking'},
                         'custom_active': {'kind': 'active'}}, 'worktrees': {'base_path': 'trees'}}
    touch(runtime, 'custom_done/0001-done.yaml')
    touch(runtime, 'custom_wait/0002-waiting.yaml')
    touch(runtime, 'custom_active/nonnumeric-slug.yaml')
    for name in ('0001-done', '0002-waiting', '0002', 'nonn', 'nonnumeric-slug'):
        (tmp_path/'trees'/name).mkdir(parents=True)
    assert [row['name'] for row in orphan_worktrees(tmp_path, runtime, schema)] == ['0001-done', 'nonn']


def test_unreadable_directory_is_not_reported_as_empty(tmp_path, monkeypatch):
    original = Path.iterdir
    blocked = tmp_path/'active'
    def iterdir(path):
        if path == blocked:
            raise PermissionError('private filesystem detail')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', iterdir)
    with pytest.raises(PermissionError):
        stale_tasks(tmp_path, {'queues': {'active': {'kind': 'active'}}}, now=100)


@pytest.mark.parametrize('queue,flag', [('verified', 'cleanup_on_verified'), ('archive', 'cleanup_on_archive')])
def test_explicit_retention_policy_does_not_produce_false_orphan(tmp_path, queue, flag):
    runtime = tmp_path/'.greatminds'
    schema = {'queues': {queue: {'kind': 'terminal'}}, 'worktrees': {flag: False}}
    touch(runtime, f'{queue}/0001-retained.yaml')
    (tmp_path/'.worktrees/0001-retained').mkdir(parents=True)
    assert orphan_worktrees(tmp_path, runtime, schema) == []
    schema['worktrees'][flag] = True
    assert orphan_worktrees(tmp_path, runtime, schema)[0]['name'] == '0001-retained'
