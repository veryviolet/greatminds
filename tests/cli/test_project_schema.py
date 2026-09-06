import json

import pytest
from click.testing import CliRunner

from greatminds.cli import task
from greatminds.cli.main import cli
from greatminds.core.paths import find_canon_dir
from greatminds.core.schema import load_schema_snapshot


def test_agent_and_runtime_read_same_contract_despite_stale_mirror(tmp_path, monkeypatch):
    runtime = tmp_path / ".greatminds"
    runtime.mkdir()
    stale = "version: 0\nroles: {ARCHITECT-REVIEWER: {lifecycle: interactive}}\n"
    (runtime / "schema.yaml").write_text(stale)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(task, "_schema_cache", None)

    result = CliRunner().invoke(cli, ["project", "schema", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["project_copy"]["status"] == "drifted"
    assert report["schema"] == task.schema()
    from greatminds.runtime.observation import configuration
    (tmp_path / "coordination").mkdir()
    (tmp_path / "coordination/execution.yaml").write_text("version: 1\nagents: {}\nbindings: {}\n")
    runtime_schema, _ = configuration(tmp_path)
    assert report["schema"] == runtime_schema.document
    lifecycle = runtime_schema.document["roles"]["ARCHITECT-REVIEWER"]["lifecycle"]
    assert lifecycle == report["schema"]["roles"]["ARCHITECT-REVIEWER"]["lifecycle"]
    assert lifecycle == "driven"
    assert (runtime / "schema.yaml").read_text() == stale


@pytest.mark.parametrize("status", ["missing", "drifted", "current"])
def test_schema_check_has_machine_readable_exit_status(tmp_path, status):
    snapshot = load_schema_snapshot()
    if status != "missing":
        runtime = tmp_path / ".greatminds"
        runtime.mkdir()
        (runtime / "schema.yaml").write_text(
            snapshot.text if status == "current" else "version: 0\n")
    result = CliRunner().invoke(cli, [
        "project", "schema", "--project-dir", str(tmp_path), "--json", "--check",
    ])
    assert result.exit_code == (0 if status == "current" else 2), result.output
    report = json.loads(result.output)
    assert report["project_copy"]["status"] == status
    assert report["sha256"] == snapshot.sha256
    assert "schema" not in report


def test_print_schema_works_without_project_and_has_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["project", "schema"])
    assert result.exit_code == 0, result.output
    assert result.output == load_schema_snapshot().text
    assert list(tmp_path.iterdir()) == []
