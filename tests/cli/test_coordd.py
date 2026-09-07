"""Registered project selection uses the common ACP daemon."""
from click.testing import CliRunner
from greatminds.cli import coordd, daemon
from greatminds.runtime.bootstrap import bootstrap


def test_registered_project_runs_acp(tmp_path, monkeypatch):
    root = tmp_path/'project'
    bootstrap(root)
    daemon.register_project('example', root)
    result = CliRunner().invoke(coordd.coordd, ['--project', 'example', '--once'])
    assert result.exit_code == 0, result.output
    assert (root/'.greatminds/.runtime').is_dir()
    assert not (root/'.greatminds/.agent_registry').exists()


def test_unknown_project_does_not_start(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, 'lookup_project_dir', lambda name: None)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(coordd.coordd, ['--project', 'missing', '--once'])
    assert result.exit_code != 0 and 'no project registered' in result.output
    assert list(tmp_path.iterdir()) == []
