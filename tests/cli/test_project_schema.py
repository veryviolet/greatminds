import json

import pytest
from click.testing import CliRunner

from greatminds.cli import coordd, daemon, task
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
    assert report["schema"]["roles"] == coordd.load_schema_roles(find_canon_dir())
    lifecycle = daemon._schema_lifecycles(tmp_path)["ARCHITECT-REVIEWER"]
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


@pytest.mark.parametrize("mode", ["chat", "loop", "staged", "", "driven"])
def test_service_install_uses_dispatch_mode_and_ignores_legacy_schema(tmp_path, mode):
    (tmp_path / "coord.yaml").write_text(json.dumps({"windows": [{
        "role": "ARCHITECT-REVIEWER", "tool": "codex", "mode": mode,
    }]}))
    (tmp_path / "schema.yaml").write_text(
        "roles: {ARCHITECT-REVIEWER: {lifecycle: interactive}}\n")
    assert daemon.has_driven_codex_roles(tmp_path) is (mode == "driven")
