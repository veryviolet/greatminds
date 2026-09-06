"""Migration exclusion must cover native CLI admission and parent lifetimes."""
import importlib
import os
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.migration_safety import execution_barrier


@pytest.fixture
def project(tmp_path, monkeypatch):
    config = tmp_path / "coordination"
    config.mkdir()
    (config / "coord.yaml").write_text("windows: []\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("entry", ["start", "pty", "direct_pty", "coordd", "launch"])
def test_exclusive_migration_blocks_native_launch_before_effects(project, monkeypatch, entry):
    start = importlib.import_module("greatminds.cli.start_agent")
    pty = importlib.import_module("greatminds.cli.pty_launch")
    coordd = importlib.import_module("greatminds.cli.coordd")
    launch = importlib.import_module("greatminds.cli.launch")

    def forbidden(*args, **kwargs):
        pytest.fail("migration exclusion allowed a native launch side effect")

    monkeypatch.setattr(start, "load_env_file", forbidden)
    monkeypatch.setattr(pty.pty, "fork", forbidden)
    monkeypatch.setattr(coordd, "_run_native_daemon", forbidden)
    monkeypatch.setattr(launch.gm_env, "detect", forbidden)
    before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
    with execution_barrier(project, exclusive=True):
        if entry == "direct_pty":
            with pytest.raises(GreatMindsError, match="execution/migration"):
                pty._pty_launch_impl("DEVELOPER", "claude", [])
        else:
            command, args = {
                "start": (start.start_agent, ["DEVELOPER", "claude"]),
                "pty": (pty.pty_launch, ["DEVELOPER", "claude"]),
                "coordd": (coordd.coordd, ["--project-dir", str(project)]),
                "launch": (launch.launch, ["--target", "tmux", "--project-dir", str(project)]),
            }[entry]
            result = CliRunner().invoke(command, args)
            assert result.exit_code == 4, (result.output, result.exception)
            assert "execution/migration" in result.output
    assert sorted(str(p.relative_to(project)) for p in project.rglob("*")) == before


@pytest.mark.parametrize("entry", ["pty", "coordd", "launch", "start"])
def test_native_scope_holds_until_return_and_releases_after_error(project, monkeypatch, entry):
    module_name, body_name, command_name, args = {
        "pty": ("pty_launch", "_run_native_pty", "pty_launch", ["DEVELOPER", "claude"]),
        "coordd": ("coordd", "_run_native_daemon", "coordd", []),
        "launch": ("launch", "_launch_native_frontend", "launch", ["--target", "tmux"]),
        "start": ("start_agent", "_start_native_agent", "start_agent", ["DEVELOPER", "claude"]),
    }[entry]
    module = importlib.import_module("greatminds.cli." + module_name)
    reached = []

    def running(*args, **kwargs):
        with pytest.raises(GreatMindsError, match="execution/migration"):
            with execution_barrier(project, exclusive=True):
                pytest.fail("migration entered while native parent was running")
        reached.append(True)
        raise RuntimeError("synthetic native failure")

    monkeypatch.setattr(module, body_name, running)
    result = CliRunner().invoke(getattr(module, command_name), args)
    assert reached == [True], result.output
    assert isinstance(result.exception, RuntimeError)
    with execution_barrier(project, exclusive=True):
        pass


def test_coordd_resolves_acp_project_before_selecting_backend(project, monkeypatch):
    coordd = importlib.import_module("greatminds.cli.coordd")
    daemon = importlib.import_module("greatminds.runtime.daemon")
    (project / "coordination/execution.yaml").write_text("{}")
    cwd = project / ".worktrees/task/src"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("GREATMINDS_PROJECT_DIR", raising=False)
    calls = []

    async def serve(root, **options):
        calls.append((root, options["once"]))

    def forbidden(*args, **kwargs):
        pytest.fail("ACP project reached the native daemon")

    monkeypatch.setattr(daemon, "serve", serve)
    monkeypatch.setattr(coordd, "_run_native_daemon", forbidden)
    result = CliRunner().invoke(coordd.coordd, ["--once"])
    assert result.exit_code == 0, result.output
    assert calls == [(project, True)]


def test_explicit_project_keeps_native_barrier_and_environment_aligned(project, tmp_path, monkeypatch):
    coordd = importlib.import_module("greatminds.cli.coordd")
    other = tmp_path / "other"
    (other / "coordination").mkdir(parents=True)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(other))
    reached = []

    def running(root, *args):
        assert root == project
        assert os.environ["GREATMINDS_PROJECT_DIR"] == str(project)
        with pytest.raises(GreatMindsError):
            with execution_barrier(project, exclusive=True):
                pytest.fail("selected project's barrier was not held")
        with execution_barrier(other, exclusive=True):
            pass
        reached.append(True)

    monkeypatch.setattr(coordd, "_run_native_daemon", running)
    result = CliRunner().invoke(coordd.coordd, ["--project-dir", str(project)])
    assert result.exit_code == 0, result.output
    assert reached == [True]
    assert os.environ["GREATMINDS_PROJECT_DIR"] == str(other)


@pytest.mark.parametrize("kind", ["directory", "broken_symlink"])
def test_bad_acp_contract_does_not_fall_back_to_native(project, monkeypatch, kind):
    coordd = importlib.import_module("greatminds.cli.coordd")
    contract = project / "coordination/execution.yaml"
    if kind == "directory":
        contract.mkdir()
    else:
        contract.symlink_to(project / "missing.yaml")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid ACP contract selected the native daemon")

    monkeypatch.setattr(coordd, "_run_native_daemon", forbidden)
    result = CliRunner().invoke(coordd.coordd, ["--once"])
    assert result.exit_code == 2, result.output
    assert "cannot load execution config" in result.output


def test_real_pty_parent_excludes_migration_until_child_cleanup(project):
    ready = project / "child-ready"
    child_code = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "greatminds.cli.pty_launch", "DEVELOPER",
         sys.executable, "-c", child_code, str(ready)],
        cwd=project, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), "synthetic PTY child did not start"
        child_pid = int(ready.read_text())
        assert process.poll() is None
        with pytest.raises(GreatMindsError, match="execution/migration"):
            with execution_barrier(project, exclusive=True):
                pytest.fail("migration entered while PTY child was alive")
        process.stdin.close()
        assert process.wait(timeout=5) == 0, process.stderr.read().decode()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert not (project / "coordination/.agent_registry/developer.json").exists()
        with execution_barrier(project, exclusive=True):
            pass
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stderr.close()
