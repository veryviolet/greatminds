"""Durable deployment intent; an unknown external outcome never permits replay."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import os
import uuid
from contextlib import ExitStack
from pathlib import Path

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_json, file_lock
from greatminds.core.util import now_iso
from greatminds.runtime.processes import process_identity, group_members, terminate_group


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
                       or ('sequence' in item and (type(item['sequence']) is not int or item['sequence'] <= 0))
                       or (item.get('status') in {'command_finished', 'applied'}
                           and type(item.get('exit_code')) is not int)
                       or item.get('status') not in {'started', 'command_finished', 'applied', 'needs_recovery', 'resolved'}
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

    def reconcile(self):
        """Daemon sweep: clean orphans, preserve unresolved external outcomes."""
        if not self.path.exists():
            return  # Local workflows without stands need no stand state.
        with ExitStack() as stack:
            try:
                stack.enter_context(file_lock(self.path.parent / 'deployment.lock',
                                              label='stand deployment', timeout=0))
            except GreatMindsError:
                return  # A deployment or operator recovery owns the scope.
            self.reconcile_applied()
            for attempt in self.snapshot()['attempts'].values():
                if (attempt['status'] not in {'applied', 'resolved'}
                        and attempt.get('launch_protocol') == 'gated-v1'
                        and attempt.get('cleanup') != 'confirmed'):
                    self._recover_process_locked(attempt['id'])

    def require_resolved(self):
        pending = [item['id'] for item in self.snapshot()['attempts'].values() if item['status'] not in {'applied', 'resolved'}]
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
            attempt = {'id': uuid.uuid4().hex, 'status': 'started',
                       'sequence': max((a.get('sequence', 0) for a in document['attempts'].values()), default=0) + 1, 'lease': {key: copy.deepcopy(lease[key]) for key in
                       ('lease_id', 'task', 'profile', 'worktree', 'holder_role', 'granted_at') if key in lease},
                       'launch_protocol': 'gated-v1', 'created_at': now_iso(), 'owner_process': process_identity(os.getpid())}
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

    def attach_process(self, attempt_id, identity, *, argv, cwd):
        with file_lock(self.lock, label='deployment ledger'):
            document = self.snapshot()
            attempt = document['attempts'].get(attempt_id)
            if (not attempt or attempt['status'] != 'started' or attempt.get('process')
                    or attempt.get('owner_process') != process_identity(os.getpid())):
                raise GreatMindsError('deployment process assignment is stale or already used', exit_code=4)
            attempt.update(process=identity, process_status='running',
                           argv_sha256=hashlib.sha256(json.dumps(list(argv)).encode()).hexdigest(),
                           cwd=str(cwd) if cwd is not None else None, launched_at=now_iso())
            atomic_json(self.path, document)

    def process_exited(self, attempt_id, returncode, *, output=None):
        self._update(attempt_id, {'started'}, process_status='exited', process_returncode=returncode,
                     process_exited_at=now_iso(), output=output or {})

    def recover_process(self, attempt_id):
        """Clean owned children without overlapping a live deployment."""
        with file_lock(self.path.parent / 'deployment.lock', label='stand deployment', timeout=0):
            return self._recover_process_locked(attempt_id)

    def _recover_process_locked(self, attempt_id):
        attempt = self.snapshot()['attempts'].get(attempt_id)
        if not attempt or attempt['status'] in {'applied', 'resolved'}:
            raise GreatMindsError('deployment is not awaiting recovery', exit_code=4)
        if attempt.get('launch_protocol') != 'gated-v1':
            raise GreatMindsError('deployment has no gated child tracking; automatic cleanup cannot prove safety', exit_code=4)
        if attempt.get('process'):
            asyncio.run(terminate_group(attempt['process']))
        self._update(attempt_id, {'started', 'needs_recovery', 'command_finished'},
                     status='command_finished' if attempt['status'] == 'command_finished' else 'needs_recovery',
                     process_status='cleaned', cleanup='confirmed', cleanup_at=now_iso())
        return self.snapshot()['attempts'][attempt_id]

    def resolve(self, attempt_id, *, reason):
        """Acknowledge external uncertainty; never manufacture passing evidence."""
        with file_lock(self.path.parent / 'deployment.lock', label='stand deployment', timeout=0):
            return self._resolve_locked(attempt_id, reason=reason)

    def _resolve_locked(self, attempt_id, *, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise GreatMindsError('deployment resolution requires an operator explanation', exit_code=2)
        attempt = self.snapshot()['attempts'].get(attempt_id)
        if not attempt or attempt.get('cleanup') != 'confirmed':
            raise GreatMindsError('deployment requires confirmed process cleanup before resolution', exit_code=4)
        if attempt.get('process') and group_members(attempt['process']):
            raise GreatMindsError('deployment process group is still alive', exit_code=4)
        self._update(attempt_id, {'started', 'needs_recovery', 'command_finished'},
                     status='resolved', resolution=reason, resolved_at=now_iso())
        return self.snapshot()['attempts'][attempt_id]

    def inputs(self, attempt_id, *, before=None, after=None, context=None):
        fields = {}
        if context is not None:
            fields['inputs_context'] = context
        if before is not None:
            fields['inputs_before'] = before
        if after is not None:
            fields['inputs_after'] = after
            fields['inputs_match'] = self.snapshot()['attempts'][attempt_id].get('inputs_before') == after
        self._update(attempt_id, {'started'}, **fields)

    def finished(self, attempt_id, rc, log):
        # Raw logs may contain credentials; the ledger only retains identity.
        self._update(attempt_id, {'started'}, status='command_finished', exit_code=rc,
                     log_sha256=hashlib.sha256((log or '').encode()).hexdigest())

    def applied(self, attempt_id):
        self._update(attempt_id, {'command_finished'}, status='applied')

    def uncertain(self, attempt_id):
        self._update(attempt_id, {'started'}, status='needs_recovery',
                     reason='executor_raised_without_durable_result')
