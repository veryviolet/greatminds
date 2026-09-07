"""Foreground and registered daemon startup share environment precedence."""
import os
import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from greatminds.cli import daemon
from greatminds.cli.coordd import coordd
from greatminds.core.service_environment import encode_environment
from greatminds.runtime.bootstrap import bootstrap


def project(root):
    root.mkdir()
    bootstrap(root)
    (root / '.greatminds/PROJECT.env').write_text(encode_environment({'VALUE': 'project', 'MULTI': 'a\nb'}))
    return root


@pytest.mark.parametrize('registered,explicit', [(False, False), (True, False), (True, True)])
def test_precedence_and_subprocess_environment_are_identical(tmp_path, monkeypatch, registered, explicit):
    root = project(tmp_path / 'project with spaces')
    monkeypatch.setenv('VALUE', 'shell')
    if registered:
        daemon.register_project('fixture', root)
        daemon.AGENT_ENV_DIR.mkdir(parents=True)
        daemon._agent_env_file('fixture').write_text(encode_environment({'VALUE': 'captured'}))
    expected = 'captured' if registered else 'project'
    seen = []
    async def serve(path, **kwargs):
        assert path == root
        assert kwargs['environment']['VALUE'] == os.environ['VALUE'] == expected
        assert kwargs['environment']['MULTI'] == os.environ['MULTI'] == 'a\nb'
        child = subprocess.run([sys.executable, '-c',
            'import os,json; print(json.dumps([os.environ["VALUE"],os.environ["MULTI"]]))'],
            capture_output=True, text=True, check=True, timeout=10)
        assert json.loads(child.stdout) == [expected, 'a\nb']
        seen.append(True)
    monkeypatch.setattr('greatminds.runtime.daemon.serve', serve)
    args = ['--project-dir', str(root), '--once']
    if explicit:
        args += ['--project', 'fixture']
    before = dict(os.environ)
    result = CliRunner().invoke(coordd, args)
    assert result.exit_code == 0, result.output
    assert seen and dict(os.environ) == before


def test_failure_restores_environment(tmp_path, monkeypatch):
    root = project(tmp_path / 'project')
    before = dict(os.environ)
    async def serve(*args, **kwargs):
        raise RuntimeError('injected')
    monkeypatch.setattr('greatminds.runtime.daemon.serve', serve)
    result = CliRunner().invoke(coordd, ['--project-dir', str(root), '--once'])
    assert result.exit_code != 0
    assert dict(os.environ) == before


@pytest.mark.parametrize('fault', ['mismatch', 'ambiguous', 'malformed', 'unknown'])
def test_bad_identity_or_environment_never_starts_daemon(tmp_path, monkeypatch, fault):
    root = project(tmp_path / 'project')
    args = ['--project-dir', str(root)]
    if fault == 'mismatch':
        daemon.register_project('fixture', tmp_path / 'other')
        args += ['--project', 'fixture']
    elif fault == 'ambiguous':
        daemon.register_project('one', root)
        daemon.register_project('two', root)
    elif fault == 'unknown':
        args += ['--project', 'absent']
    else:
        (root / '.greatminds/PROJECT.env').write_text('VALUE="synthetic-secret')
    async def serve(*args, **kwargs):
        pytest.fail('invalid startup reached daemon')
    monkeypatch.setattr('greatminds.runtime.daemon.serve', serve)
    result = CliRunner().invoke(coordd, args)
    assert result.exit_code != 0
    assert 'synthetic-secret' not in result.output
