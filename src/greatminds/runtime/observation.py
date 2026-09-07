"""Read-only operator observations of configured ACP bindings and durable runs."""
from pathlib import Path

from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from .config import load_execution_config
from .processes import process_identity
from .store import RunStore, TERMINAL


def configuration(project):
    schema = load_schema_snapshot()
    return schema, load_execution_config(Path(project)/'coordination/execution.yaml',
                                         roles=set(schema.document['roles']))


def manifests(project):
    _, config = configuration(project)
    return [{'id': agent.id, 'transport': 'acp', 'adapter_version': agent.adapter_version,
             'harness_version': agent.harness_version,
             'bindings': [b.id for b in config.bindings if b.agent == agent.id],
             'verification': 'configured'} for agent in config.agents]


def agent_rows(project, *, config=None, state=None):
    if config is None:
        _, config = configuration(project)
    snapshot = RunStore(project_runtime_dir(Path(project))).snapshot() if state is None else state
    bindings = {binding.id: binding for binding in config.bindings}
    grouped = {key: [] for key in bindings}
    for run in snapshot['runs'].values():
        grouped.setdefault(run['binding_id'], []).append(run)
    rows = []
    for binding_id, runs in sorted(grouped.items()):
        binding = bindings.get(binding_id)
        active = [run for run in runs if run['state'] not in TERMINAL]
        selected = active or ([max(runs, key=lambda r: (r['created_at'], r.get('sequence', 0)))] if runs else [None])
        for run in selected:
            identity = run.get('process') if run else None
            alive = False
            if identity:
                try:
                    alive = process_identity(identity['pid']) == identity
                except (OSError, ValueError, KeyError, IndexError):
                    alive = None
            state = run['state'] if run else ('paused' if snapshot['paused'] else 'idle')
            rows.append({
                'binding_id': binding_id, 'role': run['role'] if run else binding.role,
                'agent_id': run['agent_id'] if run else binding.agent, 'transport': 'acp',
                'configured': binding is not None, 'state': state,
                'reason': run.get('reason', '') if run else '',
                'run_id': run['id'] if run else None,
                'task_id': run['task_id'] if run else None,
                'task_revision': run['task_revision'] if run else None,
                'session_id': run.get('session_id') if run else None,
                'conversation_id': run.get('conversation_id') if run else None,
                'updated_at': run['updated_at'] if run else None,
                'config_current': run['config_sha256'] == config.sha256 if run else True,
                'pid': identity['pid'] if identity else None, 'alive': alive,
                'usable': state == 'running' and alive is True,
                'dispatch_paused': snapshot['paused'],
            })
    return rows


def snapshot(project):
    """Best-effort observation across stores; claims revalidate before execution."""
    from .daemon import assignments
    from .retry_policy import startup_retry, account_backoff
    from .stand_scheduler import StandScheduler
    from greatminds.domain.stand_deployments import DeploymentLedger
    from greatminds.domain.stand_leases import StandLeaseService
    from greatminds.domain.maintenance import MaintenanceService

    project = Path(project).resolve()
    schema, config = configuration(project)
    store = RunStore(project_runtime_dir(project))
    result = store.snapshot()
    result['stand_deployments'] = DeploymentLedger(store.runtime).snapshot()
    result['stand_lease'] = StandLeaseService(store).inspect()
    stand = StandScheduler(store, config.stand)
    result['stand_dispatch'] = stand.inspect()
    result['stand_schedule'] = stand.snapshot()
    result['maintenance_findings'] = MaintenanceService(store, schema).inspect()
    result['assignments'] = [{'task_id': task.task_id, 'binding': binding.id,
                              'role': binding.role, 'reason': reason,
                              'retry': startup_retry(result, binding, task, config, schema, store.clock())}
                             for binding, task, reason in assignments(store, config, schema, snapshot=result)]
    result['accounts'] = {account: account_backoff(result, account, config, store.clock())
                          for account in sorted({b.account for b in config.bindings})}
    result['agents'] = agent_rows(project, config=config, state=result)
    from greatminds.cli.task import load_task
    result['tasks'] = []
    for queue in schema.document['queues']:
        if queue.startswith('.'):
            continue
        for path in sorted((store.runtime/queue).glob('*.yaml')):
            try:
                task = load_task(path)
                row = {'id': path.stem, 'queue': queue, 'title': task.get('title', '')}
            except Exception:
                row = {'id': path.stem, 'queue': queue, 'error': 'unreadable_task'}
            result['tasks'].append(row)
    return result
