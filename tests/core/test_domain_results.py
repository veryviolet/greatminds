import json
from pathlib import Path

import pytest
import yaml

from greatminds.cli import task as policy
from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.results import ResultService
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.store import ResultEnvelope, RunStore, TaskRevision


def task_document(task_id="0001-domain"):
    return {"id": task_id, "stream": "product", "kind": "research", "scope": "backend",
            "reporter": "USER", "opened_at": "2026-09-06T10:00:00Z", "priority": "normal",
            "title": "Domain fixture", "blocks": []}


def setup(tmp_path, *, role="DEVELOPER", queue="feature_dev", payload=None, decision="handoff", finish=True):
    schema = load_schema_snapshot()
    config = parse_execution_config({"version": 1, "agents": {"fixture": {
        "transport": "acp", "argv": ["unused"], "adapter_version": "fixture", "harness_version": "fixture"}},
        "bindings": {"role": {"role": role, "agent": "fixture"}}}, roles=set(schema.document["roles"]))
    runtime = tmp_path / ".greatminds"
    source = runtime / queue / "0001-domain.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(yaml.safe_dump(task_document()))
    (tmp_path / "evidence.txt").write_text("changed implementation")
    store = RunStore(runtime)
    claim = store.claim(task=TaskRevision.capture(runtime, source), binding=config.bindings[0],
                        config=config, schema=schema, project=tmp_path, owner_id="supervisor")
    for state in ("starting", "running"):
        store.transition(claim.run["id"], owner_id="supervisor", event_id=state, target=state)
    payload = payload if payload is not None else {
        "to_queue": "feature_test", "blocks": [{"kind": "implementation", "base_commit": "abc123",
                                                  "files": ["evidence.txt"], "ready_for_test": True}],
        "artifacts": ["evidence.txt"]}
    envelope = ResultEnvelope("domain-result", claim.run["id"], claim.run["task_id"],
                              claim.run["task_revision"], schema.sha256, decision, payload)
    store.receive_result(envelope, token=claim.token)
    if finish:
        store.transition(claim.run["id"], owner_id="supervisor", event_id="finished", target="completed")
    return store, source, claim, envelope


def test_result_applies_one_transition_and_preserves_decision_provenance(tmp_path):
    store, source, claim, envelope = setup(tmp_path)
    receipt = ResultService(store).apply(envelope.result_id)
    assert receipt["status"] == "applied", receipt
    target = store.runtime / "feature_test" / source.name
    assert target.is_file() and not source.exists()
    data = yaml.safe_load(target.read_text())
    block = data["blocks"][0]
    assert block["by"] == "DEVELOPER"
    assert block["closed_by"] == "DEVELOPER"
    assert block["provenance"]["run_id"] == claim.run["id"]
    entries = [json.loads(line) for line in (store.runtime / "journal.ndjson").read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["actor"] == "SYSTEM"
    assert entries[0]["decision_by"] == "DEVELOPER"
    assert ResultService(store).apply(envelope.result_id) == receipt
    assert len(yaml.safe_load(target.read_text())["blocks"]) == 1


@pytest.mark.parametrize("stage", ["prepared", "task_written", "task_moved", "journaled", "committed"])
def test_crash_recovery_never_duplicates_blocks_or_journal(tmp_path, stage):
    store, source, _, envelope = setup(tmp_path)
    def crash(at):
        if at == stage:
            raise RuntimeError("injected crash")
    with pytest.raises(RuntimeError, match="injected crash"):
        ResultService(store, checkpoint=crash).apply(envelope.result_id)
    receipt = ResultService(RunStore(store.runtime)).apply(envelope.result_id)
    assert receipt["status"] == "applied", receipt
    assert not source.exists()
    assert len(yaml.safe_load((store.runtime / "feature_test" / source.name).read_text())["blocks"]) == 1
    assert len((store.runtime / "journal.ndjson").read_text().splitlines()) == 1


@pytest.mark.parametrize("payload", [
    {"to_queue": "verified", "blocks": []},
    {"to_queue": "feature_test", "blocks": []},
    {"to_queue": "feature_test", "blocks": [{"kind": "implementation", "by": "ARCHITECT-REVIEWER"}]},
    {"to_queue": "feature_test", "blocks": [{"kind": "review", "outcome": "approved", "commit": "abc"}]},
])
def test_invalid_handoff_has_no_partial_task_mutation(tmp_path, payload):
    store, source, _, envelope = setup(tmp_path, payload=payload)
    before = source.read_bytes()
    receipt = ResultService(store).apply(envelope.result_id)
    assert receipt["status"] == "rejected", receipt
    assert source.read_bytes() == before
    assert not (store.runtime / "journal.ndjson").exists()


def test_stale_result_is_rejected_after_receipt_before_application(tmp_path):
    store, source, _, envelope = setup(tmp_path)
    source.write_text(source.read_text() + "description: changed by operator\n")
    assert ResultService(store).apply(envelope.result_id)["status"] == "rejected"
    assert source.exists()


def test_cli_alias_cannot_mutate_task_during_incomplete_operation(tmp_path, monkeypatch):
    store, source, _, envelope = setup(tmp_path)
    def crash(stage):
        if stage == "task_written":
            raise RuntimeError("crash")
    with pytest.raises(RuntimeError):
        ResultService(store, checkpoint=crash).apply(envelope.result_id)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    with pytest.raises(GreatMindsError, match="incomplete domain operation"):
        policy.move_task(task_id="0001", to_queue="feature_test")
    assert source.exists()


def test_changed_artifact_holds_recovery_without_applying_stale_evidence(tmp_path):
    store, source, _, envelope = setup(tmp_path)
    def crash(stage):
        if stage == "prepared":
            raise RuntimeError("crash")
    with pytest.raises(RuntimeError):
        ResultService(store, checkpoint=crash).apply(envelope.result_id)
    (tmp_path / "evidence.txt").write_text("different revision")
    receipt = ResultService(store).apply(envelope.result_id)
    assert receipt["status"] == "needs_recovery"
    assert source.exists()
    assert yaml.safe_load(source.read_text())["blocks"] == []


def test_uncertain_merge_is_never_repeated_after_crash(tmp_path, monkeypatch):
    store, source, _, envelope = setup(tmp_path)
    service = ResultService(store)
    prepare = service._prepare
    def with_merge(*args):
        operation = prepare(*args)
        operation["pre"] = ["merge"]
        return operation
    monkeypatch.setattr(service, "_prepare", with_merge)
    calls = []
    def interrupted_action(operation, kind):
        calls.append(kind)
        raise RuntimeError("crash after command may have committed")
    monkeypatch.setattr(service, "_action", interrupted_action)
    with pytest.raises(RuntimeError):
        service.apply(envelope.result_id)
    receipt = service.apply(envelope.result_id)
    assert receipt["status"] == "needs_recovery"
    assert calls == ["merge"]
    assert source.exists()


def test_recovery_rechecks_dynamic_gates_before_moving_task(tmp_path, monkeypatch):
    store, source, _, envelope = setup(tmp_path)
    def crash(stage):
        if stage == "prepared":
            raise RuntimeError("crash")
    with pytest.raises(RuntimeError):
        ResultService(store, checkpoint=crash).apply(envelope.result_id)
    def changed_gate(*args):
        raise GreatMindsError("external gate no longer satisfied", exit_code=2)
    monkeypatch.setattr(policy, "enforce_schema_requires", changed_gate)
    receipt = ResultService(store).apply(envelope.result_id)
    assert receipt["status"] == "needs_recovery"
    assert source.exists()
    assert yaml.safe_load(source.read_text())["blocks"] == []


def test_context_uses_pinned_tables_without_mutating_global_schema(tmp_path):
    baseline = policy.schema()
    document = load_schema_snapshot().document
    document["product_enums"]["priorities"] = ["pinned-only"]
    data = task_document()
    data["priority"] = "pinned-only"
    with policy.domain_context(document=document, runtime=tmp_path, workspace=tmp_path):
        policy.validate_task(data)
    assert policy.schema() is baseline
    with pytest.raises(GreatMindsError):
        policy.validate_task(data)


def test_needs_input_records_question_without_moving_task(tmp_path):
    store, source, _, envelope = setup(tmp_path, decision="needs_input", payload={"question": "Which behavior is intended?"})
    receipt = ResultService(store).apply(envelope.result_id)
    assert receipt["status"] == "applied"
    assert receipt["details"]["question"] == "Which behavior is intended?"
    assert source.exists()


def test_run_identity_cannot_be_overridden_with_role_environment(tmp_path, monkeypatch):
    from greatminds.core.paths import caller_role
    store, _, claim, _ = setup(tmp_path, finish=False)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("GREATMINDS_RUN_ID", claim.run["id"])
    monkeypatch.setenv("GREATMINDS_RUN_TOKEN", claim.token)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")
    with pytest.raises(GreatMindsError, match="does not match"):
        caller_role()
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    assert caller_role() == "DEVELOPER"
    with pytest.raises(GreatMindsError, match="run submit"):
        policy.move_task(task_id="0001-domain", to_queue="feature_test")
