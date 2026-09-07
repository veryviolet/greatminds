"""Daemon and stand inputs interpret the same literal environment contract."""
import json
from pathlib import Path
import subprocess

import pytest

from greatminds.cli.daemon import _parse_env_file
from greatminds.cli import stand_executor as se
from greatminds.core.errors import GreatMindsError
from greatminds.core.service_environment import encode_environment
from test_stand_executor_0279 import _yaml_spec, _lease


def test_literal_multiline_values_reach_ansible_unchanged(tmp_path, monkeypatch):
    values = {'MULTILINE': 'one\ntwo\n', 'QUOTED': ' "double" and \'single\' ',
              'LITERAL': '$HOME `command` %h # untouched', 'BACKSLASH': 'a\\b\\'}
    path = tmp_path / 'PROJECT.env'
    path.write_text(encode_environment(values))
    assert _parse_env_file(path) == se.read_project_env(tmp_path) == values
    seen = []
    def run(argv, **kwargs):
        file = Path(argv[argv.index('--extra-vars') + 1][1:])
        extra = json.loads(file.read_text())
        assert {key: extra[key] for key in values} == values
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'ok\n', '')
    monkeypatch.setattr(se.subprocess, 'run', run)
    monkeypatch.setattr(se, 'is_deploy_safe', lambda *a, **k: (True, ''))
    rc, _ = se.execute_yaml_profile(_yaml_spec(tmp_path), _lease(coord=str(tmp_path)),
                                    ansible_playbook='/synthetic/ansible')
    assert rc == 0 and len(seen) == 1


@pytest.mark.parametrize('contents', [b'KEY="synthetic-secret', b'KEY=x\x00y', b'KEY=\xff'])
def test_invalid_file_is_rejected_without_secret_output_or_execution(tmp_path, monkeypatch, contents):
    (tmp_path / 'PROJECT.env').write_bytes(contents)
    for read in (lambda: _parse_env_file(tmp_path / 'PROJECT.env'), lambda: se.read_project_env(tmp_path)):
        with pytest.raises(GreatMindsError) as exc:
            read()
        assert 'synthetic-secret' not in str(exc.value)
    monkeypatch.setattr(se.subprocess, 'run', lambda *a, **k: pytest.fail('invalid environment launched a command'))
    monkeypatch.setattr(se, 'is_deploy_safe', lambda *a, **k: (True, ''))
    with pytest.raises(GreatMindsError):
        se.execute_yaml_profile(_yaml_spec(tmp_path), _lease(coord=str(tmp_path)),
                                ansible_playbook='/synthetic/ansible')


def test_missing_is_optional_but_broken_link_and_directory_are_errors(tmp_path):
    path = tmp_path / 'PROJECT.env'
    assert se.read_project_env(tmp_path) == _parse_env_file(path) == {}
    path.symlink_to(tmp_path / 'missing-target')
    for read in (lambda: se.read_project_env(tmp_path), lambda: _parse_env_file(path)):
        with pytest.raises(GreatMindsError, match='cannot read environment file'):
            read()
    path.unlink()
    path.mkdir()
    with pytest.raises(GreatMindsError, match='cannot read environment file'):
        se.read_project_env(tmp_path)
