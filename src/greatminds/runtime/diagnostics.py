"""Read-only, allowlisted findings assembled from existing runtime services."""
from pathlib import Path
import os
import shutil

from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot, inspect_schema_copy
from .config import load_execution_config
from .store import RunStore, TERMINAL


def agent_prerequisites(config, environment):
    checks = []
    for agent in config.agents:
        effective = dict(environment)
        effective.update({dest: environment[ref] for dest, ref in agent.environment if ref in environment})
        executable = agent.argv[0]
        if os.path.isabs(executable):
            available = Path(executable).is_file() and os.access(executable, os.X_OK)
        elif '/' in executable:
            available = False
        else:
            path = os.pathsep.join(part for part in effective.get('PATH', os.defpath).split(os.pathsep)
                                   if os.path.isabs(part))
            available = shutil.which(executable, path=path) is not None
        missing = [key for key in agent.required_env if not environment.get(key)]
        checks.append({'agent': agent.id, 'executable_available': available,
                       'missing_required_env': missing, 'ready': available and not missing})
    return checks


ACTIONS = {
    'waiting_auth': 'Complete harness authentication in the daemon environment, then explicitly retry the run.',
    'waiting_input': 'Inspect run permissions and answer the outstanding operator request.',
    'failed': 'Inspect the run outcome and resolve its cause before requesting retry.',
    'interrupted': 'Inspect operation evidence before retrying interrupted work.',
    'input_budget_exceeded': 'Inspect input_budget; reduce input or explicitly adjust the budget/session.',
    'no_progress_limit': 'Review the task and result evidence; explicit retry does not reset its progress count.',
    'startup_retry_limit': 'Resolve the startup failure before authorizing another run.',
    'retry_backoff': 'Wait until the recorded retry admission time.',
    'account_backoff': 'Wait for the shared account admission time; inspect its startup failures.',
    'human_input_required': 'Resolve the task human-input request before authorizing further work.',
    'result_rejected': 'Inspect the result receipt and correct its contract or evidence violation.',
    'no_change_reported': 'Review the reported no-change result before authorizing more work.',
    'dispatch_paused': 'Use greatminds run resume when ready to admit new work.',
}


def diagnose(project, *, environment=None):
    """Best-effort component inspection; failures do not hide independent checks.

    No repair, subprocess, provider call or raw exception/command/prompt content.
    A no-findings result only describes the listed checks, not live compatibility.
    """
    project = Path(project).resolve()
    runtime = project_runtime_dir(project)
    report = {'version': 1, 'verification': 'local_inspection', 'checks': {}, 'findings': []}

    def finding(component, code, severity, action, **evidence):
        from .recovery_actions import recovery_actions
        report['findings'].append({'component': component, 'code': code, 'severity': severity,
                                   'action': action, 'evidence': evidence,
                                   'recovery_actions': recovery_actions(component, code, evidence, project=project)})

    def inspect(name, function):
        try:
            value = function()
        except Exception as exc:
            report['checks'][name] = 'failed'
            finding(name, 'inspection_failed', 'error',
                    'Inspect this component locally and restore valid state before retrying inspection.',
                    error_type=type(exc).__name__)
            return None
        report['checks'][name] = 'inspected'
        return value

    schema = inspect('schema', load_schema_snapshot)
    config = None
    if schema is not None:
        mirror = inspect('schema_mirror', lambda: inspect_schema_copy(schema, project))
        if mirror and mirror['status'] != 'current':
            finding('schema_mirror', 'mirror_not_current', 'warning',
                    'Compare greatminds project schema with the diagnostic mirror; setup preserves existing mirrors.',
                    status=mirror['status'])
        config = inspect('configuration', lambda: load_execution_config(project/'coordination/execution.yaml',
                                                                       roles=set(schema.document['roles'])))
    else:
        report['checks']['configuration'] = 'unavailable'
    if environment is None:
        from greatminds.cli.daemon import execution_environment
        environment = inspect('environment', lambda: execution_environment(project))
    else:
        report['checks']['environment'] = 'supplied'
    if config is not None and environment is not None:
        prerequisites = inspect('agent_prerequisites', lambda: agent_prerequisites(config, environment))
        for row in prerequisites or []:
            if not row['ready']:
                finding('agent_prerequisites', 'agent_prerequisites_missing', 'error',
                        'Install the configured ACP executable and supply required environment references.', **row)
    else:
        report['checks']['agent_prerequisites'] = 'unavailable'

    store = RunStore(runtime)
    state = inspect('runtime', store.snapshot)
    if state is not None:
        if state['paused']:
            finding('runtime', 'dispatch_paused', 'info', ACTIONS['dispatch_paused'])
        # Current active runs or latest terminal run per binding, as in operator views.
        if config is not None:
            from .observation import agent_rows
            rows = inspect('agents', lambda: agent_rows(project, config=config, state=state))
            for row in rows or []:
                status = row['state']
                evidence = {key: row[key] for key in ('run_id', 'binding_id', 'task_id', 'state')}
                if status in ('waiting_auth', 'waiting_input', 'failed', 'interrupted'):
                    code = row['reason'] if row['reason'] in ACTIONS else status
                    finding('agents', code, 'warning', ACTIONS[code], **evidence)
                elif status not in TERMINAL and row['pid'] is not None and row['alive'] is not True:
                    finding('agents', 'process_identity_unconfirmed', 'warning',
                            'Inspect supervisor recovery and process evidence; do not infer safe replay from a PID.', **evidence)
        for collection in ('results', 'commands', 'maintenance'):
            def pending_operations():
                items = state.get(collection, {})
                if not isinstance(items, dict) or any(not isinstance(item, dict) for item in items.values()):
                    raise ValueError('invalid operation collection')
                return [key for key, item in sorted(items.items()) if item.get('status') == 'needs_recovery']
            for key in inspect(collection, pending_operations) or []:
                finding(collection, 'operation_needs_recovery', 'error',
                        'Inspect the operation receipt and use its explicit recovery control; do not replay blindly.',
                        operation_id=key, status='needs_recovery')
        if schema is not None and config is not None:
            from .daemon import assignments
            rows = inspect('assignments', lambda: list(assignments(store, config, schema, snapshot=state)))
            for binding, task, reason in rows or []:
                if reason not in {'ready', 'active_run', 'dispatch_paused'}:
                    finding('assignments', reason, 'info' if reason.endswith('backoff') else 'warning',
                            ACTIONS.get(reason, 'Inspect greatminds run status for this assignment and its pending gates.'),
                            task_id=task.task_id, binding_id=binding.id)
    if schema is not None and state is not None and environment is not None:
        from greatminds.domain.maintenance import MaintenanceService
        dependencies = inspect('dependencies', lambda: MaintenanceService(store, schema, environment=environment or {}).inspect())
        for key, item in sorted((dependencies or {}).get('tasks', {}).items()):
            if item['status'] != 'ready':
                finding('dependencies', 'dependency_hold', 'warning',
                        'Inspect greatminds wake-check --json; correct semantic declarations or resolve the reported gate.',
                        task_id=item.get('task_id', key), status=item['status'],
                        reason_codes=sorted({reason['code'] for reason in item.get('reasons', []) if 'code' in reason}))
    else:
        report['checks']['dependencies'] = 'unavailable'
    if schema is not None:
        from greatminds.domain.filesystem_health import orphan_intents, stale_tasks, orphan_worktrees
        filesystem_checks = [
            ('intents', lambda: orphan_intents(runtime, schema.document), 'orphan_intent',
             'Inspect the transition intent and journal; do not remove uncertain recovery evidence.'),
            ('stale_tasks', lambda: stale_tasks(runtime, schema.document), 'stale_task',
             'Inspect task/run progress and its admission hold; age alone does not authorize retry.'),
            ('worktrees', lambda: orphan_worktrees(project, runtime, schema.document), 'orphan_worktree',
             'Inspect the worktree and task history before explicit worktree pruning.'),
        ]
        for component, check, code, action in filesystem_checks:
            for row in inspect(component, check) or []:
                evidence = {key: row[key] for key in ('age_seconds', 'threshold_seconds', 'task_id') if key in row}
                evidence['artifact_id'] = row['name']
                finding(component, code, 'warning', action, **evidence)
    else:
        for component in ('intents', 'stale_tasks', 'worktrees'):
            report['checks'][component] = 'unavailable'
    from greatminds.domain.stand_deployments import DeploymentLedger
    deployments = inspect('deployments', lambda: DeploymentLedger(runtime).snapshot())
    for key, item in sorted((deployments or {}).get('attempts', {}).items()):
        if item['status'] in {'started', 'command_finished', 'needs_recovery'}:
            finding('deployments', 'deployment_unresolved', 'warning',
                    'Inspect stand deployment evidence and daemon reconciliation before authorizing another deployment.',
                    operation_id=key, status=item['status'])
    report['findings'].sort(key=lambda row: (row['component'], row['code'], str(row['evidence'])))
    report['summary'] = {level: sum(row['severity'] == level for row in report['findings'])
                         for level in ('error', 'warning', 'info')}
    report['status'] = 'findings' if report['findings'] else 'no_findings'
    return report
