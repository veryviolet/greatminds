import json
from dataclasses import replace

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.cli.task import task_file_lock
from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.dependencies import cycle_components, inspect_dependencies
from greatminds.domain.maintenance import MaintenanceService
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.store import RunStore, TaskRevision


def document(task_id, *, dependencies=None, resume_to="feature_dev", ready=True, reason="waiting for prerequisite"):
    blocks = []
    if dependencies is not None:
        blocks = [{"kind": "plan", "by": "ARCHITECT-PLANNER", "at": "2026-09-06T10:00:00Z",
                   "base_commit": "fixture", "assignee_role": "DEVELOPER", "stand_required": False,
                   "plan_kind": "full", "mode": "A", "ready_for_implementation": ready},
                  {"kind": "blocked", "by": "DEVELOPER", "at": "2026-09-06T10:00:00Z",
                   "reason": reason, "dependencies": dependencies, "resume_to": resume_to}]
    return {"id": task_id, "title": "Dependency fixture", "stream": "product", "kind": "research",
            "scope": "backend", "reporter": "USER", "opened_at": "2026-09-06T10:00:00Z",
            "priority": "normal", "blocks": blocks}


def write(runtime, queue, task_id, **kwargs):
    path = runtime / queue / f"{task_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document(task_id, **kwargs)))
    return path


def setup(tmp_path, **kwargs):
    runtime = tmp_path / ".greatminds"
    source = write(runtime, "feature_blocked", "0001-waiter", dependencies=["verified/0002-dependency.yaml"], **kwargs)
    dependency = write(runtime, "verified", "0002-dependency")
    store = RunStore(runtime)
    service = MaintenanceService(store, load_schema_snapshot(), environment={})
    return service, source, dependency


def test_system_resumes_once_with_dependency_evidence_and_without_a_run(tmp_path):
    service, source, dependency = setup(tmp_path)
    original = source.read_bytes()
    report = service.inspect()
    assert report["tasks"][source.stem]["status"] == "ready", report
    assert not service.store.path.exists()  # Inspection is read-only.
    service.reconcile()
    assert not source.exists()
    target = service.store.runtime / "feature_dev" / source.name
    assert target.read_bytes() == original
    snapshot = service.store.snapshot()
    assert snapshot["runs"] == {}
    operation = next(iter(snapshot["maintenance"].values()))
    assert operation["status"] == "applied"
    journal = [json.loads(line) for line in (service.store.runtime / "journal.ndjson").read_text().splitlines()]
    assert len(journal) == 1 and journal[0]["actor"] == "SYSTEM"
    assert journal[0]["blocked_by"] == "DEVELOPER"
    assert journal[0]["dependency_evidence"][0]["task_revision"] == TaskRevision.capture(service.store.runtime, dependency).sha256
    service.reconcile()
    assert service.store.snapshot() == snapshot


@pytest.mark.parametrize("stage", ["prepared", "task_moved", "journaled"])
def test_system_resume_recovers_each_durable_stage_without_duplicate_journal(tmp_path, stage):
    service, source, _ = setup(tmp_path)
    def crash(at):
        if at == stage:
            raise RuntimeError("injected crash")
    service.checkpoint = crash
    with pytest.raises(RuntimeError, match="injected crash"):
        service.reconcile()
    restarted = MaintenanceService(service.store, service.schema, environment={})
    restarted.reconcile()
    assert not source.exists()
    assert len((service.store.runtime / "journal.ndjson").read_text().splitlines()) == 1
    assert next(iter(service.store.snapshot()["maintenance"].values()))["status"] == "applied"


def test_changed_dependency_holds_recovery_and_bounded_repair_rechecks_it(tmp_path):
    service, source, dependency = setup(tmp_path)
    original = dependency.read_bytes()
    def crash(at):
        if at == "prepared":
            dependency.unlink()
            raise RuntimeError("injected crash")
    service.checkpoint = crash
    with pytest.raises(RuntimeError):
        service.reconcile()
    restarted = MaintenanceService(service.store, service.schema, environment={})
    restarted.reconcile()
    operation = next(iter(service.store.snapshot()["maintenance"].values()))
    assert operation["status"] == "needs_recovery" and source.exists()
    with pytest.raises(GreatMindsError, match="incomplete system operation"):
        with task_file_lock(service.store.runtime, "0001"):
            pytest.fail("CLI alias acquired an incomplete task")
    restarted.request_repair(operation["id"])
    restarted.reconcile()
    assert service.store.snapshot()["maintenance"][operation["id"]]["status"] == "needs_recovery"
    dependency.write_bytes(original)
    restarted.request_repair(operation["id"])
    restarted.reconcile()
    assert service.store.snapshot()["maintenance"][operation["id"]]["status"] == "applied"


def test_abandon_uncommitted_intent_preserves_task_changes_and_allows_fresh_evidence(tmp_path):
    service, source, dependency = setup(tmp_path)
    def crash(at):
        if at == "prepared":
            dependency.write_text(dependency.read_text() + "description: updated evidence\n")
            raise RuntimeError("crash")
    service.checkpoint = crash
    with pytest.raises(RuntimeError):
        service.reconcile()
    restarted = MaintenanceService(service.store, service.schema, environment={})
    restarted.reconcile()
    operation = next(iter(service.store.snapshot()["maintenance"].values()))
    original = source.read_bytes()
    restarted.abandon(operation["id"], reason="Prerequisite changed; prepare fresh evidence")
    assert source.read_bytes() == original
    restarted.reconcile()
    states = {op["status"] for op in service.store.snapshot()["maintenance"].values()}
    assert states == {"abandoned", "applied"}


@pytest.mark.parametrize("change,expected", [
    ("missing", "waiting"), ("active", "waiting"), ("wrong_terminal", "wrong_terminal"),
    ("malformed", "malformed"), ("duplicate", "malformed"), ("unready", "gate_failed"),
    ("withdrawn", "withdrawn"), ("terminal_resume", "malformed"), ("wrong_identity", "malformed"),
])
def test_nonready_dependencies_never_trigger_a_system_move(tmp_path, change, expected):
    service, source, dependency = setup(tmp_path, ready=change != "unready",
        reason="withdrawn by operator" if change == "withdrawn" else "waiting for prerequisite",
        resume_to="verified" if change == "terminal_resume" else "feature_dev")
    if change == "missing":
        dependency.unlink()
    elif change in {"active", "wrong_terminal"}:
        target = dependency.parent.parent / ("archive" if change == "wrong_terminal" else "feature_dev")
        target.mkdir()
        dependency.rename(target / dependency.name)
    elif change == "malformed":
        data = yaml.safe_load(source.read_text())
        data["blocks"][-1]["dependencies"].append("../../elsewhere")
        source.write_text(yaml.safe_dump(data))
    elif change == "duplicate":
        write(service.store.runtime, "archive", dependency.stem)
    elif change == "wrong_identity":
        dependency.write_text("id: different-task\n")
    report = service.inspect()
    assert report["tasks"][source.stem]["status"] == expected, report
    service.reconcile()
    assert source.exists() and service.store.snapshot().get("maintenance", {}) == {}


def test_cycle_components_include_entire_cycle_and_handle_deep_graphs():
    graph = {str(i): [str(i + 1)] for i in range(5000)}
    graph["5000"] = ["4000"]
    assert set(cycle_components(graph)[0]) == {str(i) for i in range(4000, 5001)}
    assert cycle_components({"a": ["b", "c"], "b": ["a"], "c": ["a"]}) == [["a", "b", "c"]]


def test_blocked_graph_cycle_is_reported_and_never_resumed(tmp_path):
    service, source, dependency = setup(tmp_path)
    dependency.unlink()
    write(service.store.runtime, "feature_blocked", dependency.stem, dependencies=[f"verified/{source.name}"])
    report = service.inspect()
    assert report["cycles"] == [[source.stem, dependency.stem]]
    assert all(item["status"] == "cycle" for item in report["tasks"].values())
    service.reconcile()
    assert source.exists()


def test_active_run_and_pending_operation_block_new_maintenance_and_claims(tmp_path):
    service, source, _ = setup(tmp_path)
    config = parse_execution_config({"version": 1, "agents": {"fixture": {"transport": "acp", "argv": ["unused"],
        "adapter_version": "fixture", "harness_version": "fixture"}},
        "bindings": {"review": {"role": "ARCHITECT-REVIEWER", "agent": "fixture"}}},
        roles=set(service.schema.document["roles"]))
    task = TaskRevision.capture(service.store.runtime, source)
    claim = service.store.claim(task=task, config=config, schema=service.schema, binding=config.bindings[0],
                                project=tmp_path, owner_id="fixture-owner")
    service.reconcile()
    assert service.inspect()["tasks"][source.stem]["status"] == "active_run"
    service.store.transition(claim.run["id"], owner_id="fixture-owner", event_id="cancel", target="cancelled")
    def crash(stage):
        if stage == "prepared":
            raise RuntimeError("crash")
    service.checkpoint = crash
    with pytest.raises(RuntimeError):
        service.reconcile()
    with pytest.raises(GreatMindsError, match="unresolved maintenance"):
        service.store.claim(task=task, config=config, schema=service.schema, binding=config.bindings[0],
                            project=tmp_path, owner_id="another-owner")


def test_required_live_roles_are_not_inferred_from_configuration_alone(tmp_path):
    service, source, _ = setup(tmp_path)
    data = yaml.safe_load(source.read_text())
    data["requires_live_roles"] = ["DEVELOPER"]
    source.write_text(yaml.safe_dump(data))
    report = service.inspect()
    assert report["tasks"][source.stem]["status"] == "live_role_hold"
    assert report["tasks"][source.stem]["reasons"][0]["code"] == "live_context_unavailable"
    service.reconcile()
    assert source.exists()


def test_schema_must_explicitly_authorize_system_operations(tmp_path):
    service, source, _ = setup(tmp_path)
    document = service.schema.document
    document.pop("system_transitions")
    text = yaml.safe_dump(document)
    import hashlib
    schema = replace(service.schema, text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
    service = MaintenanceService(service.store, schema)
    assert service.inspect()["tasks"][source.stem]["status"] == "gate_failed"
    service.reconcile()
    assert source.exists()


def test_json_wake_check_uses_shared_findings_without_mutation(tmp_path):
    service, _, _ = setup(tmp_path)
    result = CliRunner().invoke(cli, ["wake-check", "--project-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == service.inspect()
    assert not service.store.path.exists()


def test_domain_resume_uses_terminal_dependencies_and_declared_destination(tmp_path):
    from greatminds.cli import task as policy
    service, source, dependency = setup(tmp_path)
    data = yaml.safe_load(source.read_text())
    with policy.domain_context(document=service.schema.document, runtime=service.store.runtime, workspace=tmp_path):
        assert policy._check_all_dependencies_exist(data, "feature_blocked", "feature_dev") is None
        assert "resume_to" in policy._check_all_dependencies_exist(data, "feature_blocked", "feature_review")
        target = service.store.runtime / "feature_dev" / dependency.name
        target.parent.mkdir()
        dependency.rename(target)
        assert "not ready" in policy._check_all_dependencies_exist(data, "feature_blocked", "feature_dev")


def test_recovery_uses_pinned_policy_and_does_not_reapply_a_reappearing_revision(tmp_path):
    service, source, _ = setup(tmp_path)
    original = source.read_bytes()
    def crash(stage):
        if stage == "prepared":
            raise RuntimeError("crash")
    service.checkpoint = crash
    with pytest.raises(RuntimeError):
        service.reconcile()
    doc = service.schema.document
    doc.pop("system_transitions")
    text = yaml.safe_dump(doc)
    import hashlib
    changed_schema = replace(service.schema, text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
    MaintenanceService(service.store, changed_schema).reconcile()
    assert not source.exists()
    target = service.store.runtime / "feature_dev" / source.name
    target.rename(source)
    assert source.read_bytes() == original
    original_service = MaintenanceService(service.store, service.schema)
    assert original_service.inspect()["tasks"][source.stem]["status"] == "gate_failed"
    original_service.reconcile()
    assert source.exists()
    assert len(service.store.snapshot()["maintenance"]) == 1


def test_live_role_gate_reports_unconfigured_idle_and_auth_states(tmp_path):
    service, source, _ = setup(tmp_path)
    data = yaml.safe_load(source.read_text())
    data["requires_live_roles"] = ["DEVELOPER"]
    source.write_text(yaml.safe_dump(data))
    config = {"version": 1, "agents": {"fixture": {"transport": "acp", "argv": ["unused"],
        "adapter_version": "fixture", "harness_version": "fixture"}}, "bindings": {}}
    path = tmp_path / "coordination" / "execution.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(config))
    def code():
        return service.inspect()["tasks"][source.stem]["reasons"][0]["code"]
    assert code() == "required_role_unconfigured"
    config["bindings"]["developer"] = {"role": "DEVELOPER", "agent": "fixture"}
    path.write_text(yaml.safe_dump(config))
    assert code() == "required_role_not_running"
    config["agents"]["fixture"]["required_env"] = ["MISSING_REQUIRED_KEY"]
    path.write_text(yaml.safe_dump(config))
    assert code() == "required_role_authentication"


def test_abandon_cannot_discard_a_move_that_already_happened(tmp_path):
    service, source, _ = setup(tmp_path)
    def crash(stage):
        if stage == "task_moved":
            raise RuntimeError("crash")
    service.checkpoint = crash
    with pytest.raises(RuntimeError):
        service.reconcile()
    operation = next(iter(service.store.snapshot()["maintenance"].values()))
    target = service.store.runtime / "feature_dev" / source.name
    original = target.read_bytes()
    target.write_text(target.read_text() + "description: changed after move\n")
    restarted = MaintenanceService(service.store, service.schema)
    restarted.reconcile()
    with pytest.raises(GreatMindsError, match="may already have moved"):
        restarted.abandon(operation["id"], reason="cannot throw away the moved task")
    target.write_bytes(original)
    restarted.request_repair(operation["id"])
    restarted.reconcile()
    assert service.store.snapshot()["maintenance"][operation["id"]]["status"] == "applied"
