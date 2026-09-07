"""Bounded local diagnostic export; no raw runtime/configuration documents."""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import secrets
import tempfile

from greatminds.core.paths import project_runtime_dir
from .diagnostics import ACTIONS, diagnose
from .observation import configuration
from .protocol_evidence import summary as protocol_summary
from .store import RunStore, TERMINAL, TRANSITIONS


STAGES = {'workspace_ready', 'context_ready', 'process_recorded', 'protocol_ready',
          'session_ready', 'first_prompt_started', 'first_protocol_activity',
          'first_prompt_activity', 'cleanup_complete'}
STATES = set(TRANSITIONS) | set(TERMINAL)
CODES = set(ACTIONS) | {'inspection_failed', 'mirror_not_current', 'agent_prerequisites_missing',
                      'process_identity_unconfirmed', 'operation_needs_recovery', 'dependency_hold',
                      'deployment_unresolved', 'orphan_intent', 'stale_task', 'orphan_worktree', 'transport_failure', 'configuration_error', 'timeout',
                      'turn_ended', 'authentication_required', 'permission_required', 'operator_cancelled',
                      'supervisor_restart', 'protocol_error', 'no_progress_backoff'}
EVENTS = STATES | {'run_claimed', 'claimed', 'run_stage_observed', 'prompt_input_reserved',
                   'run_recovered', 'process_recorded', 'workspace_ready', 'workspace_preparing',
                   'result_received', 'result_applied', 'result_rejected', 'control_requested',
                   'control_completed', 'control_processing', 'dispatch_paused', 'dispatch_resumed',
                   'events_pruned', 'event_retention_configured', 'protocol_observed', 'protocol_trace_truncated'}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def collect_bundle(project, *, report=None, environment=None, run_limit=100, event_limit=200, finding_limit=200):
    if type(run_limit) is not int or not 1 <= run_limit <= 1000 or type(event_limit) is not int or not 1 <= event_limit <= 10000:
        raise ValueError('invalid diagnostic bundle limits')
    if type(finding_limit) is not int or not 1 <= finding_limit <= 10000:
        raise ValueError('invalid finding limit')
    salt = secrets.token_bytes(32)
    def ref(value):
        if value is None:
            return None
        return hashlib.sha256(salt + str(value).encode()).hexdigest()[:20]
    def version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None
    source_report = report if report is not None else diagnose(project, environment=environment)
    findings = []
    selected_findings = sorted(source_report['findings'],
                               key=lambda row: ({'error': 0, 'warning': 1, 'info': 2}[row['severity']], row['component']))
    for item in selected_findings[:finding_limit]:
        evidence = item['evidence']
        findings.append({key: item[key] for key in ('component', 'severity', 'action')} | {
            'code': item['code'] if item['code'] in CODES else 'other_hold',
            'references': {key: ref(evidence[key]) for key in
                           ('run_id', 'task_id', 'binding_id', 'operation_id', 'agent', 'artifact_id') if key in evidence}})
    bundle = {'version': 1, 'kind': 'greatminds_local_diagnostics',
              'versions': {'greatminds': version('greatminds'), 'acp_sdk': version('agent-client-protocol'),
                           'python': platform.python_version()},
              'report': {'version': source_report['version'], 'verification': source_report['verification'],
                         'checks': source_report['checks'], 'summary': source_report['summary'],
                         'status': source_report['status'], 'findings': findings},
              'collection': {}, 'configuration': None, 'runs': [], 'events': [],
              'truncated': {'runs': None, 'events': None, 'findings': len(selected_findings) > finding_limit},
              'limits': {'runs': run_limit, 'events': event_limit, 'findings': finding_limit}}
    try:
        schema, config = configuration(project)
        bundle['configuration'] = {'schema_sha256': schema.sha256, 'execution_sha256': config.sha256,
            'max_runtime_events': config.max_runtime_events,
            'agents': [{'reference': ref(a.id), 'manifest_sha256': a.sha256,
                        'required_environment_count': len(a.required_env)} for a in config.agents],
            'bindings': [{'reference': ref(b.id), 'agent_reference': ref(b.agent),
                          'binding_sha256': b.sha256, 'scheduling': b.scheduling, 'permission': b.permission,
                          'max_running': b.max_running, 'timeout_seconds': b.timeout_seconds,
                          'max_prompt_bytes': b.max_prompt_bytes, 'max_session_input_bytes': b.max_session_input_bytes,
                          'max_startup_retries': b.max_startup_retries, 'max_no_progress_turns': b.max_no_progress_turns}
                         for b in config.bindings]}
        bundle['collection']['configuration'] = 'inspected'
    except Exception:
        bundle['collection']['configuration'] = 'unavailable'
    try:
        state = RunStore(project_runtime_dir(Path(project))).snapshot()
        runs = sorted(state['runs'].values(), key=lambda row: row['sequence'])
        rows = []
        for run in runs[-run_limit:]:
            rows.append({'reference': ref(run['id']), 'task_reference': ref(run['task_id']),
                         'binding_reference': ref(run['binding_id']), 'agent_reference': ref(run['agent_id']),
                         'state': run['state'] if run['state'] in STATES else 'unknown',
                         'protocol': protocol_summary(run.get('protocol')),
                         'reason': run.get('reason') if run.get('reason') in CODES else 'other',
                         'created_at': number(run.get('created_at')), 'updated_at': number(run.get('updated_at')),
                         'timings': {key: number(value) for key, value in run.get('timings', {}).items() if key in STAGES},
                         'metrics': {key: number(run.get('outcome', {}).get(key)) for key in
                                     ('elapsed_seconds', 'context_bytes', 'input_bytes_reserved',
                                      'session_input_bytes_reserved', 'updates')}})
        events = [{'sequence': number(event.get('sequence')), 'at': number(event.get('at')),
                   'kind': event.get('kind') if event.get('kind') in EVENTS else 'other',
                   'run_reference': ref(event.get('run_id'))} for event in state['events'][-event_limit:]]
        bundle['runs'], bundle['events'] = rows, events
        retention = state.get('event_retention', {})
        bundle['event_retention'] = {key: number(retention.get(key)) for key in
                                     ('max_events', 'discarded_count', 'discarded_through', 'updated_at')}
        bundle['truncated'].update(runs=len(runs) > run_limit,
            events=bool(retention.get('discarded_count')) or len(state['events']) > event_limit)
        bundle['collection']['runtime'] = 'inspected'
    except Exception:
        bundle['collection']['runtime'] = 'unavailable'
    return bundle


def write_bundle(path, bundle):
    """Publish one private file atomically, refusing existing paths and symlinks."""
    path = Path(path).absolute()
    data = (json.dumps(bundle, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('diagnostic bundle exceeds 5 MiB export limit')
    descriptor, temporary = tempfile.mkstemp(prefix='.greatminds-diagnostics-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic publication with no replacement.
    finally:
        os.unlink(temporary)
    return path
