import json
import sys

import yaml
from click.testing import CliRunner

from greatminds.cli import daemon as dm
from greatminds.cli.main import cli


def test_doctor_is_static_private_and_requires_no_coord_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, 'REGISTRY_PATH', tmp_path/'registry.json')
    monkeypatch.setattr(dm, 'AGENT_ENV_DIR', tmp_path/'captured')
    monkeypatch.setenv('DOCTOR_SECRET', 'never-print-this')
    def forbidden(*args, **kwargs):
        raise AssertionError('doctor must not launch a process')
    monkeypatch.setattr(dm.subprocess, 'run', forbidden)
    coord = tmp_path/'coordination'
    coord.mkdir()
    document = {'version': 1, 'agents': {'fixture': {
        'transport': 'acp', 'argv': [sys.executable, 'private-argument'],
        'adapter_version': 'fixture', 'harness_version': 'fixture',
        'required_env': ['DOCTOR_SECRET']}}, 'bindings': {}}
    path = coord/'execution.yaml'
    path.write_text(yaml.safe_dump(document))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = CliRunner().invoke(cli, ['daemon', 'doctor', '--project-dir', str(tmp_path), '--json'])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report['verification'] == 'static'
    assert report['agents'][0]['ready'] is True
    assert 'never-print-this' not in result.output
    assert 'private-argument' not in result.output
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.delenv('DOCTOR_SECRET')
    result = CliRunner().invoke(cli, ['daemon', 'doctor', '--project-dir', str(tmp_path), '--json'])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)['agents'][0]['missing_required_env'] == ['DOCTOR_SECRET']
    document['agents']['fixture']['argv'] = ['greatminds-nonexistent-doctor-fixture']
    document['agents']['fixture']['required_env'] = []
    path.write_text(yaml.safe_dump(document))
    result = CliRunner().invoke(cli, ['daemon', 'doctor', '--project-dir', str(tmp_path), '--json'])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)['agents'][0]['executable_available'] is False
