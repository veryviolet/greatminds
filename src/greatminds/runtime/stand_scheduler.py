"""Authorized stand dispatch in a separate worker, with durable no-replay tickets."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
import threading

from greatminds.cli import stand_state as ss
from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_json, file_lock
from greatminds.core.util import now_iso
from greatminds.domain.stand_deployments import DeploymentLedger
from .config import fingerprint


class StandScheduler:
    def __init__(self, store, policy):
        self.store, self.policy = store, policy
        self.path = store.runtime / '.stand/schedule.json'
        self.pool = None
        self.future = None
        self.cancel = threading.Event()

    def snapshot(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'version': 1, 'tickets': {}}
        except (ValueError, OSError) as exc:
            raise GreatMindsError('cannot read stand schedule', exit_code=4) from exc
        if (not isinstance(value, dict) or value.get('version') != 1
                or not isinstance(value.get('tickets'), dict)
                or any(not isinstance(ticket, dict)
                       or ticket.get('status') not in {'selected','completed','failed','cancelled'}
                       for ticket in value['tickets'].values())):
            raise GreatMindsError('invalid stand schedule', exit_code=4)
        return value

    def _identity(self, lease):
        return fingerprint({'lease': lease, 'policy': asdict(self.policy)})

    def inspect(self):
        if self.policy is None or not self.policy.authorized:
            return {'status': 'automatic_deployment_disabled'}
        if self.store.snapshot()['paused']:
            return {'status': 'dispatch_paused'}
        state = ss.read_stand_state(self.store.runtime)
        lease = state.get('active_lease')
        if state.get('state') != 'preparing' or not isinstance(lease, dict) or not lease.get('lease_id'):
            return {'status': 'no_preparing_lease'}
        if lease.get('profile') not in self.policy.profiles:
            return {'status': 'profile_not_authorized', 'lease_id': lease['lease_id']}
        ticket = self._identity(lease)
        prior = self.snapshot()['tickets'].get(ticket)
        if prior:
            return {'status': 'already_attempted', 'ticket': ticket, 'outcome': prior['status'],
                    'lease_id': lease['lease_id'], 'error': prior.get('error')}
        try:
            DeploymentLedger(self.store.runtime).require_resolved()
        except GreatMindsError:
            return {'status': 'deployment_unresolved'}
        return {'status': 'ready', 'ticket': ticket, 'lease': lease}

    def _execute(self):
        from greatminds.cli.stand import _deploy_lease_locked
        if self.cancel.is_set():
            return
        # Exclude operator deploy/recovery/reclaim for the complete worker scope.
        guard = file_lock(self.store.runtime / '.stand/deployment.lock', label='stand deployment', timeout=0)
        try:
            guard.__enter__()
        except GreatMindsError:
            return
        try:
            # Serialize the dispatch decision against pause and new claims.
            with self.store._transaction() as state:
                if state['paused'] or self.cancel.is_set():
                    return
                finding = self.inspect()
                if finding['status'] != 'ready':
                    return
                ticket, lease = finding['ticket'], finding['lease']
                schedule = self.snapshot()
                schedule['tickets'][ticket] = {'status': 'selected', 'lease_id': lease['lease_id'],
                    'profile': lease['profile'], 'policy_sha256': fingerprint(asdict(self.policy)), 'at': now_iso()}
                atomic_json(self.path, schedule)
            try:
                if self.cancel.is_set():
                    raise InterruptedError('daemon shutdown before deployment')
                rc, _ = _deploy_lease_locked(self.store.runtime, lease_id=lease['lease_id'],
                    expected_lease=lease, allowed_profiles=self.policy.profiles,
                    timeout_seconds=self.policy.timeout_seconds, output_limit=self.policy.max_output_bytes,
                    cancel_event=self.cancel)
                outcome = {'status': 'completed' if rc == 0 else 'failed', 'exit_code': rc}
            except (Exception, KeyboardInterrupt) as exc:
                outcome = {'status': 'cancelled' if self.cancel.is_set() else 'failed',
                           'error_type': type(exc).__name__}
                if isinstance(exc, GreatMindsError):
                    from .permissions import redact
                    outcome['error'] = redact(str(exc), os.environ.values())[:1000]
            schedule = self.snapshot()
            schedule['tickets'][ticket].update(outcome, finished_at=now_iso())
            atomic_json(self.path, schedule)
        finally:
            guard.__exit__(None, None, None)

    def poll(self):
        if self.future is not None:
            if not self.future.done():
                return
            self.future.result()  # Storage failures must stop the supervisor.
            self.future = None
        if self.inspect()['status'] != 'ready':
            return
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='greatminds-stand')
        self.future = asyncio.get_running_loop().run_in_executor(self.pool, self._execute)

    @property
    def busy(self):
        return self.future is not None

    async def close(self):
        self.cancel.set()
        try:
            if self.future is not None:
                while not self.future.done():
                    await asyncio.wait({self.future}, timeout=.1)
                self.future.result()
                self.future = None
        finally:
            if self.pool is not None:
                self.pool.shutdown(wait=True, cancel_futures=True)
