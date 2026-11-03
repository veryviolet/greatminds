"""Project-scoped browser operations; the daemon remains the execution owner."""
import hashlib
import json
import os
from pathlib import Path

import yaml
from greatminds import __version__

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
from greatminds.runtime.observation import agent_rows
from .snapshots import ReadCache, task_row


class WebService:
    def __init__(self, project):
        self.version = __version__
        self.project = Path(project).resolve()
        self.runtime = project_runtime_dir(self.project)
        self.store = RunStore(self.runtime)
        self.config_path = self.project / 'coordination/execution.yaml'
        self.cache = ReadCache()

    def config(self):
        schema = load_schema_snapshot()
        config = self.cache.read(('config', schema.sha256), [self.config_path],
            lambda: load_execution_config(self.config_path, roles=set(schema.document['roles'])))
        return schema, config

    def default_language(self):
        default = os.environ.get('GREATMINDS_WEB_LANGUAGE')
        if default is None:
            path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'greatminds/web.json'
            try:
                default = json.loads(path.read_text()).get('language')
            except (OSError, ValueError, AttributeError):
                pass
        return default if isinstance(default, str) and default in {'en', 'ru', 'zh'} else 'en'

    def default_scale(self):
        path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'greatminds/web.json'
        try:
            value = json.loads(path.read_text()).get('scale')
        except (OSError, ValueError, AttributeError):
            value = None
        return value if isinstance(value, str) and value in {'compact', 'normal', 'large'} else 'normal'

    def daemon_status(self):
        from greatminds.runtime.background import status
        return status(self.project)

    def start_daemon(self):
        from greatminds.runtime.background import control
        return control(self.project, 'start')

    def stop_daemon(self):
        from greatminds.runtime.background import control
        return control(self.project, 'stop')

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
            state = self.cache.read('runtime', [self.store.path], self.store.snapshot)
            schema, config = self.config()
            tasks = [self.cache.read(('task', str(path)), [path], lambda path=path: task_row(path))
                     for queue in schema.document['queues'] if not queue.startswith('.')
                     for path in sorted((self.runtime / queue).glob('*.yaml'))]
            agents = agent_rows(self.project, config=config, state=state)
            bindings = [{'id': b.id, 'role': b.role, 'agent': b.agent, 'model': b.model,
                         'mode': b.mode, 'scheduling': b.scheduling} for b in config.bindings]
        except GreatMindsError as exc:
            state, bindings, error = self.store.snapshot(), [], str(exc)
            tasks, agents = [], []
        conversations = []
        for path in sorted((self.runtime / '.runtime/conversations').glob('*/state.json')):
            try:
                def summary():
                    d = ConversationStore(self.runtime, path.parent.name).snapshot()
                    return {k: d.get(k) for k in ('id', 'binding_id', 'task', 'closed', 'close_requested', 'dispatch')} | {'turn_count': len(d['turns'])}
                conversations.append(self.cache.read(('summary', str(path)), [path], summary))
            except GreatMindsError:
                continue
        # Detailed trace/events are loaded only for the selected run.
        recent = sorted(state['runs'].values(), key=lambda r: r['sequence'], reverse=True)[:100]
        return {'version': 1, 'app_version': self.version, 'project': str(self.project), 'name': self.project.name,
                'daemon': self.daemon_status(), 'configuration_error': error,
                'bindings': bindings, 'conversations': conversations,
                'paused': state.get('paused', False), 'agents': agents,
                'tasks': tasks,
                'run_count': len(state['runs']),
                'runs_before': recent[-1]['sequence'] if recent else None,
                'run_totals': {name: sum(r['state'] == name for r in state['runs'].values())
                               for name in ('completed', 'failed', 'cancelled', 'interrupted')},
                'runs': self._run_rows([r for r in state['runs'].values()
                    if r['state'] not in {'completed', 'failed', 'cancelled', 'interrupted'}]
                    + recent),
                'permissions': [p for p in state.get('permissions', {}).values() if p['status'] == 'pending'],
                'events': state.get('events', [])[-40:]}

    @staticmethod
    def _run_rows(runs):
        unique = {r['id']: r for r in runs}
        return [{k: r.get(k) for k in ('id', 'role', 'agent_id', 'binding_id', 'task_id',
                'conversation_id', 'state', 'reason', 'created_at', 'updated_at', 'sequence')}
                for r in sorted(unique.values(), key=lambda r: r['sequence'], reverse=True)]

    def run_page(self, *, before=None, limit=100):
        if before is not None and (type(before) is not int or before < 1):
            raise GreatMindsError('invalid run cursor', exit_code=2)
        state = self.cache.read('runtime', [self.store.path], self.store.snapshot)
        rows = sorted((r for r in state['runs'].values() if before is None or r['sequence'] < before),
                      key=lambda r: r['sequence'], reverse=True)
        page = rows[:limit]
        return {'runs': self._run_rows(page), 'has_more': len(rows) > limit,
                'before': page[-1]['sequence'] if page else before}

    def conversation(self, identity):
        d = ConversationStore(self.runtime, identity).snapshot()
        return {k: d.get(k) for k in ('id', 'binding_id', 'closed', 'close_requested', 'dispatch', 'turns')}

    def conversation_updates(self, identity, *, after=None, before=None, limit=50):
        if ((after is not None and (type(after) is not int or after < 0))
                or (before is not None and (type(before) is not int or before < 1))
                or type(limit) is not int or not 1 <= limit <= 50):
            raise GreatMindsError('invalid conversation window', exit_code=2)
        chat = ConversationStore(self.runtime, identity)
        doc = self.cache.read(('conversation', identity), [chat.path], chat.snapshot)
        events = doc['events']
        if after is not None and after > len(events):
            raise GreatMindsError('conversation cursor is ahead of the journal', exit_code=2)
        result = {k: doc.get(k) for k in ('id', 'closed', 'close_requested', 'dispatch')}
        # Metadata describes this snapshot, even if an event page stops earlier.
        result['snapshot_cursor'] = len(events)
        if after is None or before is not None:
            turns = sorted((t for t in doc['turns'].values() if before is None or t['sequence'] < before),
                           key=lambda t: t['sequence'])[-limit:]
            first = turns[0]['sequence'] if turns else before or 1
            if before is None:
                selected = {t['id'] for t in turns}
                turns += [t for t in doc['turns'].values() if t['status'] == 'running' and t['id'] not in selected]
            ids = {t['id'] for t in turns}
            outputs = {key: '' for key in ids}
            for event in events:
                if event['kind'] == 'text' and event['turn_id'] in ids:
                    outputs[event['turn_id']] += event['text']
            result.update(turns={t['id']: t for t in turns}, outputs=outputs, events=[],
                          cursor=len(events), has_more=False, before=first, has_older=first > 1)
        else:
            page = events[after:after + 200]
            ids = {event.get('turn_id') for event in page}
            result.update(turns={key: doc['turns'][key] for key in ids if key in doc['turns']},
                          events=page, cursor=after + len(page), has_more=after + len(page) < len(events))
        return result

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
        try:
            activity = ActivityStore(self.runtime, identity).events(after)
        except (OSError, ValueError):
            activity = {'events': [], 'cursor': 0, 'discarded_through': 0,
                        'unavailable': True}
        return {'run': state['runs'][identity], 'activity': activity,
                'events': [e for e in state['events'] if e.get('run_id') == identity],
                'commands': commands,
                'results': [r for r in state['results'].values() if r['envelope']['run_id'] == identity]}

    def task_detail(self, identity):
        paths = list(self.runtime.glob('*/' + safe_name(identity) + '.yaml'))
        if len(paths) != 1:
            raise GreatMindsError('Task must identify one workflow file.', exit_code=2)
        return {'id': identity, 'queue': paths[0].parent.name, 'text': paths[0].read_text()}

    def action(self, path, body):
        if path.startswith('/api/stands/'):
            from .stands import Stands
            stands = Stands(self)
            if path == '/api/stands/connections':
                return stands.save_connections(body)
            if path == '/api/stands/deployment-output':
                return stands.deployment_output(body['id'])
            if path == '/api/stands/file':
                return stands.save_file(body) if 'text' in body else stands.file(body['name'])
            if path == '/api/stands/operations':
                return stands.operations.submit(body)
        if path == '/api/executor-options':
            from .capabilities import discover
            schema = load_schema_snapshot()
            config = parse_execution_config(body['document'], roles=set(schema.document['roles']))
            return discover(config, self.project, body['binding_id'], body.get('model') or None)
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
