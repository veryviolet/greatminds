import json

from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.core.paths import project_runtime_dir
from greatminds.runtime.store import RunStore


def test_events_are_read_only_and_cursor_pages_do_not_overlap(tmp_path):
    store = RunStore(project_runtime_dir(tmp_path))
    for paused in (True, False, True):
        store.set_paused(paused)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    args = ['run', 'events', '--project-dir', str(tmp_path), '--limit', '2']
    first = CliRunner().invoke(cli, args)
    assert first.exit_code == 0, first.output
    rows = [json.loads(line) for line in first.output.splitlines()]
    second = CliRunner().invoke(cli, [*args, '--after', str(rows[-1]['sequence'])])
    assert second.exit_code == 0, second.output
    remaining = [json.loads(line) for line in second.output.splitlines()]
    assert rows + remaining == store.snapshot()['events']
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_follow_emits_new_events_once_and_interrupts_cleanly(tmp_path, monkeypatch):
    from greatminds.cli import run
    store = RunStore(project_runtime_dir(tmp_path))
    store.set_paused(True)
    waits = []
    def tick(interval):
        waits.append(interval)
        if len(waits) == 1:
            store.set_paused(False)
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(run.time, 'sleep', tick)
    result = CliRunner().invoke(cli, ['run', 'events', '--project-dir', str(tmp_path), '--follow'])
    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.output.splitlines()] == store.snapshot()['events']
    assert waits == [1.0, 1.0]


def test_removed_native_commands_are_unavailable():
    for command in ('stop-decide', 'driven-log'):
        result = CliRunner().invoke(cli, [command, '--help'])
        assert result.exit_code == 2
        assert 'No such command' in result.output
