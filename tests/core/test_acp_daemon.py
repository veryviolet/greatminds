import asyncio
import json
import sys
import subprocess
import time
from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.runtime.daemon import serve
from greatminds.runtime.store import RunStore
from greatminds.runtime.processes import process_identity, terminate_group


SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "acp_server.py"


def project(tmp_path, *, scenario="echo", tasks=True):
    config = {"version": 1, "agents": {"anything-acp": {
        "transport": "acp", "argv": [sys.executable, str(SERVER), scenario],
        "adapter_version": "fixture", "harness_version": "fixture"}},
        "bindings": {"implementation": {"role": "DEVELOPER", "agent": "anything-acp",
                                          "scheduling": "queue", "timeout_seconds": 5}}}
    (tmp_path / "coordination").mkdir()
    (tmp_path / "coordination" / "execution.yaml").write_text(yaml.safe_dump(config))
    queue = tmp_path / ".greatminds" / "feature_dev"
    queue.mkdir(parents=True)
    if tasks:
        (queue / "0001-example.yaml").write_text(yaml.safe_dump({
            "id": "0001-example", "stream": "product", "kind": "research", "scope": "backend",
            "reporter": "USER", "opened_at": "2026-09-06T10:00:00Z", "priority": "normal",
            "title": "Implement example", "blocks": []}))
    return tmp_path


def test_idle_daemon_never_starts_an_agent(tmp_path):
    root = project(tmp_path, tasks=False)
    for _ in range(2):
        snapshot = asyncio.run(serve(root, once=True, environment={}))
        assert snapshot["runs"] == {}
    assert not (root / "agent-starts.log").exists()


def test_coordd_routes_explicit_execution_contract_through_acp(tmp_path):
    root = project(tmp_path)
    runner = CliRunner()
    outcome = runner.invoke(cli, ["coordd", "--project-dir", str(root), "--once"])
    assert outcome.exit_code == 0, outcome.exception
    snapshot = RunStore(root / ".greatminds").snapshot()
    assert len(snapshot["runs"]) == 1
    run = next(iter(snapshot["runs"].values()))
    assert run["state"] == "completed"
    assert run["agent_id"] == "anything-acp"
    status = runner.invoke(cli, ["run", "status", "--project-dir", str(root)])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["assignments"][0]["reason"] == "revision_already_attempted"
    again = runner.invoke(cli, ["coordd", "--project-dir", str(root), "--once"])
    assert again.exit_code == 0, again.exception
    assert (root / "agent-starts.log").read_text().splitlines() == ["echo"]


def test_typed_result_travels_from_agent_cli_to_durable_receipt(tmp_path):
    root = project(tmp_path, scenario="submit")
    snapshot = asyncio.run(serve(root, once=True, environment={}))
    run = next(iter(snapshot["runs"].values()))
    assert run["state"] == "completed", run
    receipt = snapshot["results"]["fixture-result"]
    assert receipt["role"] == "DEVELOPER"
    assert receipt["status"] == "applied"
    assert receipt["envelope"]["run_id"] == run["id"]
    assert (root / ".greatminds" / "feature_dev" / "0001-example.yaml").exists()


def test_acp_decision_advances_task_only_through_domain_gates(tmp_path):
    root = project(tmp_path, scenario="handoff")
    snapshot = asyncio.run(serve(root, once=True, environment={}))
    receipt = snapshot["results"]["fixture-result"]
    assert receipt["status"] == "applied", receipt
    assert not (root / ".greatminds" / "feature_dev" / "0001-example.yaml").exists()
    data = yaml.safe_load((root / ".greatminds" / "feature_test" / "0001-example.yaml").read_text())
    assert data["blocks"][0]["by"] == "DEVELOPER"
    assert data["blocks"][0]["provenance"]["applied_by"] == "SYSTEM"


def test_pause_prevents_dispatch_and_resume_allows_work(tmp_path, monkeypatch):
    root = project(tmp_path)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(root))
    runner = CliRunner()
    assert runner.invoke(cli, ["run", "pause"]).exit_code == 0
    assert asyncio.run(serve(root, once=True, environment={}))["runs"] == {}
    assert runner.invoke(cli, ["run", "resume"]).exit_code == 0
    assert len(asyncio.run(serve(root, once=True, environment={}))["runs"]) == 1


def test_explicit_retry_authorizes_exactly_one_further_attempt(tmp_path):
    root = project(tmp_path)
    first = asyncio.run(serve(root, once=True, environment={}))
    store = RunStore(root / ".greatminds")
    run_id = next(iter(first["runs"]))
    control = store.request_control(run_id, "retry")
    assert store.request_control(run_id, "retry") == control
    second = asyncio.run(serve(root, once=True, environment={}))
    assert len(second["runs"]) == 2
    assert second["runs"][run_id]["control"]["status"] == "completed"
    third = asyncio.run(serve(root, once=True, environment={}))
    assert len(third["runs"]) == 2
    assert (root / "agent-starts.log").read_text().splitlines() == ["echo", "echo"]


def test_compatible_session_is_loaded_only_when_capability_is_advertised(tmp_path):
    root = project(tmp_path, scenario="resume")
    first = asyncio.run(serve(root, once=True, environment={}))
    store = RunStore(root / ".greatminds")
    run_id = next(iter(first["runs"]))
    store.request_control(run_id, "retry")
    second = asyncio.run(serve(root, once=True, environment={}))
    latest = max(second["runs"].values(), key=lambda run: run["sequence"])
    assert latest["outcome"]["session_strategy"] == "loaded"
    assert (root / "loads.log").read_text().splitlines() == ["test-session"]


def test_run_submit_rejects_missing_or_wrong_environment_identity(tmp_path, monkeypatch):
    project(tmp_path)
    source = tmp_path / "result.json"
    source.write_text(json.dumps({"result_id": "r1", "run_id": "other", "task_id": "0001-example",
                                  "task_revision": "x", "schema_sha256": "x", "decision": "no_change",
                                  "payload": {}}))
    monkeypatch.delenv("GREATMINDS_RUN_ID", raising=False)
    monkeypatch.delenv("GREATMINDS_RUN_TOKEN", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "submit", "--file", str(source)])
    assert result.exit_code == 3
    monkeypatch.setenv("GREATMINDS_RUN_ID", "assigned")
    monkeypatch.setenv("GREATMINDS_RUN_TOKEN", "synthetic")
    result = runner.invoke(cli, ["run", "submit", "--file", str(source)])
    assert result.exit_code == 3


def wait_for_run(store, state, process):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert process.poll() is None, "daemon exited before expected state"
        for run in store.snapshot()["runs"].values():
            if run["state"] == state:
                return run
        time.sleep(0.05)
    raise AssertionError(f"daemon did not reach {state}: {store.snapshot()}")


def test_sigkill_recovery_cleans_orphan_without_replaying_task(tmp_path):
    root = project(tmp_path, scenario="orphan")
    source = root / "coordination" / "execution.yaml"
    document = yaml.safe_load(source.read_text())
    document["bindings"]["implementation"]["timeout_seconds"] = 60
    source.write_text(yaml.safe_dump(document))
    store = RunStore(root / ".greatminds")
    daemon = subprocess.Popen([sys.executable, "-m", "greatminds.cli.main", "coordd",
                               "--project-dir", str(root), "--interval-sec", "0.2"],
                              cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    identity = None
    try:
        run = wait_for_run(store, "running", daemon)
        identity = run["process"]
        daemon.kill()
        daemon.wait(timeout=5)
        assert process_identity(identity["pid"]) == identity
        recovered = asyncio.run(serve(root, once=True, environment={}))
        assert process_identity(identity["pid"]) is None
        assert recovered["runs"][run["id"]]["state"] == "interrupted"
        assert len(recovered["runs"]) == 1
        assert (root / "agent-starts.log").read_text().splitlines() == ["orphan"]
    finally:
        if daemon.poll() is None:
            daemon.kill()
        daemon.wait()
        if identity:
            asyncio.run(terminate_group(identity, timeout=0.2))
        daemon.stderr.close()


def test_operator_cancel_reaches_running_acp_daemon(tmp_path):
    root = project(tmp_path, scenario="hang")
    store = RunStore(root / ".greatminds")
    daemon = subprocess.Popen([sys.executable, "-m", "greatminds.cli.main", "coordd",
                               "--project-dir", str(root), "--interval-sec", "0.2"],
                              cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        run = wait_for_run(store, "running", daemon)
        store.request_control(run["id"], "cancel")
        cancelled = wait_for_run(store, "cancelled", daemon)
        assert cancelled["id"] == run["id"]
        assert process_identity(run["process"]["pid"]) is None
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=6)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
        daemon.stderr.close()
