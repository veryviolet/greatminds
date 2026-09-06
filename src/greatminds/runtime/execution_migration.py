"""Read-only, revision-pinned review of an explicit ACP migration contract."""
from dataclasses import asdict
import hashlib
from pathlib import Path

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import coord_yaml_path, project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from .config import fingerprint, parse_execution_config


def _read(path):
    try:
        raw = path.read_bytes()
        document = yaml.safe_load(raw)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise GreatMindsError('cannot read migration configuration', exit_code=2) from exc
    if not isinstance(document, dict):
        raise GreatMindsError('migration configuration must be a mapping', exit_code=2)
    return raw, document


def plan_execution_migration(project: Path, proposed: Path, *, retire_roles=()):
    """Compare explicit source/target contracts without guessing transport or auth."""
    project = project.resolve()
    source = coord_yaml_path(project)
    old_raw, old = _read(source)
    new_raw, new = _read(proposed)
    schema = load_schema_snapshot()
    config = parse_execution_config(new, roles=set(schema.document['roles']))
    windows = old.get('windows')
    if not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
        raise GreatMindsError('source windows must be an array of mappings', exit_code=2)
    roles = []
    observers = []
    for index, window in enumerate(windows):
        if any(window.get(key) is not None and not isinstance(window[key], str)
               for key in ('name','role','tool','mode','model')):
            raise GreatMindsError('source window identity, tool, mode and model must be strings', exit_code=2)
        role = window.get('role')
        if role is not None and not isinstance(role, str):
            raise GreatMindsError('source role must be a string', exit_code=2)
        if not role:
            observers.append({'index': index, 'name': window.get('name'), 'mode': window.get('mode')})
            continue
        roles.append({'index': index, **{key: window.get(key) for key in ('name','role','tool','mode','model')}})
    retired = set(retire_roles)
    source_roles = {r['role'] for r in roles}
    if retired - source_roles:
        raise GreatMindsError('retirement names must identify roles in the source configuration', exit_code=2)
    target_roles = {b.role for b in config.bindings}
    if retired & target_roles:
        raise GreatMindsError('a retired role cannot also have a target binding', exit_code=2)
    missing = sorted(source_roles - target_roles - retired)
    bindings = [{**{key: getattr(b,key) for key in ('id','role','agent','model','mode','workspace',
                  'scheduling','permission','session','account','max_running','timeout_seconds')},
                 'adapter_version': config.agent(b.agent).adapter_version,
                 'harness_version': config.agent(b.agent).harness_version} for b in config.bindings]
    existing = project/'coordination/execution.yaml'
    current_digest = hashlib.sha256(existing.read_bytes()).hexdigest() if existing.exists() else None
    review = {'version':1, 'project':str(project), 'runtime':str(project_runtime_dir(project)),
              'source':str(source), 'source_sha256':hashlib.sha256(old_raw).hexdigest(),
              'proposed_sha256':hashlib.sha256(new_raw).hexdigest(), 'execution_sha256':config.sha256,
              'schema_sha256':schema.sha256, 'installed_execution_sha256':current_digest,
              'source_roles':roles, 'observer_windows':observers, 'target_bindings':bindings,
              'added_roles':sorted(target_roles-source_roles), 'retired_roles':sorted(retired),
              'repeated_source_roles':sorted(role for role in source_roles if sum(r['role']==role for r in roles)>1),
              'missing_roles':missing, 'stand':asdict(config.stand) if config.stand else None,
              'role_coverage_complete':not missing,
              'required_follow_up':['stop_and_verify_existing_execution','backup_source_artifacts',
                  'publish_execution_contract','retire_generated_launcher_artifacts','verify_runtime_and_frontends']}
    # The raw launch argv and environment data are intentionally absent from review output.
    return {**review, 'review_sha256':fingerprint(review), 'applied':False}
