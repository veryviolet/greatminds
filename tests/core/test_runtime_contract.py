"""Persistent execution contract behavior; no harness or inference required."""

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.store import ResultEnvelope, RunStore, TaskRevision


def config_document():
    return {
        "version": 1,
        "agents": {"test-acp": {
            "transport": "acp", "argv": ["test-acp", "--stdio"],
            "adapter_version": "1.0.0", "harness_version": "1.0.0",
            "environment": {"API_KEY": "TEST_API_KEY"}, "required_env": ["TEST_API_KEY"],
        }},
        "bindings": {"developer": {"role": "DEVELOPER", "agent": "test-acp", "scheduling": "queue"}},
    }


def contract():
    schema = load_schema_snapshot()
    config = parse_execution_config(config_document(), roles=set(schema.document["roles"]))
    return schema, config


def claim_at(runtime: Path):
    schema, config = contract()
    task = TaskRevision.capture(runtime, runtime / "feature_dev" / "0001-example.yaml")
    return RunStore(runtime).claim(task=task, binding=config.bindings[0], config=config,
                                   schema=schema, project=runtime.parent, owner_id="test-supervisor")


def compete(runtime: str):
    try:
        return claim_at(Path(runtime)).run["id"]
    except GreatMindsError as exc:
        return str(exc)


@pytest.fixture
def runtime(tmp_path):
    runtime = tmp_path / ".greatminds"
    task = runtime / "feature_dev" / "0001-example.yaml"
    task.parent.mkdir(parents=True)
    task.write_text("title: Example\n")
    return runtime


def running(runtime):
    claim = claim_at(runtime)
    store = RunStore(runtime)
    store.transition(claim.run["id"], owner_id="test-supervisor", event_id="start", target="starting")
    store.transition(claim.run["id"], owner_id="test-supervisor", event_id="ready", target="running")
    envelope = ResultEnvelope("result-one", claim.run["id"], claim.run["task_id"],
                              claim.run["task_revision"], claim.run["schema_sha256"],
                              "handoff", {"to_queue": "done", "artifacts": []})
    return store, claim, envelope


def test_claim_is_atomic_across_processes(runtime):
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(compete, [str(runtime)] * 8))
    assert sum("already has an active run" in result for result in results) == 7
    state = RunStore(runtime).snapshot()
    assert len(state["runs"]) == 1
    assert len(state["events"]) == 1


def test_compact_decision_gets_stable_identity_and_duplicate_delivery(runtime):
    store, claim, _ = running(runtime)
    document = {"decision": "no_change", "payload": {"reason": "needs further analysis"}}
    receipt = store.submit_decision(document, run_id=claim.run["id"], token=claim.token)
    assert document == {"decision": "no_change", "payload": {"reason": "needs further analysis"}}
    envelope = receipt["envelope"]
    assert envelope["result_id"] == claim.run["id"] + "-result"
    for name in ("task_id", "task_revision", "schema_sha256"):
        assert envelope[name] == claim.run[name]
    store.transition(claim.run["id"], owner_id="test-supervisor", event_id="finished", target="completed")
    assert store.submit_decision(document, run_id=claim.run["id"], token=claim.token) == receipt
    with pytest.raises(GreatMindsError, match="different contents"):
        store.submit_decision({"decision": "no_change", "payload": {}},
                              run_id=claim.run["id"], token=claim.token)


@pytest.mark.parametrize("field", ["run_id", "task_id", "task_revision", "schema_sha256"])
def test_compact_decision_cannot_override_assignment(runtime, field):
    store, claim, _ = running(runtime)
    with pytest.raises(GreatMindsError, match="does not match"):
        store.submit_decision({"decision": "no_change", "payload": {}, field: "other"},
                              run_id=claim.run["id"], token=claim.token)
    assert store.snapshot()["results"] == {}


def test_compact_decision_still_requires_the_run_credential(runtime):
    store, claim, _ = running(runtime)
    with pytest.raises(GreatMindsError, match="credential"):
        store.submit_decision({"decision": "no_change", "payload": {}},
                              run_id=claim.run["id"], token="wrong")
    assert store.snapshot()["results"] == {}


def test_read_only_snapshot_does_not_initialize_runtime(tmp_path):
    assert RunStore(tmp_path).snapshot()["project_id"] is None
    assert not (tmp_path / ".runtime").exists()


def test_claim_pins_identity_and_keeps_credentials_out_of_snapshot(runtime):
    claim = claim_at(runtime)
    state = RunStore(runtime).snapshot()
    run = state["runs"][claim.run["id"]]
    assert run == claim.run
    assert run["role"] == "DEVELOPER"
    assert run["schema_sha256"] == contract()[0].sha256
    assert run["project_id"] == state["project_id"]
    assert "token_sha256" not in run
    assert claim.token not in (runtime / ".runtime" / "state.json").read_text()


def test_role_cannot_claim_another_roles_queue(runtime):
    schema, config = contract()
    path = runtime / "feature_review" / "0002-review.yaml"
    path.parent.mkdir()
    path.write_text("title: Review\n")
    with pytest.raises(GreatMindsError, match="cannot claim"):
        RunStore(runtime).claim(task=TaskRevision.capture(runtime, path), binding=config.bindings[0],
                                config=config, schema=schema, project=runtime.parent, owner_id="owner")


def test_run_can_read_pinned_contract_after_source_changes(runtime, monkeypatch):
    claim = claim_at(runtime)
    store = RunStore(runtime)
    schema = contract()[0]
    monkeypatch.setenv("GREATMINDS_CANON_DIR", str(runtime / "missing-canon"))
    pinned = store.contracts(claim.run["id"])
    assert pinned["schema"]["text"] == schema.text
    assert pinned["execution"]["bindings"][0]["role"] == "DEVELOPER"
    path = store.directory / "contracts" / f"schema-{schema.sha256}.json"
    path.write_text('{"text": "tampered"}')
    with pytest.raises(GreatMindsError, match="pinned contract"):
        store.contracts(claim.run["id"])


@pytest.mark.parametrize("limit_type", ["project", "binding", "account"])
def test_capacity_is_checked_before_publishing_claim(runtime, limit_type):
    schema, config = contract()
    if limit_type == "project":
        config = replace(config, max_running=1)
    elif limit_type == "account":
        config = replace(config, account_limits=(("default", 1),),
                         bindings=(replace(config.bindings[0], max_running=2),))
    store = RunStore(runtime)
    binding = config.bindings[0]
    for number in (1, 2):
        path = runtime / "feature_dev" / f"{number:04d}-capacity.yaml"
        path.write_text("title: Capacity\n")
    def claim(number):
        return store.claim(task=TaskRevision.capture(runtime, runtime / "feature_dev" / f"{number:04d}-capacity.yaml"),
                           binding=binding, config=config, schema=schema,
                           project=runtime.parent, owner_id="owner")
    claim(1)
    before = store.path.read_bytes()
    with pytest.raises(GreatMindsError, match=f"{limit_type} .*capacity reached"):
        claim(2)
    assert store.path.read_bytes() == before


def test_failed_atomic_replace_leaves_claim_retryable(runtime, monkeypatch):
    import greatminds.core.storage as storage
    original = storage.os.replace
    def fail_state(source, destination):
        if Path(destination).name == "state.json":
            raise OSError("injected write failure")
        return original(source, destination)
    monkeypatch.setattr(storage.os, "replace", fail_state)
    with pytest.raises(OSError, match="injected"):
        claim_at(runtime)
    assert RunStore(runtime).snapshot()["runs"] == {}
    monkeypatch.setattr(storage.os, "replace", original)
    assert claim_at(runtime).run["state"] == "claimed"


def test_duplicate_event_after_restart_is_noop_and_conflict_is_rejected(runtime):
    claim = claim_at(runtime)
    args = dict(owner_id="test-supervisor", event_id="start", target="starting")
    RunStore(runtime).transition(claim.run["id"], **args)
    before = (runtime / ".runtime" / "state.json").read_bytes()
    RunStore(runtime).transition(claim.run["id"], **args)
    assert (runtime / ".runtime" / "state.json").read_bytes() == before
    with pytest.raises(GreatMindsError, match="reused"):
        RunStore(runtime).transition(claim.run["id"], **{**args, "target": "running"})


def test_pause_blocks_new_work_but_allows_active_run_to_finish(runtime):
    store, claim, _ = running(runtime)
    store.set_paused(True)
    store.transition(claim.run["id"], owner_id="test-supervisor", event_id="stop", target="completed")
    with pytest.raises(GreatMindsError, match="paused"):
        claim_at(runtime)
    store.set_paused(False)
    assert claim_at(runtime).run["id"] != claim.run["id"]


def test_terminal_run_rejects_late_nonduplicate_events(runtime):
    store, claim, _ = running(runtime)
    store.transition(claim.run["id"], owner_id="test-supervisor", event_id="stop", target="completed")
    with pytest.raises(GreatMindsError, match="illegal run transition"):
        store.transition(claim.run["id"], owner_id="test-supervisor", event_id="late", target="running")


def test_result_receipt_is_idempotent_even_after_task_moves(runtime):
    store, claim, envelope = running(runtime)
    receipt = store.receive_result(envelope, token=claim.token)
    assert receipt["status"] == "received"  # Domain approval is not implied.
    task = runtime / "feature_dev" / "0001-example.yaml"
    (runtime / "done").mkdir()
    task.rename(runtime / "done" / task.name)
    assert RunStore(runtime).receive_result(envelope, token=claim.token) == receipt
    assert len(store.snapshot()["results"]) == 1
    assert store.snapshot()["runs"][claim.run["id"]]["state"] == "running"
    with pytest.raises(GreatMindsError, match="reused"):
        store.receive_result(replace(envelope, decision="no_change"), token=claim.token)


@pytest.mark.parametrize("mutation", ["contents", "queue"])
def test_stale_result_rejected_without_writes(runtime, mutation):
    store, claim, envelope = running(runtime)
    task = runtime / "feature_dev" / "0001-example.yaml"
    if mutation == "contents":
        task.write_text("title: Changed\n")
    else:
        (runtime / "done").mkdir()
        task.rename(runtime / "done" / task.name)
    before = store.path.read_bytes()
    with pytest.raises(GreatMindsError, match="stale task revision"):
        store.receive_result(envelope, token=claim.token)
    assert store.path.read_bytes() == before


def test_wrong_run_token_role_and_revision_are_rejected(runtime):
    store, claim, envelope = running(runtime)
    with pytest.raises(GreatMindsError, match="credential"):
        store.receive_result(envelope, token="wrong")
    with pytest.raises(GreatMindsError, match="override run identity"):
        store.receive_result(replace(envelope, payload={"role": "REVIEWER"}), token=claim.token)
    with pytest.raises(GreatMindsError, match="does not match"):
        store.receive_result(replace(envelope, task_revision="wrong"), token=claim.token)
    with pytest.raises(GreatMindsError, match="different supervisor"):
        store.transition(claim.run["id"], owner_id="wrong", event_id="stop", target="completed")
    assert store.snapshot()["results"] == {}


def test_corrupt_state_is_not_silently_reinitialized(runtime):
    claim_at(runtime)
    store = RunStore(runtime)
    store.path.write_text("{truncated")
    with pytest.raises(GreatMindsError, match="cannot read runtime state"):
        store.set_paused(True)
    assert store.path.read_text() == "{truncated"


def test_environment_is_resolved_only_at_launch():
    _, config = contract()
    agent = config.agents[0]
    before = agent.sha256
    assert agent.environment_values({"TEST_API_KEY": "secret"}) == {"API_KEY": "secret"}
    assert agent.sha256 == before
    assert "secret" not in repr(config)
    with pytest.raises(GreatMindsError, match="TEST_API_KEY"):
        agent.environment_values({})


def test_execution_cli_exposes_validated_config_without_environment_values(tmp_path, monkeypatch):
    import json
    import yaml
    from click.testing import CliRunner
    from greatminds.cli.main import cli

    source = tmp_path / "execution.yaml"
    source.write_text(yaml.safe_dump(config_document()))
    monkeypatch.setenv("TEST_API_KEY", "do-not-serialize-this-value")
    result = CliRunner().invoke(cli, ["project", "execution", "--config", str(source)])
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["sha256"] == contract()[1].sha256
    assert document["execution"]["agents"][0]["argv"] == ["test-acp", "--stdio"]
    assert "do-not-serialize-this-value" not in result.output
    assert not (tmp_path / ".runtime").exists()


@pytest.mark.parametrize("field,value", [("transport", "subprocess"), ("argv", "agent --acp"),
                                         ("argv", []), ("mispelled", True),
                                         ("environment", {"KEY": "literal-secret-value"})])
def test_invalid_manifest_fails_before_launch(field, value):
    document = config_document()
    document["agents"]["test-acp"][field] = value
    with pytest.raises(GreatMindsError):
        parse_execution_config(document, roles={"DEVELOPER"})


@pytest.mark.parametrize("field,value", [("role", "IMPOSTOR"), ("agent", "absent"),
                                         ("max_running", True), ("permission", "yolo"),
                                         ("scheduling", "unknown")])
def test_invalid_binding_fails_before_launch(field, value):
    document = config_document()
    document["bindings"]["developer"][field] = value
    with pytest.raises(GreatMindsError):
        parse_execution_config(document, roles={"DEVELOPER"})
