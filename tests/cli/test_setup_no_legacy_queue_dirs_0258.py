"""Public ACP setup creates schema queues and preserves optional project inputs."""
from click.testing import CliRunner

from greatminds.cli.setup import setup
from greatminds.core.schema import load_schema_snapshot


def test_setup_creates_effective_queues_and_no_native_artifacts(tmp_path):
    expected = {name for name in load_schema_snapshot().document['queues'] if not name.startswith('.')}
    result = CliRunner().invoke(setup, ['--project-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    runtime = tmp_path / '.greatminds'
    assert {p.name for p in runtime.iterdir() if p.is_dir()} == expected
    for name in ('stand_requests', 'stand_wip', 'stand_done', 'bootstrap.md', '.agent_registry'):
        assert not (runtime / name).exists()
    assert not (tmp_path / 'coordination/stand-profiles').exists()


def test_repeated_setup_preserves_operator_environment_and_project_context(tmp_path):
    config = tmp_path / 'coordination'
    config.mkdir()
    runtime = tmp_path / '.greatminds'
    runtime.mkdir()
    files = {config / 'PROJECT.md': '# User project\nLanguage: ru\n',
             runtime / 'PROJECT.env': 'TOKEN="synthetic-value"\n'}
    for path, value in files.items():
        path.write_text(value)
    for _ in range(2):
        result = CliRunner().invoke(setup, ['--project-dir', str(tmp_path)])
        assert result.exit_code == 0, result.output
    for path, value in files.items():
        assert path.read_text() == value
        assert not path.with_suffix(path.suffix + '.bak').exists()
