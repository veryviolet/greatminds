"""Impossible dependencies stay actionable in the shared CLI/daemon report."""
import json

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.wake_check import wake_check
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.maintenance import MaintenanceService
from greatminds.runtime.bootstrap import bootstrap
from greatminds.runtime.store import RunStore


def make(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    bootstrap(project)
    return project, project / '.greatminds'


def write(runtime, queue, task_id, deps=None, ready=True):
    blocks = []
    if deps is not None:
        blocks = [
            {'kind': 'plan', 'by': 'ARCHITECT-PLANNER', 'at': '2026-09-06T10:00:00Z',
             'base_commit': 'fixture', 'assignee_role': 'DEVELOPER', 'stand_required': False,
             'plan_kind': 'full', 'mode': 'A', 'ready_for_implementation': ready},
            {'kind': 'blocked', 'by': 'DEVELOPER', 'at': '2026-09-06T10:00:00Z',
             'reason': 'waiting for prerequisite', 'dependencies': deps, 'resume_to': 'feature_dev'}]
    path = runtime / queue / f'{task_id}.yaml'
    path.write_text(yaml.safe_dump({'id': task_id, 'title': 'Dependency fixture',
        'stream': 'product', 'kind': 'research', 'scope': 'backend', 'reporter': 'USER',
        'opened_at': '2026-09-06T10:00:00Z', 'priority': 'normal', 'blocks': blocks}))
    return path


def report(project):
    result = CliRunner().invoke(wake_check, ['--project-dir', str(project), '--json'])
    assert result.exit_code == 0, result.output
    actual = json.loads(result.output)
    service = MaintenanceService(RunStore(project / '.greatminds'), load_schema_snapshot())
    assert actual == service.inspect()
    text = CliRunner().invoke(wake_check, ['--project-dir', str(project)])
    assert text.exit_code == 0, text.output
    for task in actual['tasks'].values():
        assert f"{task['task_id']}: {task['status']}" in text.output
    return actual


@pytest.mark.parametrize('location,status,reason', [
    ('archive', 'wrong_terminal', 'wrong_terminal'),
    ('verified', 'ready', None),
    (None, 'waiting', 'missing'),
    ('feature_dev', 'waiting', 'waiting'),
])
def test_dependency_location_has_same_meaning_for_cli_and_daemon(tmp_path, location, status, reason):
    project, runtime = make(tmp_path)
    if location:
        write(runtime, location, '0123-foo')
    source = write(runtime, 'feature_blocked', '0200-waiter', ['verified/0123-foo.yaml'])
    before = {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
    item = report(project)['tasks'][source.stem]
    assert item['status'] == status
    assert [r['code'] for r in item['reasons']] == ([reason] if reason else [])
    assert {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob('*') if p.is_file()} == before


def test_cascading_dependency_surfaces_dead_root(tmp_path):
    project, runtime = make(tmp_path)
    write(runtime, 'archive', '0123-foo')
    write(runtime, 'feature_blocked', '0300-b', ['verified/0123-foo.yaml'])
    write(runtime, 'feature_blocked', '0301-a', ['feature_blocked/0300-b.yaml'])
    tasks = report(project)['tasks']
    assert tasks['0300-b']['status'] == 'wrong_terminal'
    assert tasks['0300-b']['reasons'][0]['actual_queues'] == ['archive']
    assert tasks['0301-a']['status'] == 'waiting'
    assert tasks['0301-a']['reasons'][0]['code'] == 'active_dependency'


def test_satisfied_dependencies_do_not_bypass_readiness_without_execution_file(tmp_path):
    project, runtime = make(tmp_path)
    (project / 'coordination/execution.yaml').unlink()
    write(runtime, 'verified', '0123-foo')
    write(runtime, 'feature_blocked', '0200-waiter', ['verified/0123-foo.yaml'], ready=False)
    assert report(project)['tasks']['0200-waiter']['status'] == 'gate_failed'


def test_cycle_diagnostics_share_the_daemon_graph(tmp_path):
    project, runtime = make(tmp_path)
    write(runtime, 'feature_blocked', '0300-b', ['verified/0301-a.yaml'])
    write(runtime, 'feature_blocked', '0301-a', ['verified/0300-b.yaml'])
    result = report(project)
    assert result['cycles'] == [['0300-b', '0301-a']]
    assert {task['status'] for task in result['tasks'].values()} == {'cycle'}


def test_explicit_project_ignores_environment_override(tmp_path, monkeypatch):
    project, runtime = make(tmp_path)
    write(runtime, 'feature_blocked', '0200-waiter', ['verified/0999-missing.yaml'])
    other = tmp_path / 'other'
    other.mkdir()
    bootstrap(other)
    monkeypatch.setenv('GREATMINDS_PROJECT_DIR', str(other))
    assert '0200-waiter' in report(project)['tasks']


def test_missing_project_does_not_report_empty_success(tmp_path):
    result = CliRunner().invoke(wake_check, ['--project-dir', str(tmp_path), '--json'])
    assert result.exit_code != 0
    assert 'runtime directory not found' in result.output
