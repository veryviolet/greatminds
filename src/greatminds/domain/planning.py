"""Authenticated planning requests, applied by the daemon using task gates.

A standalone conversation can operate on several planner-owned tasks. It never
acquires another role's authority or changes its run's assigned subject.
"""
from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import parse_schema_document
from greatminds.core.storage import atomic_bytes, safe_name, task_lock
from greatminds.core.util import now_iso
from greatminds.runtime.config import fingerprint
from greatminds.runtime.store import RunStore, TaskRevision, TERMINAL
from .results import ResultService

PENDING = {'received', 'applying', 'needs_recovery'}


class PlanningService:
    def __init__(self, store, *, environment=None, checkpoint=None):
        self.store = store
        self.results = ResultService(store, environment=environment, checkpoint=checkpoint)
        self.checkpoint = checkpoint or (lambda stage: None)

    def _caller(self, run_id, token, *, active=True):
        run = self.store.authorize(run_id, token, active=active)
        if run['role'] != 'ARCHITECT-PLANNER' or not run.get('conversation_id') or run.get('conversation_task'):
            raise GreatMindsError('planning requests require a standalone planner conversation', exit_code=3)
        return run

    def get(self, identity):
        safe_name(identity)
        item = self.store.snapshot().get('planning', {}).get(identity)
        if item is None:
            raise GreatMindsError('unknown planning request', exit_code=2)
        return item

    @staticmethod
    def _busy(state, task_id, *, ignore=None):
        return (any(r['task_id'] == task_id and r['state'] not in TERMINAL for r in state['runs'].values())
            or any(r['task_id'] == task_id and r['id'] != ignore and r['status'] in PENDING for r in state.get('planning', {}).values())
            or any(r['envelope']['task_id'] == task_id and r['status'] in PENDING for r in state['results'].values())
            or any(r['task_id'] == task_id and r['status'] in {'queued', 'starting', 'running', 'needs_recovery'} for r in state.get('commands', {}).values())
            or any(r['task_id'] == task_id and r['status'] in {'prepared', 'needs_recovery'} for r in state.get('maintenance', {}).values()))

    def request(self, action, payload, *, run_id, token, request_id=None):
        from greatminds.cli import task as policy
        if action not in {'create', 'decide', 'plan', 'append', 'move'} or not isinstance(payload, dict):
            raise GreatMindsError('unsupported planning action or payload')
        try:
            encoded = json.dumps(payload, allow_nan=False).encode()
        except (ValueError, TypeError) as exc:
            raise GreatMindsError('planning payload must contain finite JSON values') from exc
        if len(encoded) > 65536:
            raise GreatMindsError('planning request exceeds 64 KiB')
        run = self._caller(run_id, token, active=False)
        request = {'action': action, 'payload': payload}
        digest = fingerprint(request)
        key = safe_name(request_id) if request_id is not None else digest
        identity = 'planning-' + fingerprint([run['conversation_id'], key])[:32]
        contract = parse_schema_document(self.store.contracts(run_id)['schema']['text'])
        # Serialize deduplication and ID reservation with all runtime admissions.
        with self.store._transaction() as state:
            old = state.get('planning', {}).get(identity)
            if old:
                if old['digest'] != digest:
                    raise GreatMindsError('planning request ID already has different contents', exit_code=4)
                return copy.deepcopy(old)
            self._caller(run_id, token)
            if sum(r['status'] in PENDING for r in state.get('planning', {}).values()) >= 64:
                raise GreatMindsError('planning request queue is full', exit_code=4)
            item = {'id': identity, 'run_id': run_id, 'conversation_id': run['conversation_id'],
                    'digest': digest, 'action': action, 'status': 'received', 'at': now_iso(),
                    'steps': [], 'receipts': {}, 'index': 0}
            if action == 'create':
                args = dict(payload)
                if not isinstance(args.get('title'), str):
                    raise GreatMindsError('task title must be text', exit_code=2)
                description = args.pop('description', None)
                with policy.domain_context(document=contract, runtime=self.store.runtime,
                                           workspace=Path(run['workspace'])):
                    try:
                        path, data = policy.prepare_new_task(coord=self.store.runtime, role=run['role'], **args)
                    except TypeError as exc:
                        raise GreatMindsError('unsupported or missing task creation fields') from exc
                    if description is not None:
                        if not isinstance(description, str):
                            raise GreatMindsError('task description must be text')
                        data['description'] = description
                    queue = path.parent.name
                    if (run['role'] not in contract['queues'][queue].get('writers', [])
                            and queue not in contract['roles'][run['role']]['claims_from']):
                        raise GreatMindsError('planner cannot create tasks in this queue', exit_code=3)
                    policy.validate_task(data)
                item.update(task_id=data['id'], destination=str(path.relative_to(self.store.runtime)),
                            document=yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            else:
                if action != 'decide':
                    payload = self._decisions(action, payload)
                if set(payload) - {'task_id', 'revision', 'steps'}:
                    raise GreatMindsError('unknown planning decision fields')
                task_id = safe_name(payload.get('task_id'))
                found = policy.find_task(self.store.runtime, task_id)
                if not found:
                    raise GreatMindsError('planning task not found')
                revision = TaskRevision.capture(self.store.runtime, found[0])
                if revision.task_id != task_id:
                    raise GreatMindsError('planning requires the exact task ID')
                if found[1] not in contract['roles'][run['role']]['claims_from']:
                    raise GreatMindsError('planner does not own this queue', exit_code=3)
                if payload.get('revision') != revision.sha256:
                    raise GreatMindsError('stale planning task revision', exit_code=4)
                steps = payload.get('steps')
                if not isinstance(steps, list) or not 1 <= len(steps) <= 8 or any(
                        not isinstance(s, dict) or set(s) != {'decision', 'payload'} or
                        not isinstance(s['decision'], str) or
                        s['decision'] not in {'handoff', 'blocked', 'needs_input', 'no_change'} or
                        not isinstance(s['payload'], dict) for s in steps):
                    raise GreatMindsError('planning requires 1–8 typed decision steps')
                # Every step is validated again against the pinned schema at application.
                item.update(task_id=task_id, source_queue=found[1], revision=asdict(revision), steps=copy.deepcopy(steps))
            if self._busy(state, item['task_id']):
                raise GreatMindsError('task has active work or an unresolved operation', exit_code=4)
            state.setdefault('planning', {})[identity] = item
            self.store._event(state, 'planning_received', run_id, {'request_id': identity, 'task_id': item['task_id']})
            return copy.deepcopy(item)

    def _decisions(self, action, payload):
        from greatminds.cli import task as policy
        found = policy.find_task(self.store.runtime, payload.get('task_id', ''))
        if not found:
            raise GreatMindsError('planning task not found')
        revision = TaskRevision.capture(self.store.runtime, found[0])
        if action == 'move':
            if set(payload) != {'task_id', 'to_queue', 'reason'}:
                raise GreatMindsError('invalid move fields')
            steps = [{'decision': 'handoff', 'payload': {'to_queue': payload['to_queue'],
                                                        'reason': payload['reason']}}]
        elif action == 'append':
            if set(payload) != {'task_id', 'block'}:
                raise GreatMindsError('invalid append fields')
            steps = [{'decision': 'no_change', 'payload': {'blocks': [payload['block']]}}]
        else:
            if set(payload) != {'task_id', 'scope', 'fields', 'body', 'triage', 'stop_at', 'audit_only'}:
                raise GreatMindsError('invalid plan fields')
            if found[1] not in {'feature_inbox', 'user_feedback'}:
                raise GreatMindsError('plan requires feature_inbox or user_feedback')
            scope = payload['scope']
            if scope not in {'backend', 'ui', 'docs'} or payload['audit_only'] and scope != 'docs':
                raise GreatMindsError('invalid planning scope')
            data = yaml.safe_load(found[0].read_text())
            if data.get('scope') != scope:
                raise GreatMindsError('plan scope must match task scope')
            target = 'feature_inbox' if found[1] == 'user_feedback' else 'feature_plan'
            steps = [{'decision': 'handoff', 'payload': {'to_queue': target, 'blocks': [
                {'kind': 'triage', 'notes': payload['triage'] or f'triaged for planning (scope:{scope})'}]}}]
            if found[1] == 'user_feedback':
                steps.append({'decision': 'handoff', 'payload': {'to_queue': 'feature_plan'}})
            plan = {**payload['fields'], 'kind': 'plan', 'body': payload['body']}
            if payload['audit_only']:
                plan['audit_only'] = True
            final = {'blocks': [plan]}
            if payload['stop_at'] != 'plan':
                final['to_queue'] = 'feature_docs_review' if payload['audit_only'] else {
                    'backend': 'feature_dev', 'ui': 'feature_ui_dev', 'docs': 'feature_docs'}[scope]
            steps.append({'decision': 'handoff' if 'to_queue' in final else 'no_change', 'payload': final})
        return {'task_id': revision.task_id, 'revision': revision.sha256, 'steps': steps}

    def _update(self, identity, **changes):
        with self.store._transaction() as state:
            item = state['planning'][identity]
            item.update(changes)
            if item['status'] == 'applied':
                item.pop('error', None)
            return copy.deepcopy(item)

    def reconcile(self):
        for item in self.store.snapshot().get('planning', {}).values():
            if item['status'] in {'received', 'applying'}:
                try:
                    self.apply(item['id'])
                except (GreatMindsError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    # Hold uncertain work rather than taking down unrelated
                    # conversations/queues on a damaged operation or I/O error.
                    self._update(item['id'], status='needs_recovery', error=str(exc))

    def apply(self, identity):
        from greatminds.cli import task as policy
        item = self.get(identity)
        with task_lock(self.store.runtime, item['task_id']):
            item = self.get(identity)
            if item['status'] not in {'received', 'applying'}:
                return item
            state = self.store.snapshot()
            if self._busy(state, item['task_id'], ignore=identity):
                return self._update(identity, status='rejected', error='task acquired conflicting work')
            run = state['runs'][item['run_id']]
            self._update(identity, status='applying')
            if item['action'] == 'create':
                path = self.store.runtime / item['destination']
                raw = item['document'].encode()
                if path.exists() and path.read_bytes() != raw:
                    return self._update(identity, status='needs_recovery', error='intake task changed before recovery')
                if not path.exists():
                    if any(self.store.runtime.glob('*/' + item['task_id'] + '.yaml')):
                        return self._update(identity, status='needs_recovery', error='intake task moved before recovery')
                    atomic_bytes(path, raw)
                self.checkpoint('task_written')
                policy.journal_append(self.store.runtime, {'t': item['at'], 'actor': 'SYSTEM',
                    'actor_kind': 'system', 'decision_by': run['role'], 'task': item['task_id'],
                    'from': '_new', 'to': path.parent.name, 'reason': 'planner intake',
                    'operation_id': identity, 'run_id': run['id'], 'intent_id': identity})
                self.checkpoint('journaled')
                return self._update(identity, status='applied')
            while item['index'] < len(item['steps']):
                index = item['index']
                step_id = f'{identity}-{index}'
                scoped = dict(run, task_id=item['task_id'], task_path=item['revision']['path'],
                              task_revision=item['revision']['sha256'])
                prior = item['receipts'].get(step_id, {'status': 'received'})
                receipt = {**prior, 'planning': True,
                           'envelope': {**item['steps'][index], 'result_id': step_id}}
                def finish(_result_id, status, *, details=None):
                    nonlocal item
                    with self.store._transaction() as state:
                        current = state['planning'][identity]
                        current['receipts'][step_id] = {'status': status, 'details': details}
                        if status == 'applied':
                            current['index'] = index + 1
                            current['destination'] = details['destination']
                            current['revision'] = {'task_id': item['task_id'], 'path': details['destination'],
                                                   'sha256': details['destination_revision']}
                        elif status in {'rejected', 'needs_recovery'}:
                            current.update(status=status, error=details['error'])
                        item = copy.deepcopy(current)
                    return item['receipts'][step_id]
                self.results.apply_operation(step_id, receipt, scoped,
                    self.store.directory / 'operations' / (step_id + '.json'), finish, track_validation=False)
                if item['status'] in {'rejected', 'needs_recovery'}:
                    return item
            return self._update(identity, status='applied')


def request_from_cli(action, payload, *, request_id=None, wait_seconds=30):
    """Compatibility commands wait for daemon receipts, never apply agent writes."""
    from greatminds.core.paths import find_project_dir, project_runtime_dir
    service = PlanningService(RunStore(project_runtime_dir(find_project_dir())))
    run_id, token = os.environ.get('GREATMINDS_RUN_ID'), os.environ.get('GREATMINDS_RUN_TOKEN')
    if not run_id or not token:
        raise GreatMindsError('planning requires a managed planner conversation', exit_code=3)
    item = service.request(action, payload, run_id=run_id, token=token, request_id=request_id)
    deadline = time.monotonic() + wait_seconds
    while item['status'] in {'received', 'applying'} and time.monotonic() < deadline:
        time.sleep(.1)
        item = service.get(item['id'])
    if item['status'] != 'applied':
        raise GreatMindsError(f"planning request {item['id']}: {item['status']}; "
                             + item.get('error', 'inspect with greatminds run planning-status'), exit_code=4)
    return item
