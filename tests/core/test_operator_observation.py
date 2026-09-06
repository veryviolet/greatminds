import json
import os
from dataclasses import asdict

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.runtime.observation import agent_rows, manifests, snapshot
from greatminds.runtime.processes import process_identity
from test_supervisor import setup


def project(root):
    store, schema, config, task = setup(root)
    (root/'coordination').mkdir()
    document = {'version': 1, 'agents': {'fake': {'transport':'acp', 'argv':['private-executable', 'secret-value'],
        'adapter_version':'fixture', 'harness_version':'fixture'}},
        'bindings': {'developer': {'agent':'fake', 'role':'DEVELOPER'}}}
    (root/'coordination/execution.yaml').write_text(yaml.safe_dump(document))
    from greatminds.runtime.observation import configuration
    _, config = configuration(root)
    return store, schema, config, task


def test_empty_configured_status_is_read_only_and_does_not_claim_live_support(tmp_path):
    store, _, _, _ = project(tmp_path)
    before = {str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    rows = agent_rows(tmp_path)
    assert len(rows) == 1 and rows[0]['state'] == 'idle'
    assert rows[0]['run_id'] is None and not rows[0]['usable']
    tools = manifests(tmp_path)
    assert tools[0]['verification'] == 'configured'
    assert 'secret-value' not in json.dumps(tools)
    assert not store.path.exists()
    assert before == {str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_live_process_does_not_hide_waiting_auth_or_pinned_configuration(tmp_path):
    store, schema, config, task = project(tmp_path)
    claim = store.claim(task=task, binding=config.bindings[0], config=config, schema=schema,
                        project=tmp_path, owner_id='observer-test')
    store.transition(claim.run['id'], owner_id='observer-test', target='starting', event_id='start')
    store.record_process(claim.run['id'], owner_id='observer-test', identity=process_identity(os.getpid()))
    store.transition(claim.run['id'], owner_id='observer-test', target='waiting_auth', event_id='auth', reason='login_required')
    row = agent_rows(tmp_path)[0]
    assert row['alive'] is True and row['usable'] is False
    assert row['state'] == 'waiting_auth' and row['reason'] == 'login_required'
    assert row['task_revision'] == task.sha256
    path = tmp_path/'coordination/execution.yaml'
    doc = yaml.safe_load(path.read_text()); doc['bindings'] = {}
    path.write_text(yaml.safe_dump(doc))
    row = agent_rows(tmp_path)[0]
    assert row['configured'] is False and row['config_current'] is False
    assert row['run_id'] == claim.run['id']
    assert claim.token not in json.dumps(row) and 'token_sha256' not in row


def test_reused_pid_does_not_report_usable_running_agent(tmp_path):
    store, schema, config, task = project(tmp_path)
    claim = store.claim(task=task, binding=config.bindings[0], config=config, schema=schema,
                        project=tmp_path, owner_id='observer-test')
    store.transition(claim.run['id'], owner_id='observer-test', target='starting', event_id='start')
    identity = process_identity(os.getpid()); identity['start_ticks'] -= 1
    store.record_process(claim.run['id'], owner_id='observer-test', identity=identity)
    store.transition(claim.run['id'], owner_id='observer-test', target='running', event_id='running')
    row = agent_rows(tmp_path)[0]
    assert row['state'] == 'running' and row['alive'] is False and row['usable'] is False


def test_cli_surfaces_share_snapshot_and_escape_terminal_controls(tmp_path):
    project(tmp_path)
    statuses = CliRunner().invoke(cli, ['agent','status','--project-dir',str(tmp_path),'--json'])
    assert statuses.exit_code == 0, statuses.output
    assert json.loads(statuses.output) == agent_rows(tmp_path)
    state = snapshot(tmp_path)
    for command in (['run','status'], ['dashboard','--json']):
        result = CliRunner().invoke(cli, [*command,'--project-dir',str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == state
    from greatminds.cli.dashboard import render_dashboard
    state['agents'][0]['reason'] = '\x1b[2J\u202ehidden'
    text = render_dashboard(state)
    assert '\x1b' not in text and '\u202e' not in text
    assert '\\u001b' in text and '\\u202e' in text


def test_explicit_live_role_gate_holds_without_a_usable_acp_run(tmp_path):
    store, _, _, _ = project(tmp_path)
    from greatminds.cli.agent import held_live_roles
    assert not held_live_roles(store.runtime, {}, None).held
    held = held_live_roles(store.runtime, {'requires_live_roles':['DEVELOPER']}, None)
    assert held.held and held.wedged == [('DEVELOPER', 'idle')]
