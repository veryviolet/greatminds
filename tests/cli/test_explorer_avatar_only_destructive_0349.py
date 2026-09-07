"""Explorer target safety reaches ACP context without a native profile."""
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.context import context_document
from greatminds.runtime.store import RunStore, TaskRevision


def test_explorer_context_preserves_stand_target_boundary(tmp_path):
    schema = load_schema_snapshot()
    config = parse_execution_config({
        "version": 1,
        "agents": {"agent": {"transport": "acp", "argv": ["unused-agent"],
                            "adapter_version": "test", "harness_version": "test"}},
        "bindings": {"explorer": {"agent": "agent", "role": "EXPLORER"}},
    }, roles=set(schema.document["roles"]))
    runtime = tmp_path / ".greatminds"
    task = runtime / "review_sessions/0001-review.yaml"
    task.parent.mkdir(parents=True)
    task.write_text("id: 0001-review\nstream: review_session\n")
    store = RunStore(runtime)
    claim = store.claim(task=TaskRevision.capture(runtime, task), binding=config.bindings[0],
                        config=config, schema=schema, project=tmp_path, owner_id="test")
    context = context_document(store, claim, schema)
    forbidden = " ".join(context["forbidden_actions"])
    assert "local orchestration host or an unresolved target" in forbidden
    assert "explicitly authorized disposable stand" in forbidden
    assert "verified target identity; otherwise submit a blocker" in forbidden
    assert "validate_off_stand_or_local_substitutes" in forbidden
    assert "operate_on_the_stand_as_real_user_whatever_its_shape" in context["responsibilities"]
    for verb in ("kill", "pkill", "systemctl", "logout", "reboot"):
        assert verb in forbidden
