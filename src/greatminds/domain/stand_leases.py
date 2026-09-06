"""Mechanical expiry of unused stand leases under durable runtime ownership."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import math
import os

from greatminds.cli import stand_state as ss
from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import file_lock, safe_name
from greatminds.runtime.processes import group_members
from greatminds.runtime.store import TERMINAL
from .stand_deployments import DeploymentLedger


def holder_hold(runtime, lease, snapshot):
    role, task = lease.get('holder_role'), lease.get('task')
    if not isinstance(role, str) or not role or not isinstance(task, str) or not task:
        return 'invalid_holder_identity'
    for run in snapshot['runs'].values():
        if run['role'] != role and run['task_id'] != task:
            continue
        if run['state'] not in TERMINAL:
            return 'active_run'
        if run.get('process'):
            try:
                if group_members(run['process']):
                    return 'live_process_group'
            except (OSError, ValueError, KeyError):
                return 'process_identity_unknown'
    if any(c['task_id'] == task and c['status'] in {'queued', 'starting', 'running', 'needs_recovery'}
           for c in snapshot.get('commands', {}).values()):
        return 'command_unresolved'
    if any(r['envelope']['task_id'] == task and r['status'] in {'received', 'applying', 'needs_recovery'}
           for r in snapshot['results'].values()):
        return 'domain_result_unresolved'
    try:
        safe_name(role)
        registry = runtime / '.agent_registry' / (role.lower() + '.json')
        if registry.exists():
            record = json.loads(registry.read_text())
            pid = record.get('pid')
            if type(pid) is not int or pid <= 0:
                return 'legacy_holder_unknown'
            try:
                os.kill(pid, 0)
                return 'legacy_holder_alive'
            except ProcessLookupError:
                pass
    except (OSError, ValueError, AttributeError, GreatMindsError):
        return 'legacy_holder_unknown'
    return None


def expiry_reason(lease, now):
    ttl = lease.get('ttl_seconds')
    if type(ttl) not in (int, float) or ttl <= 0:
        return 'invalid_expiry'
    try:
        if not math.isfinite(ttl):
            return 'invalid_expiry'
        start = datetime.fromisoformat(str(lease.get('granted_at')))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        expired = now >= start + timedelta(seconds=ttl)
    except (ValueError, TypeError, OverflowError):
        return 'invalid_expiry'
    return None if expired else 'within_ttl'


class StandLeaseService:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def inspect(self, state=None, snapshot=None):
        state = ss.read_stand_state(self.store.runtime) if state is None else state
        lease = state.get('active_lease')
        if not lease:
            return {'status': 'no_active_lease'}
        if not isinstance(lease, dict) or not lease.get('lease_id') or state.get('state') not in {'ready', 'preparing'}:
            return {'status': 'invalid_lease_state'}
        reason = expiry_reason(lease, self.clock())
        if reason is None:
            reason = holder_hold(self.store.runtime, lease, snapshot or self.store.snapshot())
        if reason is None:
            try:
                DeploymentLedger(self.store.runtime).require_resolved()
            except GreatMindsError:
                reason = 'deployment_unresolved'
        return {'status': reason or 'expired_dead_holder', 'lease_id': lease['lease_id']}

    def reconcile(self):
        if self.inspect()['status'] != 'expired_dead_holder':
            return
        with ExitStack() as locks:
            try:
                locks.enter_context(file_lock(self.store.runtime / '.stand/deployment.lock',
                                              label='stand deployment', timeout=0))
            except GreatMindsError:
                return
            # Holding the store lock through stand publication prevents a new
            # claim racing the dead-holder check. Stand history is the audit.
            with self.store._transaction() as snapshot:
                def reclaim(state):
                    finding = self.inspect(state, snapshot)
                    if finding['status'] != 'expired_dead_holder':
                        return
                    lease = state['active_lease']
                    state['active_lease'] = None
                    ss.record_transition(state, state['state'], 'free', 'SYSTEM',
                                         lease_id=lease['lease_id'],
                                         reason='expired lease; no active run, command, result, or live holder')
                    ss.promote_head_on_free(state, 'SYSTEM')
                ss.update_stand_state(self.store.runtime, reclaim)
