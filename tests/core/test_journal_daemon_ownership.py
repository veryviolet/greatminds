"""Historical transitions cannot delegate mechanical reconciliation to an agent."""
import asyncio
import json
import sys

from click.testing import CliRunner
import yaml

from greatminds.cli.main import cli
from greatminds.runtime.daemon import serve
from greatminds.runtime.store import RunStore
from test_maintenance import write


def test_dependency_completion_uses_system_reconciliation_without_inbox_wakes(tmp_path):
    runtime = tmp_path / '.greatminds'
    source = write(runtime, 'feature_blocked', '0001-waiter',
                   dependencies=['verified/0002-dependency.yaml'])
    write(runtime, 'verified', '0002-dependency')
    task_bytes = source.read_bytes()
    historical = json.dumps({'t': '2026-09-06T10:00:00Z', 'actor': 'SYSTEM',
        'task': '0002-dependency', 'from': 'feature_review', 'to': 'verified', 'reason': 'approved'}) + '\n'
    journal = runtime / 'journal.ndjson'
    journal.write_text(historical * 2)
    inbox = runtime / 'inbox/architect-reviewer'
    inbox.mkdir(parents=True)
    existing = inbox / 'operator-note.md'
    existing.write_text('Keep this operator note.\n')
    config = tmp_path / 'coordination/execution.yaml'
    config.parent.mkdir()
    config.write_text(yaml.safe_dump({'version': 1, 'agents': {'sentinel': {
        'transport': 'acp', 'adapter_version': 'fixture', 'harness_version': 'fixture',
        'argv': [sys.executable, '-c', "from pathlib import Path; Path('unexpected-agent').touch()"]}},
        'bindings': {'reviewer': {'role': 'ARCHITECT-REVIEWER', 'agent': 'sentinel', 'scheduling': 'queue'}}}))
    first = asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    target = runtime / 'feature_dev' / source.name
    assert not source.exists() and target.read_bytes() == task_bytes
    operations = list(first['maintenance'].values())
    assert len(operations) == 1 and operations[0]['status'] == 'applied'
    assert first['runs'] == {} and first['results'] == {}
    assert not (tmp_path / 'unexpected-agent').exists()
    assert sorted(inbox.iterdir()) == [existing]
    assert existing.read_text() == 'Keep this operator note.\n'
    assert journal.read_text().startswith(historical * 2)
    added = journal.read_text()[len(historical * 2):].splitlines()
    assert len(added) == 1 and json.loads(added[0])['actor'] == 'SYSTEM'
    before = RunStore(runtime).snapshot()
    before_journal = journal.read_bytes()
    asyncio.run(serve(tmp_path, once=True, interval=.2, environment={}))
    assert RunStore(runtime).snapshot() == before
    assert journal.read_bytes() == before_journal
    assert not (tmp_path / 'unexpected-agent').exists()
    assert sorted(inbox.iterdir()) == [existing]


def test_public_cli_exposes_daemon_inspection_without_a_journal_wake_dispatcher():
    assert 'notify-journal' not in cli.commands
    assert 'coordd' in cli.commands and 'wake-check' in cli.commands
    result = CliRunner().invoke(cli, ['notify-journal', '--help'])
    assert result.exit_code == 2
