"""Deterministic ACP project bootstrap, independent of per-harness setup."""
from pathlib import Path

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot, inspect_schema_copy
from greatminds.core.storage import atomic_bytes, file_lock, safe_name
from .config import parse_execution_config
import yaml


def bootstrap(project: Path, source: Path):
    try:
        raw = source.read_bytes()
        document = yaml.safe_load(raw)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise GreatMindsError('cannot read execution contract', exit_code=2) from exc
    schema = load_schema_snapshot()
    config = parse_execution_config(document, roles=set(schema.document['roles']))
    queues = [safe_name(name) for name in schema.document['queues'] if not name.startswith('.')]
    project = project.resolve()
    runtime, coordination = project/'.greatminds', project/'coordination'
    destination = coordination/'execution.yaml'

    def validate_target():
        if destination.exists():
            installed = parse_execution_config(yaml.safe_load(destination.read_bytes()),
                                               roles=set(schema.document['roles']))
            if installed.sha256 != config.sha256:
                raise GreatMindsError('execution contract differs; setup does not replace an existing contract', exit_code=2)
        elif ((coordination/'coord.yaml').exists() or (project/'coord.yaml').exists()
              or (runtime/'.runtime/state.json').exists() or (runtime/'.agent_registry').exists()
              or (coordination/'.runtime').exists()):
            raise GreatMindsError('existing fleet requires explicit execution migration before ACP setup', exit_code=2)

    validate_target()
    with file_lock(runtime/'setup.lock', label='ACP setup'):
        validate_target()
        coordination.mkdir(parents=True, exist_ok=True)
        for name in queues:
            (runtime/name).mkdir(parents=True, exist_ok=True)
        mirror = runtime/'schema.yaml'
        if not mirror.exists():
            atomic_bytes(mirror, schema.text.encode())
        ignore = project/'.gitignore'
        text = ignore.read_text() if ignore.exists() else ''
        missing = [rule for rule in ('/.greatminds/', '/.worktrees/') if rule not in text.splitlines()]
        if missing:
            atomic_bytes(ignore, (text + ('\n' if text and not text.endswith('\n') else '')
                                 + '\n'.join(missing) + '\n').encode())
        # Publish the execution contract last; interrupted setup is retryable.
        if not destination.exists():
            atomic_bytes(destination, raw)
    return {'project': str(project), 'execution_sha256': config.sha256,
            'bindings': len(config.bindings), 'agents': len(config.agents),
            'schema_sha256': schema.sha256, 'schema_mirror': inspect_schema_copy(schema, project)['status']}
