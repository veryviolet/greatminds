"""Shared ACP readiness must agree at the observer and resume boundary."""
import json
import pytest
import yaml
from click.testing import CliRunner
from greatminds.cli import agent as agent_mod, task as task_mod
from greatminds.cli.main import cli
from greatminds.domain.maintenance import MaintenanceService
from greatminds.runtime.store import RunStore
from test_remote_live_roles_context_0389 import acp_project, ROLE

def test_required_live_roles_absent_is_empty():
    assert agent_mod.required_live_roles({}, None) == []
    assert agent_mod.required_live_roles({"id": "x"}, {"kind": "blocked"}) == []

def test_required_live_roles_from_header():
    hdr = {"requires_live_roles": ["architect-planner", "DEVELOPER"]}
    assert agent_mod.required_live_roles(hdr, None) == [
        "ARCHITECT-PLANNER", "DEVELOPER"]

def test_required_live_roles_blocked_block_overrides_header():
    hdr = {"requires_live_roles": ["DEVELOPER"]}
    blk = {"kind": "blocked", "requires_live_roles": ["ARCHITECT-PLANNER"]}
    assert agent_mod.required_live_roles(hdr, blk) == ["ARCHITECT-PLANNER"]

def test_required_live_roles_normalizes_and_dedups():
    hdr = {"requires_live_roles": ["planner", "PLANNER", " tester ", 5, None]}
    assert agent_mod.required_live_roles(hdr, None) == ["PLANNER", "TESTER"]

def test_required_live_roles_non_list_is_empty():
    assert agent_mod.required_live_roles(
        {"requires_live_roles": "ARCHITECT-PLANNER"}, None) == []


@pytest.mark.parametrize('state', ['running', 'waiting_auth', 'waiting_input', 'idle'])
@pytest.mark.parametrize('remote', [False, True])
def test_observer_and_resume_share_acp_role_gate(tmp_path, state, remote):
    local, schema = acp_project(tmp_path/'local', 'running' if remote else state)
    target, _ = acp_project(tmp_path/'target', state)
    block = {'kind': 'blocked', 'by': 'DEVELOPER', 'at': '2026-09-06T10:00:00Z',
             'reason': 'waiting for prerequisite', 'dependencies': ['verified/0003-dependency.yaml'],
             'resume_to': 'feature_dev', 'requires_live_roles': [ROLE]}
    if remote:
        block['requires_live_roles_context'] = str(target.parent)
    data = {'id': '0002-waiter', 'title': 'Dependency fixture', 'stream': 'product',
            'kind': 'research', 'scope': 'backend', 'reporter': 'USER',
            'opened_at': '2026-09-06T10:00:00Z', 'priority': 'normal', 'blocks': [
                {'kind': 'plan', 'by': 'ARCHITECT-PLANNER', 'at': '2026-09-06T10:00:00Z',
                 'base_commit': 'fixture', 'assignee_role': 'DEVELOPER', 'stand_required': False,
                 'plan_kind': 'full', 'mode': 'A', 'ready_for_implementation': True}, block]}
    source = local/'feature_blocked/0002-waiter.yaml'
    source.write_text(yaml.safe_dump(data))
    (local/'verified/0003-dependency.yaml').write_text('id: 0003-dependency\n')
    service = MaintenanceService(RunStore(local), schema, environment={})
    finding = service.inspect()['tasks'][data['id']]
    assert (finding['status'] == 'ready') is (state == 'running'), finding
    with task_mod.domain_context(document=schema.document, runtime=local,
                                 workspace=local.parent, environment={}):
        error = task_mod._check_all_dependencies_exist(data, 'feature_blocked', 'feature_dev')
    assert (error is None) is (state == 'running'), error
    result = CliRunner().invoke(cli, ['wake-check', '--project-dir', str(local.parent), '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['tasks'][data['id']]['status'] == finding['status']
