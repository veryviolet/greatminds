import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from greatminds.cli.main import cli
from greatminds.runtime.bootstrap import bootstrap
from greatminds.core.errors import GreatMindsError


def manifest(root):
    path=root/'manifest.yaml'
    path.write_text(yaml.safe_dump({'version':1,'agents':{'fixture':{'transport':'acp','argv':['not-launched'],
        'adapter_version':'fixture','harness_version':'fixture'}},'bindings':{'dev':{'role':'DEVELOPER','agent':'fixture'}}}))
    return path


def test_bootstrap_is_idempotent_and_creates_only_shared_project_state(tmp_path):
    source=manifest(tmp_path);project=tmp_path/'project'
    result=CliRunner().invoke(cli,['setup','--project-dir',str(project),'--execution-config',str(source)])
    assert result.exit_code==0,result.output
    assert json.loads(result.output)['bindings']==1
    assert (project/'.greatminds/feature_dev').is_dir()
    assert (project/'.greatminds/schema.yaml').is_file()
    assert not (project/'coordination/coord.yaml').exists()
    assert not (project/'.greatminds/.runtime/state.json').exists()
    before={str(p.relative_to(project)):p.read_bytes() for p in project.rglob('*') if p.is_file()}
    bootstrap(project,source)
    after={str(p.relative_to(project)):p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert after==before
    assert not any('.claude' in p or '.codex' in p for p in after)


def test_invalid_manifest_does_not_create_project(tmp_path):
    source=tmp_path/'invalid.yaml';source.write_text('version: 99')
    with pytest.raises(GreatMindsError):
        bootstrap(tmp_path/'project',source)
    assert not (tmp_path/'project').exists()


def test_bootstrap_cannot_activate_existing_fleet_or_replace_contract(tmp_path):
    source=manifest(tmp_path);project=tmp_path/'project'
    (project/'coordination').mkdir(parents=True)
    legacy=project/'coordination/coord.yaml';legacy.write_text('windows: []')
    with pytest.raises(GreatMindsError,match='migration'):
        bootstrap(project,source)
    assert not (project/'.greatminds').exists()
    legacy.unlink();bootstrap(project,source)
    before=(project/'coordination/execution.yaml').read_bytes()
    document=yaml.safe_load(source.read_text());document['max_running']=2
    source.write_text(yaml.safe_dump(document))
    with pytest.raises(GreatMindsError,match='differs'):
        bootstrap(project,source)
    assert (project/'coordination/execution.yaml').read_bytes()==before


def test_custom_schema_and_ignore_entries_are_preserved(tmp_path):
    source=manifest(tmp_path);project=tmp_path/'project'
    (project/'coordination').mkdir(parents=True)
    (project/'.greatminds').mkdir()
    (project/'.greatminds/schema.yaml').write_text('custom mirror')
    (project/'.gitignore').write_text('keep-me')
    assert bootstrap(project,source)['schema_mirror']=='drifted'
    assert (project/'.greatminds/schema.yaml').read_text()=='custom mirror'
    assert (project/'.gitignore').read_text().startswith('keep-me\n')
