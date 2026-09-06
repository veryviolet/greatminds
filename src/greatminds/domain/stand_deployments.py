"""Durable deployment intent; an unknown external outcome never permits replay."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_json, file_lock
from greatminds.core.util import now_iso
from greatminds.runtime.processes import process_identity


class DeploymentLedger:
    def __init__(self, runtime: Path):
        self.path = runtime / '.stand' / 'deployments.json'
        self.lock = runtime / '.stand' / 'deployments.lock'

    def snapshot(self):
        try:
            document = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'version': 1, 'attempts': {}}
        except (OSError, ValueError) as exc:
            raise GreatMindsError(f'cannot read deployment ledger: {exc}', exit_code=4) from exc
        if (not isinstance(document, dict) or document.get('version') != 1
                or not isinstance(document.get('attempts'), dict)
                or any(not isinstance(item, dict) or item.get('id') != key
                       or not isinstance(item.get('lease'), dict)
                       or not isinstance(item['lease'].get('lease_id'), str)
                       or not item['lease']['lease_id']
                       or (item.get('status') in {'command_finished', 'applied'}
                           and type(item.get('exit_code')) is not int)
                       or item.get('status') not in {'started', 'command_finished', 'applied', 'needs_recovery'}
                       for key, item in document['attempts'].items())):
            raise GreatMindsError('invalid deployment ledger; recovery required', exit_code=4)
        return document

    def reconcile_applied(self):
        """Recover the receipt after atomic stand publication, without execution."""
        from greatminds.cli.stand_state import read_stand_state
        with file_lock(self.lock, label='deployment ledger'):
            document = self.snapshot()
            pending = [item for item in document['attempts'].values() if item['status'] == 'command_finished']
            if not pending:
                return
            history = read_stand_state(self.path.parent.parent)['history']
            changed = False
            for attempt in pending:
                expected = 'ready' if attempt['exit_code'] == 0 else 'down'
                if any(isinstance(entry, dict)
                       and entry.get('deployment_id') == attempt['id']
                       and entry.get('lease_id') == attempt['lease'].get('lease_id')
                       and entry.get('from') == 'preparing' and entry.get('to') == expected
                       and entry.get('by') == 'COORDD' for entry in history):
                    attempt.update(status='applied', updated_at=now_iso(), recovery='stand_transition_recorded')
                    changed = True
            if changed:
                atomic_json(self.path, document)

    def require_resolved(self):
        pending = [item['id'] for item in self.snapshot()['attempts'].values() if item['status'] != 'applied']
        if pending:
            raise GreatMindsError(
                'deployment outcome requires recovery; refusing external replay: ' + ', '.join(pending)
                + '. Inspect greatminds stand deployment-status', exit_code=4)

    def begin(self, lease):
        if not isinstance(lease, dict) or not isinstance(lease.get('lease_id'), str) or not lease['lease_id']:
            raise GreatMindsError('deployment requires a lease identity', exit_code=4)
        with file_lock(self.lock, label='deployment ledger'):
            self.require_resolved()
            document = self.snapshot()
            attempt = {'id': uuid.uuid4().hex, 'status': 'started', 'lease': {key: copy.deepcopy(lease[key]) for key in
                       ('lease_id', 'task', 'profile', 'worktree', 'holder_role', 'granted_at') if key in lease},
                       'created_at': now_iso(), 'owner_process': process_identity(os.getpid())}
            document['attempts'][attempt['id']] = attempt
            atomic_json(self.path, document)
            return attempt['id']

    def _update(self, attempt_id, expected, **fields):
        with file_lock(self.lock, label='deployment ledger'):
            document = self.snapshot()
            attempt = document['attempts'].get(attempt_id)
            if not attempt or attempt['status'] not in expected:
                raise GreatMindsError('invalid deployment receipt transition', exit_code=4)
            attempt.update(fields, updated_at=now_iso())
            atomic_json(self.path, document)

    def finished(self, attempt_id, rc, log):
        # Raw logs may contain credentials; the ledger only retains identity.
        self._update(attempt_id, {'started'}, status='command_finished', exit_code=rc,
                     log_sha256=hashlib.sha256((log or '').encode()).hexdigest())

    def applied(self, attempt_id):
        self._update(attempt_id, {'command_finished'}, status='applied')

    def uncertain(self, attempt_id):
        self._update(attempt_id, {'started'}, status='needs_recovery',
                     reason='executor_raised_without_durable_result')
