"""Project-scoped browser operations; the daemon remains the execution owner."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.core.storage import atomic_bytes, file_lock, safe_name
from greatminds.runtime.config import load_execution_config, parse_execution_config
from greatminds.runtime.interactions import ConversationStore
from greatminds.runtime.store import RunStore, TaskRevision
from greatminds.runtime.permissions import PermissionService
from greatminds.runtime.activity import ActivityStore
from greatminds.runtime.commands import CommandService
from greatminds.runtime.observation import snapshot


class WebService:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.runtime = project_runtime_dir(self.project)
        self.store = RunStore(self.runtime)
        self.config_path = self.project / 'coordination/execution.yaml'
        self.child = None
        self.child_config = None
        self.child_lock = threading.RLock()
        self.daemon_output = ''

    def config(self):
        schema = load_schema_snapshot()
        return schema, load_execution_config(self.config_path, roles=set(schema.document['roles']))

    def daemon_status(self):
        locked = False
        path = self.store.directory / 'supervisor.lock'
        if path.exists():
            with path.open('rb') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream, fcntl.LOCK_UN)
                except BlockingIOError:
                    locked = True
        owned = self.child is not None and self.child.poll() is None
        revision = hashlib.sha256(self.config_path.read_bytes()).hexdigest() if self.config_path.exists() else None
        return {'running': locked, 'owned': owned, 'starting': owned and not locked,
                'exit_code': self.child.poll() if self.child else None,
                'restart_required': bool(owned and self.child_config != revision)}

    def start_daemon(self):
        with self.child_lock:
            self.config()
            if self.daemon_status()['running'] or (self.child and self.child.poll() is None):
                return self.daemon_status()
            self.daemon_output = ''
            self.child_config = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
            self.child = subprocess.Popen([sys.executable, '-I', '-m', 'greatminds.cli.main',
                'coordd', '--project-dir', str(self.project)], cwd=self.project,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True)
            child = self.child
            def drain():
                try:
                    while chunk := child.stdout.read(1024):
                        self.daemon_output = (self.daemon_output + chunk.decode('utf-8', 'replace'))[-8192:]
                finally:
                    child.stdout.close()
            threading.Thread(target=drain, daemon=True).start()
            return self.daemon_status()

    def stop_daemon(self):
        with self.child_lock:
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    raise GreatMindsError('Daemon is still finishing work; inspect its status before retrying.', exit_code=4)
            return self.daemon_status()

    def settings(self):
        raw = self.config_path.read_bytes() if self.config_path.exists() else b'version: 1\nagents: {}\nbindings: {}\n'
        schema = load_schema_snapshot()
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError:
            document = None
        return {'text': raw.decode(), 'document': document, 'revision': hashlib.sha256(raw).hexdigest(),
                'roles': list(schema.document['roles']), 'path': str(self.config_path)}

    def save_settings(self, body):
        text = body['text'] if 'text' in body else yaml.safe_dump(body['document'], sort_keys=False)
        if not isinstance(text, str) or len(text.encode()) > 262144:
            raise GreatMindsError('Execution settings must be at most 256 KiB.', exit_code=2)
        schema = load_schema_snapshot()
        parse_execution_config(yaml.safe_load(text), roles=set(schema.document['roles']))
        with file_lock(self.runtime / 'setup.lock', label='web settings'):
            if self.settings()['revision'] != body['revision']:
                raise GreatMindsError('Settings changed in another window. Reload before saving.', exit_code=4)
            atomic_bytes(self.config_path, text.encode())
        return self.settings()

    def overview(self):
        error = None
        try:
            state = snapshot(self.project)
            schema, config = self.config()
            bindings = [{'id': b.id, 'role': b.role, 'agent': b.agent, 'model': b.model,
                         'mode': b.mode, 'scheduling': b.scheduling} for b in config.bindings]
        except GreatMindsError as exc:
            state, bindings, error = self.store.snapshot(), [], str(exc)
        conversations = []
        for path in sorted((self.runtime / '.runtime/conversations').glob('*/state.json')):
            try:
                d = ConversationStore(self.runtime, path.parent.name).snapshot()
                conversations.append({k: d.get(k) for k in
                    ('id', 'binding_id', 'task', 'closed', 'close_requested', 'dispatch')} |
                    {'turn_count': len(d['turns'])})
            except GreatMindsError:
                continue
        # Detailed trace/events are loaded only for the selected run.
        return {'version': 1, 'project': str(self.project), 'name': self.project.name,
                'daemon': self.daemon_status(), 'configuration_error': error,
                'bindings': bindings, 'conversations': conversations,
                'paused': state.get('paused', False), 'agents': state.get('agents', []),
                'tasks': state.get('tasks', []), 'accounts': state.get('accounts', {}),
                'runs': sorted([{k: r.get(k) for k in ('id', 'role', 'agent_id', 'binding_id',
                    'task_id', 'conversation_id', 'state', 'reason', 'created_at', 'updated_at', 'sequence')}
                    for r in state['runs'].values()], key=lambda r: r['sequence'], reverse=True),
                'permissions': [p for p in state.get('permissions', {}).values() if p['status'] == 'pending'],
                'events': state.get('events', [])[-40:]}

    def conversation(self, identity):
        d = ConversationStore(self.runtime, identity).snapshot()
        return {k: d.get(k) for k in ('id', 'binding_id', 'closed', 'close_requested', 'dispatch', 'turns')}

    def create_conversation(self, body):
        schema, config = self.config()
        binding = next((b for b in config.bindings if b.id == body['binding_id']), None)
        if not binding:
            raise GreatMindsError('Unknown role binding.', exit_code=2)
        task = None
        if body.get('task_id'):
            paths = list(self.runtime.glob('*/' + safe_name(body['task_id']) + '.yaml'))
            if len(paths) != 1:
                raise GreatMindsError('Task must identify one workflow file.', exit_code=2)
            task = TaskRevision.capture(self.runtime, paths[0])
            if task.path.split('/')[0] not in schema.document['roles'][binding.role].get('claims_from', []):
                raise GreatMindsError('This role cannot claim the task queue.', exit_code=3)
        store = ConversationStore.create(self.runtime, binding=binding, config_sha256=config.sha256,
            schema_sha256=schema.sha256, workspace=binding.workspace_path(self.project), task=task)
        return {'id': store.id}

    def run_detail(self, identity, after=0):
        safe_name(identity)
        state = self.store.snapshot()
        if identity not in state['runs']:
            raise GreatMindsError('Unknown run.', exit_code=2)
        commands = []
        previews = CommandService(self.store)
        for command in state.get('commands', {}).values():
            if command['run_id'] != identity:
                continue
            item = dict(command)
            try:
                item['preview'] = previews.output_preview(command['id'])
            except GreatMindsError as exc:
                item['preview_error'] = str(exc)
            commands.append(item)
        return {'run': state['runs'][identity], 'activity': ActivityStore(self.runtime, identity).events(after),
                'events': [e for e in state['events'] if e.get('run_id') == identity],
                'commands': commands,
                'results': [r for r in state['results'].values() if r['envelope']['run_id'] == identity]}

    def task_detail(self, identity):
        paths = list(self.runtime.glob('*/' + safe_name(identity) + '.yaml'))
        if len(paths) != 1:
            raise GreatMindsError('Task must identify one workflow file.', exit_code=2)
        return {'id': identity, 'queue': paths[0].parent.name, 'text': paths[0].read_text()}

    def action(self, path, body):
        if path == '/api/settings':
            return self.save_settings(body)
        if path == '/api/dispatch':
            if type(body['paused']) is not bool:
                raise GreatMindsError('paused must be boolean', exit_code=2)
            self.store.set_paused(body['paused'])
            return {'paused': body['paused']}
        if path == '/api/daemon/start':
            return self.start_daemon()
        if path == '/api/daemon/stop':
            return self.stop_daemon()
        if path == '/api/conversations':
            return self.create_conversation(body)
        parts = path.strip('/').split('/')
        if len(parts) == 4 and parts[:2] == ['api', 'conversations']:
            store = ConversationStore(self.runtime, parts[2])
            if parts[3] == 'send':
                return store.enqueue(body['message'], request_id=body['request_id'])
            if parts[3] == 'interrupt':
                store.cancel(body['request_id'])
                return {'ok': True}
            if parts[3] == 'close':
                store.request_close()
                return {'ok': True}
        if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] in {'cancel', 'retry'}:
            return self.store.request_control(parts[2], parts[3])
        if len(parts) == 3 and parts[:2] == ['api', 'permissions']:
            return PermissionService(self.store).answer(parts[2], body['option_id'])
        raise GreatMindsError('Unknown operation.', exit_code=2)
