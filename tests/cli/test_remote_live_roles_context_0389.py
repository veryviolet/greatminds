"""Required live roles are evaluated against durable ACP runs in the target project."""
import os

import pytest
import yaml

from greatminds.cli import agent
from greatminds.core.paths import project_runtime_dir
from greatminds.domain.dependencies import live_role_holds
from greatminds.runtime.bootstrap import bootstrap
from greatminds.runtime.observation import configuration
from greatminds.runtime.processes import process_identity
from greatminds.runtime.store import RunStore, TaskRevision

ROLE = 'ARCHITECT-PLANNER'


def acp_project(root, state='running', reused_pid=False):
    root.mkdir(parents=True)
    bootstrap(root)
    (root/'coordination/execution.yaml').write_text(yaml.safe_dump({'version': 1,
        'agents': {'fixture': {'transport': 'acp', 'argv': ['/synthetic/acp'],
            'adapter_version': 'fixture', 'harness_version': 'fixture'}},
        'bindings': {'planner': {'role': ROLE, 'agent': 'fixture'}}}))
    schema, config = configuration(root)
    runtime = project_runtime_dir(root)
    store = RunStore(runtime)
    if state != 'idle':
        task = runtime/'feature_inbox'/'0001-fixture.yaml'
        task.write_text('id: 0001-fixture\n')
        claim = store.claim(task=TaskRevision.capture(runtime, task), binding=config.bindings[0],
                            config=config, schema=schema, project=root, owner_id='fixture')
        store.transition(claim.run['id'], owner_id='fixture', target='starting', event_id='start')
        identity = process_identity(os.getpid())
        if reused_pid:
            identity['start_ticks'] -= 1
        store.record_process(claim.run['id'], owner_id='fixture', identity=identity)
        store.transition(claim.run['id'], owner_id='fixture', target=state, event_id='state')
    return runtime, schema


@pytest.mark.parametrize('remote_state,held', [('running', False), ('waiting_auth', True),
    ('waiting_input', True), ('failed', True), ('idle', True)])
@pytest.mark.parametrize('form', ['project', 'runtime', 'config', 'relative'])
def test_remote_gate_agrees_for_all_context_forms(tmp_path, monkeypatch, remote_state, held, form):
    local, schema = acp_project(tmp_path/'local')
    remote, _ = acp_project(tmp_path/'remote', remote_state)
    context = {'project': str(remote.parent), 'runtime': str(remote),
               'config': str(remote.parent/'coordination'), 'relative': '../remote'}[form]
    monkeypatch.chdir(tmp_path)  # Relative context must use the local project, not cwd.
    data = {'requires_live_roles': [ROLE], 'requires_live_roles_context': context}
    assert agent.held_live_roles(local, data, None).held is held
    assert bool(live_role_holds(local, data, {}, schema.document)) is held


def test_remote_healthy_does_not_inherit_local_auth_failure(tmp_path):
    local, schema = acp_project(tmp_path/'local', 'waiting_auth')
    remote, _ = acp_project(tmp_path/'remote')
    data = {'requires_live_roles': [ROLE], 'requires_live_roles_context': str(remote.parent)}
    assert not agent.held_live_roles(local, data, None).held
    assert not live_role_holds(local, data, {}, schema.document)


def test_reused_pid_cannot_satisfy_required_role(tmp_path):
    local, schema = acp_project(tmp_path/'project', reused_pid=True)
    data = {'requires_live_roles': [ROLE]}
    assert agent.held_live_roles(local, data, None).held
    assert live_role_holds(local, data, {}, schema.document)


def test_missing_context_and_unconfigured_role_hold_but_opt_out_does_not(tmp_path):
    local, schema = acp_project(tmp_path/'project')
    for data in ({'requires_live_roles': [ROLE], 'requires_live_roles_context': '../missing'},
                 {'requires_live_roles': ['TESTER']}):
        assert agent.held_live_roles(local, data, None).held
        assert live_role_holds(local, data, {}, schema.document)
    data = {'requires_live_roles_context': '../missing'}
    assert not agent.held_live_roles(local, data, None).held
    assert not live_role_holds(local, data, {}, schema.document)


def test_block_override_selects_target_and_cli_field_roundtrips(tmp_path):
    from greatminds.cli.task import coerce_value
    local, schema = acp_project(tmp_path/'local', 'waiting_auth')
    remote, _ = acp_project(tmp_path/'remote')
    header = {'requires_live_roles': ['TESTER'], 'requires_live_roles_context': '../missing'}
    block = {'requires_live_roles': coerce_value('requires_live_roles', '[ARCHITECT-PLANNER]'),
             'requires_live_roles_context': coerce_value('requires_live_roles_context', str(remote.parent))}
    assert not agent.held_live_roles(local, header, block).held
    assert not live_role_holds(local, header, block, schema.document)
