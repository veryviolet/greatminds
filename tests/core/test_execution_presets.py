"""Preset application preserves explicit contracts and does not execute agents."""
import copy

import pytest
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.presets import PRESETS, catalog, configure_preset


@pytest.fixture
def project(tmp_path):
    (tmp_path / '.greatminds').mkdir()
    (tmp_path / 'coordination').mkdir()
    config = {'version': 1, 'agents': {'one': {'transport': 'acp',
              'argv': ['nonexistent-harness'], 'adapter_version': 'fixture',
              'harness_version': 'fixture'}}, 'bindings': {},
              'commands': {'check': {'argv': ['python', '-m', 'unittest'],
                                    'roles': ['TESTER']}}}
    (tmp_path / 'coordination/execution.yaml').write_text(yaml.safe_dump(config))
    return tmp_path


@pytest.mark.parametrize('name', PRESETS)
def test_preview_apply_repeat_preserve_manifest_commands_and_schema(project, name):
    path = project / 'coordination/execution.yaml'
    before = path.read_bytes()
    schema = copy.deepcopy(load_schema_snapshot().document)
    preview = configure_preset(project, name, 'one')
    assert path.read_bytes() == before
    assert not (project / '.greatminds/setup.lock').exists()
    assert preview['changed'] and not preview['applied']
    applied = configure_preset(project, name, 'one', apply=True)
    assert applied['execution'] == preview['execution']
    original = yaml.safe_load(before)
    assert applied['execution']['agents'] == original['agents']
    assert applied['execution']['commands'] == original['commands']
    bindings = applied['execution']['bindings']
    assert {b['agent'] for b in bindings.values()} == {'one'}
    assert {b['permission'] for b in bindings.values()} == {'ask'}
    assert bindings['planner']['scheduling'] == 'on-demand'
    assert bindings['reviewer']['role'] == 'ARCHITECT-REVIEWER'
    saved = path.read_bytes()
    assert not configure_preset(project, name, 'one', apply=True)['changed']
    assert path.read_bytes() == saved
    assert load_schema_snapshot().document == schema


def test_unknown_manifest_and_existing_roster_never_overwritten(project):
    path = project / 'coordination/execution.yaml'
    before = path.read_bytes()
    with pytest.raises(GreatMindsError, match='existing named ACP'):
        configure_preset(project, 'local', 'missing', apply=True)
    assert path.read_bytes() == before
    configure_preset(project, 'local', 'one', apply=True)
    saved = path.read_bytes()
    with pytest.raises(GreatMindsError, match='does not replace'):
        configure_preset(project, 'full', 'one', apply=True)
    assert path.read_bytes() == saved


def test_catalog_and_invalid_yaml(project):
    assert {p['id'] for p in catalog()} == set(PRESETS)
    path = project / 'coordination/execution.yaml'
    path.write_text('[bad: yaml')
    with pytest.raises(GreatMindsError, match='cannot read execution'):
        configure_preset(project, 'local', 'one', apply=True)
    assert path.read_text() == '[bad: yaml'
