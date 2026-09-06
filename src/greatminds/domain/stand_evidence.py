"""Source and local environment identities for managed profile execution."""
import hashlib
import os
import stat
from pathlib import Path

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import safe_name
from greatminds.runtime.commands import source_identity, environment_identity


def deployment_inputs(runtime, workspace, profile, argv, environment, extra_vars):
    workspace = Path(workspace).resolve()
    profile = Path(profile).resolve()
    if not workspace.is_dir() or not profile.is_file():
        raise GreatMindsError('deployment source or profile is unavailable', exit_code=4)
    # Use the same private HMAC key and executable identity as configured checks.
    identity = environment_identity(runtime / '.runtime', environment, argv, runtime,
                                    context={'playbook_vars': extra_vars})
    digest = hashlib.sha256()
    with profile.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return {'environment_revision': environment_revision(runtime), 'workspace': str(workspace), 'source': source_identity(workspace, runtime),
            'profile': str(profile), 'profile_sha256': digest.hexdigest(), 'environment': identity}


_CONTEXT_FIELDS = {'coord','lease_id','profile','profile_file','profile_source','profile_path',
                   'worktree','task_id','task','deploy_prerequisites_only'}


def deployment_environment(source=None):
    source = os.environ if source is None else source
    operational = {'GREATMINDS_RUN_ID','GREATMINDS_RUN_TOKEN','GREATMINDS_ROLE','GREATMINDS_PROJECT_DIR',
                   'PWD','OLDPWD','SHLVL','_','COLUMNS','LINES','TERM'}
    return {**{k:v for k,v in source.items() if k not in operational}, 'ANSIBLE_FORCE_COLOR':'0'}


def evidence_context(meta):
    from greatminds.cli.stand_executor import _build_extra_vars
    variables = _build_extra_vars(meta)
    if variables.keys() - _CONTEXT_FIELDS:
        raise GreatMindsError('unsupported managed deployment context fields', exit_code=4)
    return variables


def environment_revision(runtime):
    path = runtime.parent / 'coordination/execution.yaml'
    if not path.exists():
        return '1'
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get('stand', {}), dict):
        raise GreatMindsError('invalid stand execution configuration', exit_code=4)
    value = document.get('stand', {}).get('environment_revision', '1')
    if not isinstance(value, str) or not value.strip():
        raise GreatMindsError('invalid stand environment revision', exit_code=4)
    return value


def require_fresh_deployment(runtime, lease_id, *, task_id=None):
    """Validate a managed receipt at consumption; never fall back to prose."""
    from greatminds.domain.stand_deployments import DeploymentLedger
    from greatminds.cli.stand_executor import read_project_env
    ledger = DeploymentLedger(runtime)
    if not ledger.path.exists() and not (runtime.parent/'coordination/execution.yaml').exists():
        return None  # Retained until the explicit legacy configuration migration.
    matches = [a for a in ledger.snapshot()['attempts'].values() if a['lease'].get('lease_id') == lease_id]
    if not matches or not any(type(a.get('sequence')) is int and a['sequence'] > 0 for a in matches):
        raise GreatMindsError('stand evidence requires a current managed deployment receipt', exit_code=4)
    ranked = [a for a in matches if type(a.get('sequence')) is int and a['sequence'] > 0]
    if len({a['sequence'] for a in ranked}) != len(ranked):
        raise GreatMindsError('ambiguous stand deployment sequence', exit_code=4)
    attempt = max(ranked, key=lambda a:a['sequence'])
    if (attempt['status'] != 'applied' or attempt.get('exit_code') != 0
            or attempt.get('inputs_match') is not True
            or not isinstance(attempt.get('inputs_after'), dict)
            or attempt.get('inputs_before') != attempt.get('inputs_after')
            or not isinstance(attempt.get('inputs_context'), dict)):
        raise GreatMindsError('stand deployment is unresolved, failed, or lacks stable input evidence', exit_code=4)
    if task_id is not None and attempt['lease'].get('task') != task_id:
        raise GreatMindsError('stand deployment belongs to another task', exit_code=4)
    recorded = attempt['inputs_after']
    context = attempt['inputs_context']
    if (context.keys() - _CONTEXT_FIELDS or context.get('lease_id') != lease_id
            or context.get('worktree') != recorded.get('workspace')):
        raise GreatMindsError('invalid stand evidence context', exit_code=4)
    outputs = attempt.get('output', {})
    if not isinstance(outputs, dict) or set(outputs) != {'stdout', 'stderr'}:
        raise GreatMindsError('stand evidence lacks captured command output', exit_code=4)
    for name, metadata in outputs.items():
        if not isinstance(metadata, dict) or not isinstance(metadata.get('path'), str):
            raise GreatMindsError('invalid stand evidence output metadata', exit_code=4)
        expected = ledger.path.parent / 'deployment-output' / safe_name(attempt['id']) / name
        path = Path(metadata.get('path', ''))
        if path != expected or path.resolve() != expected or not path.is_file():
            raise GreatMindsError('stand evidence output path changed', exit_code=4)
        captured = metadata.get('captured_bytes')
        if (type(captured) is not int or not 0 <= captured <= 67108864
                or metadata.get('truncated') is not False or metadata.get('bytes') != captured):
            raise GreatMindsError('stand evidence output is incomplete', exit_code=4)
        digest, count = hashlib.sha256(), 0
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != captured:
                raise GreatMindsError('stand evidence output size/type changed', exit_code=4)
            for chunk in iter(lambda: stream.read(1048576), b''):
                count += len(chunk)
                if count > captured:
                    raise GreatMindsError('stand evidence output grew during validation', exit_code=4)
                digest.update(chunk)
        if (count != captured or digest.hexdigest() != metadata.get('captured_sha256')
                or digest.hexdigest() != metadata.get('sha256')):
            raise GreatMindsError('stand evidence output integrity mismatch', exit_code=4)
    current = deployment_inputs(runtime, recorded['workspace'], recorded['profile'],
        [recorded['environment']['executable']], deployment_environment(),
        {**read_project_env(runtime), **attempt['inputs_context']})
    if current != recorded:
        raise GreatMindsError('stand deployment evidence is stale; deploy and validate current inputs', exit_code=4)
    return attempt
