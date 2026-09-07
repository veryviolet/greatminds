"""Real local HTTP/daemon boundaries; no external harness or inference."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import time

import pytest
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.web.service import WebService
from greatminds.web.server import WebServer
from greatminds.runtime.activity import ActivityStore, ActivityRecorder


@pytest.fixture
def service(tmp_path):
    (tmp_path / '.greatminds').mkdir()
    (tmp_path / 'coordination').mkdir()
    fixture = Path(__file__).resolve().parents[1] / 'fixtures/acp_server.py'
    (tmp_path / 'coordination/execution.yaml').write_text(yaml.safe_dump({
        'version': 1, 'agents': {'fixture': {'transport': 'acp',
            'argv': [sys.executable, str(fixture), 'resume'],
            'adapter_version': 'fixture', 'harness_version': 'fixture'}},
        'bindings': {'planner': {'role': 'ARCHITECT-PLANNER', 'agent': 'fixture',
            'scheduling': 'on-demand', 'permission': 'deny', 'timeout_seconds': 10}}}))
    svc = WebService(tmp_path)
    yield svc
    svc.stop_daemon()


@pytest.fixture
def http(service):
    server = WebServer(('127.0.0.1', 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        connection.request('POST' if body is not None else 'GET', path, body=data,
                           headers=headers or ({'Content-Type': 'application/json'} if body is not None else {}))
        result = connection.getresponse()
        content = result.read()
        info = (result.status, dict(result.getheaders()), content)
        connection.close()
        return info
    yield request
    server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_local_assets_and_read_only_state_do_not_launch(http, service):
    status, headers, body = http('/')
    assert status == 200 and 'Настройки'.encode() in body
    assert "script-src 'self'" in headers['Content-Security-Policy']
    assert 'Set-Cookie' not in headers
    assert http('/app.js')[0] == http('/style.css')[0] == 200
    before = service.store.snapshot()
    assert http('/api/state')[0] == 200
    assert service.store.snapshot() == before
    assert service.child is None
    assert http('/../../pyproject.toml')[0] == 404


def test_browser_write_boundary_and_invalid_payloads(http, service):
    assert http('/api/dispatch', {'paused': True}, {'Content-Type': 'application/json',
        'Origin': 'https://another.example'})[0] == 403
    assert http('/api/dispatch', {'paused': True}, {'Content-Type': 'text/plain'})[0] == 415
    assert http('/api/dispatch', {'paused': 'false'})[0] == 400
    assert http('/api/dispatch', {})[0] == 400
    assert service.store.snapshot()['paused'] is False
    assert http('/api/dispatch', {'paused': True})[0] == 200
    assert service.store.snapshot()['paused'] is True
    assert http('/api/runs/unknown')[0] == 400
    assert http('/api/conversations/invalid.name')[0] == 400


def test_settings_validate_and_reject_stale_writes(http, service):
    original = service.settings()
    bad = {'text': 'version: 1\nagents: nope\n', 'revision': original['revision']}
    assert http('/api/settings', bad)[0] == 400
    assert service.settings() == original
    document = original['document']; document['max_running'] = 2
    assert http('/api/settings', {'document': document, 'revision': original['revision']})[0] == 200
    assert http('/api/settings', {'text': original['text'], 'revision': original['revision']})[0] == 409
    assert service.settings()['document']['max_running'] == 2
    assert service.child is None


def test_invalid_yaml_can_be_read_and_repaired(http, service):
    service.config_path.write_text('bindings: [\n')
    status, _, data = http('/api/settings')
    assert status == 200 and json.loads(data)['document'] is None
    assert json.loads(http('/api/state')[2])['configuration_error']


def test_conversation_http_idempotency_and_actual_daemon(service, http):
    conv = json.loads(http('/api/conversations', {'binding_id': 'planner'})[2])['id']
    message = {'message': 'hello', 'request_id': 'first'}
    assert http(f'/api/conversations/{conv}/send', message)[0] == 200
    assert http(f'/api/conversations/{conv}/send', message)[0] == 200
    assert http(f'/api/conversations/{conv}/send', {**message, 'message': 'different'})[0] == 409
    assert len(service.conversation(conv)['turns']) == 1
    assert service.child is None
    service.start_daemon(); child = service.child
    service.start_daemon(); assert service.child is child
    deadline = time.monotonic() + 20
    while service.conversation(conv)['turns']['first']['status'] not in {'completed', 'failed'}:
        assert time.monotonic() < deadline
        time.sleep(.1)
    assert service.conversation(conv)['turns']['first']['status'] == 'completed'
    page = json.loads(http(f'/api/conversations/{conv}/events')[2])
    assert ''.join(e['text'] for e in page['events'] if e['kind'] == 'text') == 'hello'
    assert json.loads(http(f'/api/conversations/{conv}/events?after={page["cursor"]}')[2])['events'] == []
    service.stop_daemon(); assert child.poll() is not None
    assert service.daemon_status()['running'] is False


def test_public_activity_redacts_secrets_and_excludes_thoughts(tmp_path):
    store = ActivityStore(tmp_path, 'run')
    recorder = ActivityRecorder(store, ['secret-value'])
    recorder.record({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'private reasoning'}})
    for text in ['A sec', 'ret-va', 'lue B']:
        recorder.record({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}})
    recorder.record({'sessionUpdate': 'tool_call', 'title': 'test', 'rawInput': {'api_key': 'unknown-secret', 'arg': 'secret-value'},
                     '_meta': {'credentials': 'never saved'}})
    recorder.finish()
    raw = store.path.read_text()
    assert all(secret not in raw for secret in ['secret-value','unknown-secret','private reasoning','never saved'])
    assert ''.join(e.get('text','') for e in store.events()['events']) == 'A [redacted] B'
    assert next(e for e in store.events()['events'] if e['kind'] == 'tool')['rawInput']['api_key'] == '[redacted]'
    assert store.path.stat().st_mode & 0o777 == 0o600
    before = store.path.read_bytes()
    cursor = store.events()['cursor']
    assert store.events(cursor)['events'] == []
    assert store.path.read_bytes() == before


def test_activity_retention_has_monotonic_cursors_and_gap(tmp_path, monkeypatch):
    import greatminds.runtime.activity as activity
    monkeypatch.setattr(activity, 'MAX_EVENTS', 3)
    store = ActivityStore(tmp_path, 'run')
    for _ in range(6):
        store.append({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'hello'}})
    page = store.events(1)
    assert page['gap'] and page['discarded_through'] == 3
    assert [e['sequence'] for e in page['events']] == [4,5,6]
    assert page['cursor'] == 6
    assert not store.events(6)['events']


def test_activity_bearer_redaction_across_arbitrary_chunks(tmp_path):
    store = ActivityStore(tmp_path, 'bearer')
    recorder = ActivityRecorder(store, [])
    for chunk in ['header Be', 'ar', 'er ', 'abc', 'def', ' xyz', ' B']:
        recorder.record({'sessionUpdate': 'agent_message_chunk',
                         'content': {'type': 'text', 'text': chunk}})
    recorder.finish()
    assert ''.join(e['text'] for e in store.events()['events']) == 'header Bearer [redacted] xyz B'
    assert 'abcdef' not in store.path.read_text()


def test_web_permission_round_trip_uses_daemon_broker(service, http):
    settings = service.settings()
    doc = settings['document']
    doc['agents']['fixture']['argv'][-1] = 'permission'
    doc['bindings']['planner']['permission'] = 'ask'
    service.save_settings({'document': doc, 'revision': settings['revision']})
    conv = json.loads(http('/api/conversations', {'binding_id': 'planner'})[2])['id']
    http(f'/api/conversations/{conv}/send', {'message': 'check', 'request_id': 'permission'})
    service.start_daemon()
    deadline = time.monotonic() + 20
    while not (pending := json.loads(http('/api/state')[2])['permissions']):
        assert time.monotonic() < deadline
        time.sleep(.1)
    request = pending[0]
    assert http('/api/permissions/' + request['id'], {'option_id': 'invented'})[0] != 200
    assert http('/api/permissions/' + request['id'], {'option_id': 'yes'})[0] == 200
    while service.conversation(conv)['turns']['permission']['status'] != 'completed':
        assert time.monotonic() < deadline
        time.sleep(.1)
    events = json.loads(http(f'/api/conversations/{conv}/events')[2])['events']
    assert 'yes' in ''.join(e.get('text', '') for e in events)
    assert not json.loads(http('/api/state')[2])['permissions']
