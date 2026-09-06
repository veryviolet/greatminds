"""ACP contracts must prevent native process launch before any side effects."""
import importlib

import pytest
from click.testing import CliRunner

from greatminds.core.errors import GreatMindsError


@pytest.mark.parametrize("contract_kind", ["invalid", "directory", "broken_symlink"])
@pytest.mark.parametrize("entry", ["start", "dry_run", "pty", "direct_pty"])
def test_acp_project_refuses_native_entrypoints(tmp_path, monkeypatch, contract_kind, entry):
    project = tmp_path / "project"
    config = project / "coordination"
    config.mkdir(parents=True)
    contract = config / "execution.yaml"
    if contract_kind == "invalid":
        contract.write_text("[invalid yaml")
    elif contract_kind == "directory":
        contract.mkdir()
    else:
        contract.symlink_to(config / "missing.yaml")
    # Resolve the canonical project even when invoked from a task worktree.
    cwd = project / ".worktrees" / "task" / "src"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("GREATMINDS_PROJECT_DIR", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "operator-before-launch")
    start = importlib.import_module("greatminds.cli.start_agent")
    pty = importlib.import_module("greatminds.cli.pty_launch")

    def forbidden(*args, **kwargs):
        pytest.fail("native launch performed a side effect before ACP refusal")

    monkeypatch.setattr(start, "load_env_file", forbidden)
    monkeypatch.setattr(pty.pty, "fork", forbidden)
    before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
    if entry == "direct_pty":
        with pytest.raises(GreatMindsError, match="Native agent launch is disabled"):
            pty._pty_launch_impl("DEVELOPER", "claude", [])
    else:
        command = pty.pty_launch if entry == "pty" else start.start_agent
        args = ["DEVELOPER", "claude"]
        if entry == "dry_run":
            args.append("--dry-run")
        result = CliRunner().invoke(command, args)
        assert result.exit_code == 2, result.output
        assert "Native agent launch is disabled" in result.output
    assert sorted(str(p.relative_to(project)) for p in project.rglob("*")) == before
    import os
    assert os.environ["GREATMINDS_ROLE"] == "operator-before-launch"


def test_explicit_project_refuses_launch_from_elsewhere(tmp_path, monkeypatch):
    from greatminds.core.paths import require_native_execution

    config = tmp_path / "project" / "coordination"
    config.mkdir(parents=True)
    (config / "execution.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(config.parent))
    with pytest.raises(GreatMindsError, match="Native agent launch is disabled"):
        require_native_execution()
