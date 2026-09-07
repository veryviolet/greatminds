import asyncio
import json
import subprocess
import sys

import pytest
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.results import ResultService
from greatminds.runtime.commands import CommandService
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.store import ResultEnvelope, RunStore, TaskRevision


def setup(tmp_path, script="print('checked')", *, role="DEVELOPER", queue="feature_dev", task_blocks=None, **options):
    schema = load_schema_snapshot()
    command = {"argv": [sys.executable, "-c", script], "roles": [role], **options}
    config = parse_execution_config({"version": 1, "agents": {"fixture": {
        "transport": "acp", "argv": ["unused"], "adapter_version": "fixture", "harness_version": "fixture"}},
        "bindings": {"dev": {"role": role, "agent": "fixture"}},
        "commands": {"check": command}}, roles=set(schema.document["roles"]))
    runtime = tmp_path / ".greatminds"
    task = runtime / queue / "0001-command.yaml"
    task.parent.mkdir(parents=True)
    task.write_text(yaml.safe_dump({"id": task.stem, "title": "Command fixture", "kind": "research",
        "stream": "product", "scope": "backend", "reporter": "USER", "priority": "normal",
        "opened_at": "2026-09-06T10:00:00Z", "blocks": task_blocks or []}))
    (tmp_path / "source.txt").write_text("source version one")
    store = RunStore(runtime)
    claim = store.claim(task=TaskRevision.capture(runtime, task), binding=config.bindings[0],
                        config=config, schema=schema, project=tmp_path, owner_id="owner")
    for status in ("starting", "running"):
        store.transition(claim.run["id"], owner_id="owner", event_id=status, target=status)
    return store, claim, CommandService(store, environment={})


def envelope(claim, payload=None):
    return ResultEnvelope("result", claim.run["id"], claim.run["task_id"], claim.run["task_revision"],
                          claim.run["schema_sha256"], "no_change", payload or {})


def execute(service, claim, request_id="check-one"):
    request = service.request(claim.run["id"], "check", token=claim.token, request_id=request_id)
    return asyncio.run(service.execute(request["id"], "owner"))


def test_output_preview_is_bounded_and_rejects_modified_or_linked_artifacts(tmp_path):
    from pathlib import Path
    store, claim, service = setup(tmp_path, "print('abcdefghij')")
    item = execute(service, claim)
    preview = service.output_preview(item["id"], limit=4)
    assert preview["stdout"]["text"] == "abcd"
    assert preview["stdout"]["truncated"] and preview["stdout"]["bytes"] == 11
    assert preview["stderr"]["text"] == "" and not preview["stderr"]["truncated"]
    assert "output_preview" not in store.snapshot()["commands"][item["id"]]
    output = Path(item["output"]["stdout"]["path"])
    output.write_text("changed!!!!")
    with pytest.raises(GreatMindsError, match="artifact changed"):
        service.output_preview(item["id"])
    outside = tmp_path / "unrelated.txt"
    outside.write_text("abcdefghij\n")
    output.unlink()
    output.symlink_to(outside)
    with pytest.raises(GreatMindsError, match="recorded local artifact"):
        service.output_preview(item["id"])


def test_command_status_scopes_agent_reads_and_includes_preview(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from greatminds.cli.main import cli
    store, claim, service = setup(tmp_path)
    item = execute(service, claim)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("GREATMINDS_RUN_ID", claim.run["id"])
    monkeypatch.setenv("GREATMINDS_RUN_TOKEN", claim.token)
    result = CliRunner().invoke(cli, ["run", "command-status", item["id"]])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["output_preview"]["stdout"]["text"] == "checked\n"
    monkeypatch.setenv("GREATMINDS_RUN_TOKEN", "incorrect")
    assert CliRunner().invoke(cli, ["run", "command-status", item["id"]]).exit_code == 3


def test_recorded_command_and_output_are_bound_to_result(tmp_path):
    store, claim, service = setup(tmp_path)
    pending = service.request(claim.run["id"], "check", token=claim.token, request_id="check-one")
    assert service.request(claim.run["id"], "check", token=claim.token, request_id="check-one") == pending
    with pytest.raises(GreatMindsError, match="pending command"):
        store.receive_result(envelope(claim), token=claim.token)
    record = asyncio.run(service.execute(pending["id"], "owner"))
    assert record["status"] == "succeeded", record
    assert record["exit_code"] == 0
    assert record["task_revision"] == claim.run["task_revision"]
    assert record["source_before"] == record["source_after"]
    from pathlib import Path
    output = Path(record["output"]["stdout"]["path"])
    assert output.read_text() == "checked\n"
    assert output.stat().st_mode & 0o777 == 0o600
    assert service.evidence(claim.run, record["id"]) == record
    store.receive_result(envelope(claim, {"command_evidence": [record["id"]]}), token=claim.token)
    store.transition(claim.run["id"], owner_id="owner", event_id="finished", target="completed")
    applied = ResultService(store, environment={}).apply("result")
    assert applied["status"] == "applied", applied
    assert applied["details"]["command_evidence"] == [record["id"]]


@pytest.mark.parametrize("change", ["source", "output", "environment"])
def test_changed_evidence_cannot_support_domain_result(tmp_path, change):
    store, claim, service = setup(tmp_path, environment={"CHECK_SETTING": "CHECK_SETTING"})
    record = execute(service, claim)
    from pathlib import Path
    if change == "source":
        (tmp_path / "source.txt").write_text("source version two")
    elif change == "output":
        Path(record["output"]["stdout"]["path"]).write_text("forged result")
    else:
        service.environment["CHECK_SETTING"] = "changed"
    with pytest.raises(GreatMindsError, match="stale|artifact changed"):
        service.evidence(claim.run, record["id"])
    store.receive_result(envelope(claim, {"command_evidence": [record["id"]]}), token=claim.token)
    store.transition(claim.run["id"], owner_id="owner", event_id="finished", target="completed")
    assert ResultService(store, environment=service.environment).apply("result")["status"] == "rejected"


@pytest.mark.parametrize("script,status,reason", [
    ("import sys; print('failure', file=sys.stderr); sys.exit(7)", "failed", "exit"),
    ("from pathlib import Path; Path('source.txt').write_text('changed')", "stale", "source_changed_during_command"),
    ("import time; time.sleep(60)", "failed", "timeout"),
])
def test_command_failure_and_source_changes_are_not_passing_evidence(tmp_path, script, status, reason):
    store, claim, service = setup(tmp_path, script, timeout_seconds=1)
    record = execute(service, claim)
    assert (record["status"], record["reason"]) == (status, reason), record
    with pytest.raises(GreatMindsError, match="not a successful"):
        service.evidence(claim.run, record["id"])


def test_output_limit_does_not_deadlock_a_noisy_command(tmp_path):
    _, claim, service = setup(tmp_path, "import sys; print('x'*200000); print('y'*200000,file=sys.stderr)",
                              max_output_bytes=1024)
    record = execute(service, claim)
    assert record["status"] == "succeeded", record
    assert all(item["truncated"] and item["stored_bytes"] == 1024 for item in record["output"].values())


@pytest.mark.parametrize("options,message", [
    ({"roles": ["TESTER"]}, "assigned role"),
    ({"purpose": "publication"}, "explicit project authorization"),
    ({"purpose": "deployment"}, "explicit project authorization"),
])
def test_only_authorized_commands_can_be_requested(tmp_path, options, message):
    _, claim, service = setup(tmp_path, **options)
    with pytest.raises(GreatMindsError, match=message):
        service.request(claim.run["id"], "check", token=claim.token)
    with pytest.raises(GreatMindsError, match="credential"):
        service.request(claim.run["id"], "check", token="wrong")


def test_missing_command_environment_never_launches(tmp_path):
    _, claim, service = setup(tmp_path, required_env=["MISSING"])
    record = execute(service, claim)
    assert record["status"] == "failed"
    assert "process" not in record


def test_secret_values_are_not_in_command_snapshot(tmp_path):
    store, claim, service = setup(tmp_path, environment={"CHECK_TOKEN": "CHECK_TOKEN"})
    service.environment["CHECK_TOKEN"] = "private-fixture-value-123"
    record = execute(service, claim)
    assert record["status"] == "succeeded", record
    assert service.environment["CHECK_TOKEN"] not in json.dumps(store.snapshot())


def test_uncertain_command_is_held_and_never_replayed_on_recovery(tmp_path):
    store, claim, service = setup(tmp_path)
    record = execute(service, claim)
    # Model lost final receipt after recorded exec, preserving the process's
    # real identity. Cleanup safely recognizes that it has already exited.
    service._update(record["id"], status="running", exit_code=None)
    asyncio.run(service.recover("new-owner"))
    assert service.get(record["id"])["status"] == "needs_recovery"
    assert asyncio.run(service.execute(record["id"], "new-owner"))["status"] == "needs_recovery"
    store.transition(claim.run["id"], owner_id="owner", event_id="interrupted", target="interrupted")
    with pytest.raises(GreatMindsError, match="pending command"):
        store.request_control(claim.run["id"], "retry")
    resolved = service.resolve(record["id"], reason="Inspected effects; authorize a new run separately")
    assert resolved["status"] == "resolved"
    snapshot = store.snapshot()
    assert service.resolve(record["id"], reason=resolved["resolution"]) == resolved
    assert store.snapshot() == snapshot
    with pytest.raises(GreatMindsError, match="different explanation"):
        service.resolve(record["id"], reason="changed explanation")
    with pytest.raises(GreatMindsError, match="not a successful"):
        service.evidence(claim.run, record["id"])
    assert store.request_control(claim.run["id"], "retry")["kind"] == "retry"


def test_run_end_cancels_a_command_before_its_coroutine_starts(tmp_path):
    store, claim, service = setup(tmp_path)
    request = service.request(claim.run["id"], "check", token=claim.token)
    async def check():
        await service.poll("owner")
        await service.finish_run(claim.run["id"])
    asyncio.run(check())
    assert service.get(request["id"])["status"] == "cancelled"


def test_git_evidence_hashes_dirty_and_untracked_files_but_not_ignored_output(tmp_path):
    from greatminds.runtime.commands import source_identity
    store, _, _ = setup(tmp_path)
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("generated/\n.greatminds/\n")
    subprocess.run(["git", "add", "source.txt", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "Initial"], cwd=tmp_path, check=True, capture_output=True)
    first = source_identity(tmp_path, store.runtime)
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "out.txt").write_text("not source")
    assert source_identity(tmp_path, store.runtime) == first
    (tmp_path / "source.txt").write_text("uncommitted change")
    dirty = source_identity(tmp_path, store.runtime)
    assert dirty != first and dirty["commit"] == first["commit"]
    (tmp_path / "new.txt").write_text("new source")
    assert source_identity(tmp_path, store.runtime) != dirty


@pytest.mark.parametrize("reference", [False, True])
def test_test_block_cannot_invent_configured_command_evidence(tmp_path, reference):
    store, claim, service = setup(tmp_path, role="TESTER", queue="feature_test")
    record = execute(service, claim)
    block = {"kind": "tests", "base_commit": "fixture", "test_files": ["source.txt"],
             "test_command": "invented", "test_result": "pass", "ready_for_review": True,
             "gate_check_result": "n/a", "gate_check_commit": "fixture",
             "gate_check_at": "2026-09-06T10:00:00Z"}
    if reference:
        block["command_request_id"] = record["id"]
    result = envelope(claim)
    from dataclasses import replace
    result = replace(result, decision="handoff", payload={"to_queue": "feature_review", "blocks": [block]})
    store.receive_result(result, token=claim.token)
    store.transition(claim.run["id"], owner_id="owner", event_id="finished", target="completed")
    receipt = ResultService(store, environment={}).apply("result")
    assert receipt["status"] == "rejected", receipt
    assert ("conflicts with recorded" if reference else "requires recorded command_request_id") in receipt["details"]["error"]


def test_run_cancel_cleans_a_running_command(tmp_path):
    from greatminds.runtime.processes import process_identity
    _, claim, service = setup(tmp_path, "import time; time.sleep(60)")
    request = service.request(claim.run["id"], "check", token=claim.token)
    async def check():
        await service.poll("owner")
        deadline = asyncio.get_running_loop().time() + 5
        while service.get(request["id"])["status"] != "running":
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        identity = service.get(request["id"])["process"]
        await service.finish_run(claim.run["id"])
        assert service.get(request["id"])["status"] == "cancelled"
        assert process_identity(identity["pid"]) is None
    asyncio.run(check())


@pytest.mark.parametrize("exit_code,target", [(0, "feature_review"), (7, "feature_dev")])
def test_tests_block_receives_recorded_command_and_keeps_semantic_gates(tmp_path, exit_code, target):
    from dataclasses import replace
    import shlex
    store, claim, service = setup(tmp_path, f"import sys; print('checked'); sys.exit({exit_code})",
                                  role="TESTER", queue="feature_test", task_blocks=[{
                                      "kind": "plan", "by": "PLANNER", "at": "2026-09-06T10:00:00Z",
                                      "base_commit": "fixture", "assignee_role": "DEVELOPER",
                                      "stand_required": False, "plan_kind": "full", "mode": "A",
                                      "ready_for_implementation": True}])
    record = execute(service, claim)
    block = {"kind": "tests", "command_request_id": record["id"], "base_commit": "fixture",
             "test_files": ["source.txt"], "ready_for_review": exit_code == 0,
             "gate_check_result": "n/a", "gate_check_commit": "fixture",
             "gate_check_at": "2026-09-06T10:00:00Z", "functional_probes": ["configured fixture check"],
             "stand_evidence": {"tester_observations": "Observed successful fixture output"}}
    result = replace(envelope(claim), decision="handoff",
                     payload={"to_queue": target, "blocks": [block]})
    store.receive_result(result, token=claim.token)
    store.transition(claim.run["id"], owner_id="owner", event_id="finished", target="completed")
    receipt = ResultService(store, environment={}).apply("result")
    assert receipt["status"] == "applied", receipt
    data = yaml.safe_load((store.runtime / target / "0001-command.yaml").read_text())
    assert data["blocks"][-1]["test_command"] == shlex.join(record["argv"])
    assert data["blocks"][-1]["test_result"] == ("pass" if exit_code == 0 else "fail")
    assert data["blocks"][-1]["provenance"]["command_request_id"] == record["id"]
    assert not (store.runtime / "verified" / "0001-command.yaml").exists()
