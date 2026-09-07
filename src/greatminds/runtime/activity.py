"""Bounded public ACP activity for local operator inspection, never run authority."""
import json
import re
import time

from greatminds.core.storage import atomic_json, file_lock, safe_name
from .permissions import redact

MAX_BYTES = 262144
MAX_EVENTS = 512
MAX_ITEM_BYTES = 16384


class ActivityStore:
    def __init__(self, runtime, run_id):
        self.directory = runtime / '.runtime' / 'activity'
        self.path = self.directory / (safe_name(run_id) + '.json')
        self.lock = self.directory / (run_id + '.lock')

    def snapshot(self):
        if not self.path.exists():
            return {'version': 1, 'cursor': 0, 'discarded_through': 0, 'events': []}
        return json.loads(self.path.read_text())

    def append(self, update, *, secrets=()):
        kind = update.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            content = update.get('content', {})
            if content.get('type') != 'text':
                return
            data = {'kind': 'message', 'text': content.get('text', '')}
        elif kind in {'tool_call', 'tool_call_update'}:
            data = {'kind': 'tool', **{key: update[key] for key in
                ('toolCallId', 'title', 'kind', 'status', 'rawInput', 'rawOutput', 'content') if key in update}}
            data['tool_kind'] = update.get('kind')
            data['kind'] = 'tool'
        else:
            return  # Includes private thoughts and arbitrary protocol metadata.
        encoded = json.dumps(redact(data, list(secrets)), ensure_ascii=False).encode()
        if len(encoded) > MAX_ITEM_BYTES:
            data = {'kind': data['kind'], 'text': encoded[:MAX_ITEM_BYTES].decode('utf-8', 'ignore'),
                    'truncated': True}
        else:
            data = json.loads(encoded)
        with file_lock(self.lock, label='run activity'):
            state = self.snapshot()
            state['cursor'] += 1
            state['events'].append({'sequence': state['cursor'], 'at': time.time(), **data})
            while (len(state['events']) > MAX_EVENTS or
                   len(json.dumps(state, ensure_ascii=False).encode()) > MAX_BYTES):
                state['discarded_through'] = state['events'].pop(0)['sequence']
            atomic_json(self.path, state)

    def events(self, after=0):
        state = self.snapshot()
        return {**state, 'events': [e for e in state['events'] if e['sequence'] > after],
                'gap': after < state['discarded_through']}


class ActivityRecorder:
    """Hold possible secret prefixes between text chunks; flush at turn end."""
    def __init__(self, store, secrets):
        self.store = store
        self.secrets = tuple(s for s in secrets if s)
        self.pending = ''
        self.bearer_tail = False

    def record(self, update):
        if update.get('sessionUpdate') != 'agent_message_chunk':
            self.store.append(update, secrets=self.secrets)
            return
        content = update.get('content', {})
        if content.get('type') != 'text':
            return
        self.pending += content.get('text', '')
        # Once a bearer token starts, suppress its remainder across updates.
        if self.bearer_tail:
            tail = re.match(r"[^\s\"']*", self.pending).end()
            self.pending = self.pending[tail:]
            if not self.pending:
                return
            self.bearer_tail = False
        def hide_bearer(match):
            if match.end() == len(self.pending):
                self.bearer_tail = True
            return match.group(1) + '[redacted]'
        self.pending = re.sub(r"(?i)(bearer\s+)[^\s\"']+", hide_bearer, self.pending)
        # Replace complete credentials before choosing a safe emission boundary.
        for secret in sorted(self.secrets, key=len, reverse=True):
            self.pending = self.pending.replace(secret, '[redacted]')
        keep = 0
        for secret in self.secrets:
            for size in range(min(len(secret)-1, len(self.pending)), keep, -1):
                if self.pending.endswith(secret[:size]):
                    keep = size
                    break
        bearer_prefix = re.search(r'(?i)bearer\s*$', self.pending)
        if bearer_prefix:
            keep = max(keep, len(self.pending) - bearer_prefix.start())
        for size in range(1, 7):
            if self.pending.lower().endswith('bearer'[:size]):
                keep = max(keep, size)
        text = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ''
        if text:
            self.store.append({'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': text}}, secrets=self.secrets)

    def finish(self):
        if self.pending:
            self.store.append({'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': self.pending}}, secrets=self.secrets)
            self.pending = ''
