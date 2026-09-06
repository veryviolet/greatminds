import json

import pytest
import yaml
from click.testing import CliRunner
from greatminds.cli.main import cli
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.execution_migration import plan_execution_migration


def project(root):
    coord=root/'coordination';coord.mkdir()
    old=coord/'coord.yaml'
    old.write_text(yaml.safe_dump({'session':'old','windows':[
        {'name':'dev','role':'DEVELOPER','tool':'claude','mode':'driven','model':'old-model'},
        {'name':'planner','role':'ARCHITECT-PLANNER','tool':'codex','mode':'chat'},
        {'name':'dashboard','role':'','mode':'dashboard'}]}))
    proposed=root/'proposed.yaml'
    proposed.write_text(yaml.safe_dump({'version':1,'agents':{'target':{'transport':'acp',
        'argv':['adapter','--secret-argument','private-value'],'adapter_version':'1','harness_version':'2',
        'environment':{'API_KEY':'MY_PRIVATE_ENV'}}},'bindings':{'dev':{'role':'DEVELOPER','agent':'target',
        'model':'new-model','permission':'deny','scheduling':'queue'}}}))
    return old,proposed


def test_review_exposes_missing_roles_and_policy_changes_without_writing(tmp_path):
    old,proposed=project(tmp_path)
    before={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=CliRunner().invoke(cli,['migrate','--project-dir',str(tmp_path),'--execution-config',str(proposed)])
    assert result.exit_code==0,result.output
    review=json.loads(result.output)
    assert review['missing_roles']==['ARCHITECT-PLANNER'] and not review['role_coverage_complete']
    assert review['target_bindings'][0]['model']=='new-model'
    assert review['source_roles'][0]['model']=='old-model'
    assert review['observer_windows'][0]['name']=='dashboard'
    assert not review['applied']
    assert 'private-value' not in result.output and 'MY_PRIVATE_ENV' not in result.output
    assert {str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}==before
    assert not (tmp_path/'.greatminds').exists()


def test_explicit_retirement_is_visible_and_never_inferred(tmp_path):
    _,proposed=project(tmp_path)
    review=plan_execution_migration(tmp_path,proposed,retire_roles=['ARCHITECT-PLANNER'])
    assert review['role_coverage_complete'] and review['retired_roles']==['ARCHITECT-PLANNER']
    with pytest.raises(GreatMindsError,match='source configuration'):
        plan_execution_migration(tmp_path,proposed,retire_roles=['UNKNOWN'])
    with pytest.raises(GreatMindsError,match='target binding'):
        plan_execution_migration(tmp_path,proposed,retire_roles=['DEVELOPER'])


def test_review_hash_changes_with_source_target_and_installed_contract(tmp_path):
    old,proposed=project(tmp_path)
    first=plan_execution_migration(tmp_path,proposed)
    assert plan_execution_migration(tmp_path,proposed)==first
    document=yaml.safe_load(old.read_text());document['windows'][0]['model']='changed'
    old.write_text(yaml.safe_dump(document))
    second=plan_execution_migration(tmp_path,proposed)
    assert second['review_sha256']!=first['review_sha256']
    proposed.write_text(proposed.read_text()+'# reviewed source bytes changed\n')
    third=plan_execution_migration(tmp_path,proposed)
    assert third['review_sha256']!=second['review_sha256']
    (tmp_path/'coordination/execution.yaml').write_text('existing contract')
    assert plan_execution_migration(tmp_path,proposed)['review_sha256']!=third['review_sha256']


def test_malformed_source_cannot_appear_as_complete_role_coverage(tmp_path):
    old,proposed=project(tmp_path)
    old.write_text('windows: invalid')
    with pytest.raises(GreatMindsError):
        plan_execution_migration(tmp_path,proposed)


def test_ordinary_migration_does_not_recreate_native_fleet_in_acp_project(tmp_path,monkeypatch):
    from greatminds.cli.migrate import run_migration
    _,proposed=project(tmp_path)
    (tmp_path/'coordination/execution.yaml').write_bytes(proposed.read_bytes())
    before={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    def forbidden(*args,**kwargs):
        raise AssertionError('native setup must not run')
    monkeypatch.setattr('greatminds.cli.migrate.subprocess.run',forbidden)
    run_migration(tmp_path)
    assert {str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}==before
