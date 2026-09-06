import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.processes import process_identity
from greatminds.runtime.store import RunStore, TaskRevision
from greatminds.runtime.supervisor import Supervisor


SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "acp_server.py"


def setup(tmp_path, scenario="echo"):
    schema = load_schema_snapshot()
    config = parse_execution_config({
        "version": 1, "agents": {"fake": {"transport": "acp",
            "argv": [sys.executable, str(SERVER), scenario],
            "adapter_version": "fixture", "harness_version": "fixture"}},
        "bindings": {"developer": {"role": "DEVELOPER", "agent": "fake", "timeout_seconds": 1}},
    }, roles=set(schema.document["roles"]))
    runtime = tmp_path / ".greatminds"
    path = runtime / "feature_dev" / "0001-test.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("title: Test\n")
    store = RunStore(runtime)
    return store, schema, config, TaskRevision.capture(runtime, path)


def supervisor(tmp_path, store, schema, config):
    return Supervisor(project=tmp_path, store=store, schema=schema, config=config, environment={})


@pytest.mark.parametrize("repository", [True, False])
def test_required_workspace_precedes_agent_launch(tmp_path, repository):
    store, schema, config, task = setup(tmp_path)
    path = store.runtime / task.path
    path.write_text("id: 0001-test\nkind: feature\ntitle: Test\n")
    task = TaskRevision.capture(store.runtime, path)
    if repository:
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-m", "Initial"], cwd=tmp_path, check=True, capture_output=True)

    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="work")
            if repository:
                assert result["state"] == "completed", result
                workspace = tmp_path / ".worktrees" / "0001-test"
                assert result["workspace"] == str(workspace)
                assert result["workspace_identity"]["branch"] == "task/0001-test"
                assert (workspace / "agent-starts.log").exists()
                kinds = [event["kind"] for event in store.snapshot()["events"]]
                assert kinds.index("workspace_ready") < kinds.index("process_recorded")
            else:
                assert result["state"] == "failed"
                assert "process" not in result
            assert not (tmp_path / "agent-starts.log").exists()
    asyncio.run(check())


def test_cancelled_workspace_preparation_drains_worker(monkeypatch):
    import threading
    from greatminds.runtime import workspaces
    entered, released, finished = threading.Event(), threading.Event(), threading.Event()
    def prepare(*args):
        entered.set()
        assert released.wait(2)
        finished.set()
    monkeypatch.setattr(workspaces, "prepare_workspace", prepare)
    async def check():
        task = asyncio.create_task(workspaces.prepare_workspace_async())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    asyncio.run(check())


def test_existing_workspace_wrong_branch_never_launches_agent(tmp_path):
    store, schema, config, task = setup(tmp_path)
    path = store.runtime / task.path
    path.write_text("id: 0001-test\nkind: feature\ntitle: Test\n")
    task = TaskRevision.capture(store.runtime, path)
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "Initial"], cwd=tmp_path, check=True, capture_output=True)
    workspace = tmp_path / ".worktrees" / "0001-test"
    subprocess.run(["git", "worktree", "add", "-b", "unrelated", str(workspace)],
                   cwd=tmp_path, check=True, capture_output=True)
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="work")
            assert result["state"] == "failed"
            assert "process" not in result
            assert not (workspace / "agent-starts.log").exists()
    asyncio.run(check())


def test_supervised_acp_turn_has_durable_process_and_metrics(tmp_path):
    store, schema, config, task = setup(tmp_path)
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="do useful work")
            assert result["state"] == "completed"
            assert result["session_id"] == "test-session"
            assert result["outcome"]["updates"] == 1
            assert result["outcome"]["context_bytes"] == len("do useful work")
            assert process_identity(result["process"]["pid"]) is None
            kinds = [e["kind"] for e in store.snapshot()["events"]]
            assert kinds.index("process_recorded") < kinds.index("running")
            assert store.snapshot()["results"] == {}  # A turn is not a domain decision.
    asyncio.run(check())


def test_second_supervisor_cannot_dispatch_or_recover(tmp_path):
    store, schema, config, task = setup(tmp_path)
    async def check():
        async with supervisor(tmp_path, store, schema, config) as first:
            claim = first.claim(task, config.bindings[0])
            with pytest.raises(GreatMindsError, match="ACP supervisor"):
                async with supervisor(tmp_path, store, schema, config):
                    pytest.fail("second supervisor acquired ownership")
            assert store.snapshot()["runs"][claim.run["id"]]["state"] == "claimed"
    asyncio.run(check())


def test_permission_requires_action_without_repeating_or_completing_task(tmp_path):
    store, schema, config, task = setup(tmp_path, "permission")
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="read")
            assert result["state"] == "waiting_input"
            assert result["reason"] == "permission_required"
        async with supervisor(tmp_path, store, schema, config):
            assert store.snapshot()["runs"][claim.run["id"]]["state"] == "waiting_input"
    asyncio.run(check())


def test_missing_auth_waits_before_process_launch(tmp_path):
    store, schema, config, task = setup(tmp_path)
    config = replace(config, agents=(replace(config.agents[0], required_env=("UNSET_TEST_TOKEN",)),))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="work")
            assert result["state"] == "waiting_auth"
            assert "process" not in result
    asyncio.run(check())


def test_required_capability_fails_before_prompt(tmp_path):
    store, schema, config, task = setup(tmp_path)
    config = replace(config, agents=(replace(config.agents[0], required_capabilities=("loadSession",)),))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="work")
            assert result["state"] == "failed"
            assert result["reason"] == "configuration_error"
            assert not any(e["kind"] == "running" for e in store.snapshot()["events"])
    asyncio.run(check())


@pytest.mark.parametrize("scenario,expected", [("model", "completed"), ("model-ignore", "failed")])
def test_model_choice_requires_confirmation_from_agent(tmp_path, scenario, expected):
    store, schema, config, task = setup(tmp_path, scenario)
    config = replace(config, bindings=(replace(config.bindings[0], model="chosen"),))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="work")
            assert result["state"] == expected, result
            if expected == "failed":
                assert result["reason"] == "configuration_error"
                assert result["outcome"]["updates"] == 0
    asyncio.run(check())


@pytest.mark.parametrize("scenario,expected", [("permission-local", "completed"),
                                               ("permission-outside", "waiting_input"),
                                               ("permission-execute", "waiting_input")])
def test_workspace_policy_requires_scoped_file_locations(tmp_path, scenario, expected):
    store, schema, config, task = setup(tmp_path, scenario)
    config = replace(config, bindings=(replace(config.bindings[0], permission="allow-workspace"),))
    async def check():
        async with supervisor(tmp_path, store, schema, config) as service:
            claim = service.claim(task, config.bindings[0])
            result = await service.execute(claim, binding=config.bindings[0], prompt="work")
            assert result["state"] == expected, result
    asyncio.run(check())


def test_restart_reconciles_claim_that_never_launched(tmp_path):
    store, schema, config, task = setup(tmp_path)
    async def check():
        async with supervisor(tmp_path, store, schema, config) as first:
            claim = first.claim(task, config.bindings[0])
        async with supervisor(tmp_path, store, schema, config):
            assert store.snapshot()["runs"][claim.run["id"]]["state"] == "interrupted"
    asyncio.run(check())


def test_restart_terminates_recorded_process_before_releasing_claim(tmp_path):
    store, schema, config, task = setup(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    async def check():
        async with supervisor(tmp_path, store, schema, config) as first:
            claim = first.claim(task, config.bindings[0])
            first._transition(claim.run["id"], "starting")
            store.record_process(claim.run["id"], owner_id=first.id, identity=process_identity(process.pid))
        async with supervisor(tmp_path, store, schema, config):
            assert process_identity(process.pid) is None
            assert store.snapshot()["runs"][claim.run["id"]]["state"] == "interrupted"
    try:
        asyncio.run(check())
    finally:
        process.kill()
        process.wait()


def test_launch_gate_eof_does_not_execute_command(tmp_path):
    from greatminds.runtime import agent_exec
    read_fd, write_fd = os.pipe()
    script = "from pathlib import Path; Path('executed').touch()"
    process = subprocess.Popen([sys.executable, agent_exec.__file__, str(read_fd),
                                sys.executable, "-c", script], pass_fds=(read_fd,), cwd=tmp_path)
    os.close(read_fd)
    os.close(write_fd)  # Parent died before durable PID record / launch authorization.
    assert process.wait(timeout=5) == 125
    assert not (tmp_path / "executed").exists()
