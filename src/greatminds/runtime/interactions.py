"""Durable FIFO turns and reconnect cursors for daemon-owned conversations.

This store does not launch agents. Its owner is the exclusive project supervisor;
provider session loading and permission handling belong to that supervisor.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import contextmanager

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_json, file_lock, safe_name

MAX_PROMPT_BYTES = 65536
MAX_PENDING = 64
MAX_OUTPUT_BYTES = 1048576
MAX_OUTPUT_EVENTS = 4096
MAX_TURNS = 256
FINISHED = {'completed', 'failed', 'cancelled', 'interrupted'}


def _fail(message):
    raise GreatMindsError(message, exit_code=4)


class ConversationStore:
    def __init__(self, runtime, conversation_id):
        self.id = safe_name(conversation_id)
        self.directory = runtime / '.runtime' / 'conversations' / self.id
        self.path = self.directory / 'state.json'
        self.lock = self.directory / 'conversation.lock'

    @classmethod
    def create(cls, runtime, *, binding, config_sha256, schema_sha256, workspace):
        store = cls(runtime, uuid.uuid4().hex)
        document = {'version': 1, 'id': store.id, 'binding_id': binding.id,
                    'binding_sha256': binding.sha256, 'config_sha256': config_sha256,
                    'schema_sha256': schema_sha256, 'workspace': str(workspace.resolve()),
                    'owner_id': None, 'session_id': None, 'closed': False,
                    'turns': {}, 'events': []}
        with file_lock(store.lock, label='conversation'):
            atomic_json(store.path, document)
        return store

    def snapshot(self):
        try:
            document = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise GreatMindsError('cannot read conversation journal', exit_code=4) from exc
        if (not isinstance(document, dict) or document.get('version') != 1
                or document.get('id') != self.id or not isinstance(document.get('turns'), dict)
                or not isinstance(document.get('events'), list) or type(document.get('closed')) is not bool
                or any(not isinstance(t, dict) or t.get('id') != key
                       or t.get('status') not in FINISHED | {'queued', 'running'}
                       for key, t in document['turns'].items())):
            _fail('invalid conversation journal')
        return document

    @contextmanager
    def _transaction(self):
        with file_lock(self.lock, label='conversation'):
            document = self.snapshot()
            before = copy.deepcopy(document)
            yield document
            if document != before:
                atomic_json(self.path, document)

    @staticmethod
    def _event(document, kind, turn_id, **data):
        document['events'].append({'sequence': len(document['events']) + 1,
                                   'kind': kind, 'turn_id': turn_id, **data})

    def enqueue(self, text, *, request_id):
        safe_name(request_id)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_PROMPT_BYTES:
            _fail('prompt must contain 1–65536 UTF-8 bytes')
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._transaction() as document:
            existing = document['turns'].get(request_id)
            if existing:
                if existing['prompt_sha256'] != digest:
                    _fail('request identity already belongs to a different prompt')
                return copy.deepcopy(existing)
            if document['closed']:
                _fail('conversation is closed')
            if len(document['turns']) >= MAX_TURNS:
                _fail('conversation turn limit reached; create a new conversation')
            if sum(t['status'] in {'queued', 'running'} for t in document['turns'].values()) >= MAX_PENDING:
                _fail('conversation pending turn limit reached')
            turn = {'id': request_id, 'sequence': len(document['turns']) + 1,
                    'prompt': text, 'prompt_sha256': digest, 'status': 'queued',
                    'output_bytes': 0, 'output_events': 0,
                    'output_truncated': False, 'cancel_requested': False}
            document['turns'][request_id] = turn
            self._event(document, 'queued', request_id)
            return copy.deepcopy(turn)

    def acquire(self, owner_id, *, config_sha256, schema_sha256):
        """Called only while holding the exclusive project supervisor lease."""
        safe_name(owner_id)
        with self._transaction() as document:
            if (document['config_sha256'] != config_sha256 or document['schema_sha256'] != schema_sha256):
                _fail('conversation contract changed; create a new conversation')
            if document['closed']:
                _fail('conversation is closed')
            if document['owner_id'] == owner_id:
                return
            for turn in document['turns'].values():
                if turn['status'] == 'running':
                    turn.update(status='interrupted', reason='supervisor_restart')
                    self._event(document, 'interrupted', turn['id'], reason='supervisor_restart')
            document['owner_id'] = owner_id

    @staticmethod
    def _owned(document, owner_id):
        if document['owner_id'] != owner_id or document['closed']:
            _fail('conversation is not owned by this supervisor')

    def set_session(self, owner_id, session_id):
        if not isinstance(session_id, str) or not session_id:
            _fail('session identity is required')
        with self._transaction() as document:
            self._owned(document, owner_id)
            document['session_id'] = session_id

    def claim_next(self, owner_id):
        with self._transaction() as document:
            self._owned(document, owner_id)
            if any(t['status'] == 'running' for t in document['turns'].values()):
                return None
            queued = [t for t in document['turns'].values() if t['status'] == 'queued']
            if not queued:
                return None
            turn = min(queued, key=lambda t: t['sequence'])
            turn['status'] = 'running'
            self._event(document, 'started', turn['id'])
            return copy.deepcopy(turn)

    def append_text(self, owner_id, turn_id, text):
        """Accept only normalized/redacted assistant text, never raw tool payloads."""
        if not isinstance(text, str):
            _fail('assistant output must be text')
        with self._transaction() as document:
            self._owned(document, owner_id)
            turn = document['turns'].get(turn_id)
            if not turn or turn['status'] != 'running':
                _fail('output requires a running turn')
            if turn['output_truncated']:
                return
            encoded = text.encode()
            remaining = MAX_OUTPUT_BYTES - turn['output_bytes']
            if turn['output_events'] >= MAX_OUTPUT_EVENTS:
                remaining = 0
            captured = encoded[:remaining].decode('utf-8', errors='ignore')
            if captured:
                turn['output_bytes'] += len(captured.encode())
                turn['output_events'] += 1
                self._event(document, 'text', turn_id, text=captured)
            if len(encoded) > remaining and not turn['output_truncated']:
                turn['output_truncated'] = True
                self._event(document, 'output_truncated', turn_id)

    def finish(self, owner_id, turn_id, *, status, reason):
        if status not in FINISHED or not isinstance(reason, str) or len(reason) > 200:
            _fail('invalid turn outcome')
        with self._transaction() as document:
            self._owned(document, owner_id)
            turn = document['turns'].get(turn_id)
            if not turn:
                _fail('unknown turn')
            if turn['status'] == status and turn.get('reason') == reason:
                return
            if turn['status'] != 'running':
                _fail('turn is no longer running')
            turn.update(status=status, reason=reason)
            self._event(document, status, turn_id, reason=reason)

    def cancel(self, turn_id):
        with self._transaction() as document:
            turn = document['turns'].get(turn_id)
            if not turn:
                _fail('unknown turn')
            if turn['status'] == 'queued':
                turn.update(status='cancelled', reason='operator_cancelled')
                self._event(document, 'cancelled', turn_id, reason='operator_cancelled')
            elif turn['status'] == 'running' and not turn['cancel_requested']:
                turn['cancel_requested'] = True
                self._event(document, 'cancel_requested', turn_id)

    def dispatch_status(self, status, reason=None):
        """Publish daemon admission feedback without changing queued messages."""
        value = {'status': status, 'reason': reason}
        with self._transaction() as document:
            if document.get('dispatch') != value:
                document['dispatch'] = value
                self._event(document, 'dispatch', None, **value)

    def events(self, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            _fail('invalid conversation cursor or page size')
        document = self.snapshot()
        if after > len(document['events']):
            _fail('conversation cursor is ahead of the journal')
        page = document['events'][after:after + limit]
        return {'events': page, 'cursor': page[-1]['sequence'] if page else after,
                'has_more': after + len(page) < len(document['events']),
                'dispatch': document.get('dispatch')}
