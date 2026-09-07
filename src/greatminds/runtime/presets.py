"""Explicit role rosters over the same schema, ACP manifests and evidence gates."""
import copy
from pathlib import Path

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.core.storage import atomic_bytes, file_lock
from .config import parse_execution_config


COMMON = {'planner': ('ARCHITECT-PLANNER', 'on-demand'),
          'reviewer': ('ARCHITECT-REVIEWER', 'queue')}
CODE = {'developer': ('DEVELOPER', 'queue'), 'tester': ('TESTER', 'queue')}
UI = {'ui-developer': ('UI-DEVELOPER', 'queue'), 'tester': ('TESTER', 'queue')}
DOCS = {'writer': ('TECHNICAL-WRITER', 'queue'), 'reader': ('READER', 'queue')}
PRESETS = {
    'local': {**COMMON, **CODE},
    'ui': {**COMMON, **UI},
    'docs': {**COMMON, **DOCS},
    'deployed': {**COMMON, **CODE, **UI, 'live-developer': ('LIVE-DEVELOPER', 'on-demand'),
                 'explorer': ('EXPLORER', 'queue')},
    'full': {**COMMON, **CODE, **UI, **DOCS,
             'live-developer': ('LIVE-DEVELOPER', 'on-demand'),
             'explorer': ('EXPLORER', 'queue'), 'maintainer': ('MAINTAINER', 'on-demand')},
}
DESCRIPTIONS = {
    'local': 'Local code implementation, configured daemon validation and independent review.',
    'ui': 'UI implementation, validation and review; stand requirements follow each task plan.',
    'docs': 'Documentation implementation, independent reader review and final review.',
    'deployed': 'Code/UI and deployed validation roles; configure stand policy separately.',
    'full': 'Full code/UI/docs review roster with optional interactive and deployed validation roles.',
}


def catalog():
    return [{'id': name, 'description': DESCRIPTIONS[name],
             'bindings': [{'id': key, 'role': role, 'scheduling': scheduling}
                          for key, (role, scheduling) in bindings.items()]}
            for name, bindings in PRESETS.items()]


def configure_preset(project, name, agent, *, apply=False):
    project = Path(project).resolve()
    if name not in PRESETS:
        raise GreatMindsError('unknown execution preset', exit_code=2)
    path = project/'coordination/execution.yaml'
    schema = load_schema_snapshot()

    def prepare():
        try:
            document = yaml.safe_load(path.read_bytes())
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise GreatMindsError('cannot read execution config for preset', exit_code=2) from exc
        existing = parse_execution_config(document, roles=set(schema.document['roles']))
        if agent not in {item.id for item in existing.agents}:
            raise GreatMindsError('preset requires an existing named ACP agent manifest', exit_code=2)
        candidate = copy.deepcopy(document)
        candidate['bindings'] = {key: {'role': role, 'scheduling': scheduling, 'agent': agent, 'permission': 'ask'}
                                 for key, (role, scheduling) in PRESETS[name].items()}
        configured = parse_execution_config(candidate, roles=set(schema.document['roles']))
        changed = configured.sha256 != existing.sha256
        if existing.bindings and changed:
            raise GreatMindsError('preset does not replace existing role bindings; edit the execution contract explicitly', exit_code=2)
        return candidate, configured, changed

    if apply:
        if not (project/'.greatminds').is_dir():
            raise GreatMindsError('initialize the project with greatminds setup before applying a preset', exit_code=2)
        with file_lock(project/'.greatminds/setup.lock', label='execution preset'):
            document, config, changed = prepare()
            if changed:
                atomic_bytes(path, yaml.safe_dump(document, sort_keys=False).encode())
    else:
        document, config, changed = prepare()
    return {'version': 1, 'preset': name, 'applied': apply, 'changed': changed,
            'execution_sha256': config.sha256, 'schema_sha256': schema.sha256,
            'execution': document}
