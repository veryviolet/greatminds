"""Read-only execution observations for a local Linux project migration.

Absence of observed holders is not a launch barrier. Application must additionally
exclude new launches and stop prior-version services before publishing a contract.
"""
import json
import hashlib
import os
from pathlib import Path

from greatminds.core.paths import project_runtime_dir
from .processes import process_identity, group_members


def execution_barrier(project, *, exclusive=False):
    """Stable cross-layout lock; migration takes exclusive, supervisors shared."""
    from greatminds.core.storage import file_lock
    key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()
    return file_lock(Path('/tmp')/f'greatminds-execution-{os.getuid()}-{key}.lock',
                     label='project execution/migration', timeout=0, shared=not exclusive)


def inspect_execution(project):
    project = project.resolve()
    runtime = project_runtime_dir(project)
    holds = []

    def hold(kind, identity=None):
        item = {'kind': kind}
        if identity is not None:
            item['identity'] = identity
        if item not in holds:
            holds.append(item)

    # Ignore this review process and its parent chain, not unrelated project work.
    ancestors = set()
    pid = os.getpid()
    while pid > 0 and pid not in ancestors:
        ancestors.add(pid)
        try:
            pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break

    for path in sorted((runtime/'.agent_registry').glob('*.json')):
        try:
            record = json.loads(path.read_text())
            pid = record.get('pid')
            if type(pid) is not int or pid <= 0:
                hold('unreadable_registry', path.name)
            elif process_identity(pid) is not None:
                hold('registered_process_alive', path.name)
        except (OSError, ValueError, TypeError, AttributeError, IndexError):
            hold('unreadable_registry', path.name)

    state_path = runtime/'.runtime/state.json'
    if state_path.exists():
        try:
            from .store import RunStore, TERMINAL
            state = RunStore(runtime).snapshot()
            for run in state['runs'].values():
                if run['state'] not in TERMINAL:
                    hold('active_run', run['id'])
                identity = run.get('process')
                if identity and (process_identity(identity['pid']) == identity or group_members(identity)):
                    hold('owned_process_alive', run['id'])
            for name, unresolved in (
                ('commands', {'queued','starting','running','needs_recovery'}),
                ('results', {'received','applying','needs_recovery'}),
                ('maintenance', {'prepared','needs_recovery'}),
            ):
                for key, item in state.get(name, {}).items():
                    if item['status'] in unresolved:
                        hold('unresolved_'+name, key)
        except Exception:
            hold('unreadable_runtime_state')

    from greatminds.domain.stand_deployments import DeploymentLedger
    ledger = DeploymentLedger(runtime)
    if ledger.path.exists():
        try:
            for key, attempt in ledger.snapshot()['attempts'].items():
                if attempt['status'] not in {'applied','resolved'}:
                    hold('unresolved_deployment', key)
        except Exception:
            hold('unreadable_deployment_state')

    proc = Path('/proc')
    if not (proc/'self/stat').is_file():
        hold('process_inventory_unavailable')
    else:
        for path in proc.iterdir():
            if not path.name.isdigit() or int(path.name) in ancestors:
                continue
            try:
                if path.stat().st_uid != os.getuid():
                    continue
                identity = process_identity(int(path.name))
                if identity is None:
                    continue
                cwd = (path/'cwd').resolve(strict=True)
                associated = cwd.is_relative_to(project)
                # Also detect detached coordinators with an explicit project path.
                args = (path/'cmdline').read_bytes().split(b'\0')
                for i, argument in enumerate(args):
                    if argument == b'--project-dir' and i+1 < len(args):
                        associated |= (cwd/os.fsdecode(args[i+1])).resolve() == project
                    elif argument.startswith(b'--project-dir='):
                        associated |= (cwd/os.fsdecode(argument.split(b'=',1)[1])).resolve() == project
                if associated:
                    hold('project_process_alive', path.name)
            except FileNotFoundError:
                continue  # Process exited during inventory.
            except (OSError, ValueError, IndexError):
                hold('process_inventory_incomplete', path.name)
    return {'holds': holds, 'observed_quiet': not holds,
            'scope': 'current-user processes, project registry, runtime and deployment ledgers',
            'launch_exclusion_verified': False}
