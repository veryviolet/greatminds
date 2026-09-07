"""Operator watchdog uses ACP identity and current task formats without mutations."""
import os
import time

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.watchdog import watchdog
from test_remote_live_roles_context_0389 import acp_project


@pytest.mark.parametrize('state,reused,expected', [
    ('running', False, False), ('running', True, True),
    ('waiting_auth', False, True), ('waiting_input', False, True),
    ('failed', False, True), ('interrupted', False, True), ('idle', False, False),
])
def test_watchdog_observes_acp_holds(tmp_path, state, reused, expected):
    project = tmp_path / 'project'
    runtime, _ = acp_project(project, state, reused_pid=reused)
    before = {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
    result = CliRunner().invoke(watchdog, ['--project-dir', str(project)])
    assert result.exit_code == 0, result.output
    assert ('ACP ARCHITECT-PLANNER [planner]' in result.output) is expected
    assert ('All clear.' in result.output) is (not expected)
    after = {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
    assert after == before


def test_explicit_project_ignores_environment_and_accepts_nested_cwd(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    runtime, _ = acp_project(project, 'waiting_auth')
    acp_project(tmp_path / 'other', 'running')
    monkeypatch.setenv('GREATMINDS_PROJECT_DIR', str(tmp_path / 'other'))
    result = CliRunner().invoke(watchdog, ['--project-dir', str(runtime), '--quiet'])
    assert result.exit_code == 0, result.output
    assert 'waiting_auth' in result.output


def test_watchdog_uses_selected_schema_for_yaml_and_markdown_tasks(tmp_path):
    project = tmp_path / 'project'
    runtime, snapshot = acp_project(project, 'idle')
    canon = tmp_path / 'canon'
    canon.mkdir()
    schema = snapshot.document
    schema['queues']['feature_inbox']['kind'] = 'active'
    schema['watchdog'] = {'task_stale_in_active_queue_seconds': 10}
    (canon / 'schema.yaml').write_text(yaml.safe_dump(schema))
    old = time.time() - 60
    for name in ('0001-old.yaml', '0002-old.md', '_TEMPLATE.yaml'):
        path = runtime / 'feature_inbox' / name
        path.write_text('id: fixture\n')
        os.utime(path, (old, old))
    result = CliRunner().invoke(watchdog, ['--project-dir', str(project), '--canon-dir', str(canon)])
    assert result.exit_code == 0, result.output
    assert 'STALE TASKS (2)' in result.output
    assert '0001-old.yaml' in result.output
    assert '0002-old.md' in result.output
    assert '_TEMPLATE' not in result.output


def test_bad_schema_is_not_reported_as_healthy(tmp_path):
    project = tmp_path / 'project'
    acp_project(project, 'idle')
    canon = tmp_path / 'canon'
    canon.mkdir()
    (canon / 'schema.yaml').write_text('[')
    result = CliRunner().invoke(watchdog, ['--project-dir', str(project), '--canon-dir', str(canon)])
    assert result.exit_code != 0
    assert 'cannot load schema' in result.output
    assert 'All clear' not in result.output


def test_unavailable_acp_state_is_not_reported_as_healthy(tmp_path):
    project = tmp_path / 'project'
    acp_project(project, 'idle')
    (project / 'coordination/execution.yaml').write_text('[')
    result = CliRunner().invoke(watchdog, ['--project-dir', str(project)])
    assert result.exit_code == 0
    assert 'ACP run observation unavailable' in result.output
    assert 'All clear' not in result.output


def test_worktree_inspection_uses_selected_policy_and_reports_failure(tmp_path, monkeypatch):
    from greatminds.cli import worktree

    project = tmp_path / 'project'
    _, snapshot = acp_project(project, 'idle')
    canon = tmp_path / 'canon'
    canon.mkdir()
    schema = snapshot.document
    schema['worktrees']['base_path'] = 'task-trees'
    (canon / 'schema.yaml').write_text(yaml.safe_dump(schema))
    (project / 'task-trees' / '0003-orphan').mkdir(parents=True)
    args = ['--project-dir', str(project), '--canon-dir', str(canon)]
    result = CliRunner().invoke(watchdog, args)
    assert result.exit_code == 0, result.output
    assert 'ORPHAN WORKTREES (1)' in result.output
    assert 'task-trees/0003-orphan' in result.output

    def unavailable(*args, **kwargs):
        raise OSError('unavailable')

    monkeypatch.setattr(worktree, 'load_worktree_policy', unavailable)
    result = CliRunner().invoke(watchdog, args)
    assert result.exit_code == 0
    assert 'Worktree observation unavailable' in result.output
    assert 'worktrees: 0 orphans' not in result.output
    assert 'All clear' not in result.output
