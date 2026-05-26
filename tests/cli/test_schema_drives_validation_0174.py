"""Tests for task 0174: FSM single-source-of-truth.

Pre-0174 the FSM tables (which block kinds each stream accepts, which
roles author which block kind, which scope maps to which implementer
role, etc.) lived as hardcoded literal dicts at the top of
``cli/task.py``. The macro-structure was in ``schema.yaml`` but the
fine-grained validation tables were not. Adding a new stream / scope /
block_kind required code changes on top of schema changes.

0174 moves the DATA into ``schema.yaml`` under new sections
(``streams:``, ``block_kinds:``, ``queue_accepts_blocks:``,
``product_enums:``, ``stand_enums:``, ``assignee_role_by_scope:``).
``cli/task.py`` loads them at module-import. Validator FUNCTIONS
stay in code (registered by name in transitions[*].requires lists).
"""
from __future__ import annotations

import importlib

import pytest
import yaml

from greatminds.core.paths import find_canon_dir


# ---------- schema.yaml carries the new sections ----------


def _load_schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def test_schema_has_streams_section() -> None:
    """0174 + 0247 (1.3.0): streams now contains product +
    review_session only. stand stream removed alongside the
    three-queue model in 0247."""
    doc = _load_schema()
    streams = doc.get("streams")
    assert streams is not None, "0174: schema.yaml missing 'streams:' section"
    for required in ("product", "review_session"):
        assert required in streams, (
            f"0174: streams.{required} missing"
        )
        assert streams[required].get("allowed_block_kinds"), (
            f"0174: streams.{required}.allowed_block_kinds missing"
        )
    # 0247: stand stream explicitly removed.
    assert "stand" not in streams


def test_schema_has_block_kinds_section() -> None:
    """0174 + 0247: stand_result block kind dropped alongside the
    stand stream (lease-based release writes evidence on the
    product task's tests block instead)."""
    doc = _load_schema()
    bk = doc.get("block_kinds")
    assert bk is not None, "0174: schema.yaml missing 'block_kinds:' section"
    for required in ("triage", "plan", "implementation", "tests",
                     "reader_review", "review",
                     "session_iteration"):
        assert required in bk, f"0174: block_kinds.{required} missing"
        assert bk[required].get("authored_by"), (
            f"0174: block_kinds.{required}.authored_by missing"
        )
    # ``blocked`` deliberately omits authored_by (resolved at runtime
    # to the current owner of the queue).
    assert "blocked" in bk


def test_schema_has_queue_accepts_blocks() -> None:
    doc = _load_schema()
    q = doc.get("queue_accepts_blocks")
    assert q is not None
    # Spot-check the load-bearing entries.
    assert "implementation" in q["feature_dev"]
    assert "tests" in q["feature_test"]
    assert "review" in q["feature_review"]
    # 0195: verified now accepts `rollback` blocks (withdraw/revisit
    # marker for verified tasks reverted at the code level).
    assert q["verified"] == ["rollback"]
    assert q["archive"] == []
    # 0247 (1.3.0): stand_done queue removed entirely; should not
    # appear in queue_accepts_blocks any more.
    assert "stand_done" not in q
    assert "stand_requests" not in q
    assert "stand_wip" not in q


def test_schema_has_assignee_role_by_scope() -> None:
    doc = _load_schema()
    a = doc.get("assignee_role_by_scope")
    assert a is not None
    assert a["backend"] == "DEVELOPER"
    assert a["ui"] == "UI-DEVELOPER"
    assert a["docs"] == "TECHNICAL-WRITER"


def test_schema_has_product_enums() -> None:
    doc = _load_schema()
    pe = doc.get("product_enums")
    assert pe is not None
    for key in ("kinds", "scopes", "priorities", "plan_kinds", "modes"):
        assert pe.get(key), f"0174: product_enums.{key} missing"
    assert "backend" in pe["scopes"]
    assert "feature" in pe["kinds"]
    assert "A" in pe["modes"] and "B" in pe["modes"]


def test_schema_has_stand_enums() -> None:
    doc = _load_schema()
    se = doc.get("stand_enums")
    assert se is not None
    for key in ("request_types", "profiles", "results", "statuses"):
        assert se.get(key), f"0174: stand_enums.{key} missing"
    assert "full-deploy" in se["profiles"]
    assert "READY" in se["statuses"]


def test_block_kinds_tests_carries_result_enums() -> None:
    """0174 contract: per-block-kind enums live with the block_kind
    entry (not as flat module constants). Validators read them from
    there."""
    bk = _load_schema()["block_kinds"]
    assert "pass" in bk["tests"]["allowed_test_results"]
    assert "fail" in bk["tests"]["allowed_gate_check_results"]
    assert "approved" in bk["review"]["allowed_outcomes"]
    assert "pass" in bk["reader_review"]["allowed_outcomes"]


def test_block_kinds_stand_result_carries_enums() -> None:
    bk = _load_schema()["block_kinds"]
    assert "ok" in bk["stand_result"]["allowed_results"]
    assert "READY" in bk["stand_result"]["allowed_statuses"]


# ---------- cli/task.py constants come from schema ----------


def test_task_module_constants_match_schema_streams() -> None:
    """0174 wiring pin: STREAM_BLOCK_KINDS now derives from
    schema.streams[*].allowed_block_kinds, not a hardcoded literal."""
    from greatminds.cli import task as task_mod
    doc = _load_schema()
    expected = {
        name: set(meta["allowed_block_kinds"])
        for name, meta in doc["streams"].items()
    }
    assert task_mod.STREAM_BLOCK_KINDS == expected


def test_task_module_constants_match_schema_block_kind_roles() -> None:
    from greatminds.cli import task as task_mod
    doc = _load_schema()
    expected = {
        name: set(meta["authored_by"])
        for name, meta in doc["block_kinds"].items()
        if meta and meta.get("authored_by")
    }
    assert task_mod.BLOCK_KIND_ROLES == expected


def test_task_module_constants_match_schema_queue_blocks() -> None:
    from greatminds.cli import task as task_mod
    doc = _load_schema()
    expected = {q: set(blocks) for q, blocks in
                doc["queue_accepts_blocks"].items()}
    assert task_mod.QUEUE_BLOCK_KINDS == expected


def test_task_module_constants_match_schema_impl_role_by_scope() -> None:
    from greatminds.cli import task as task_mod
    doc = _load_schema()
    assert task_mod.IMPL_ROLE_BY_SCOPE == dict(doc["assignee_role_by_scope"])


def test_task_module_enums_match_schema() -> None:
    from greatminds.cli import task as task_mod
    doc = _load_schema()
    pe = doc["product_enums"]
    assert task_mod.PRODUCT_KINDS == set(pe["kinds"])
    assert task_mod.PRODUCT_SCOPES == set(pe["scopes"])
    assert task_mod.PRIORITIES == set(pe["priorities"])
    assert task_mod.PLAN_KINDS == set(pe["plan_kinds"])
    assert task_mod.MODES == set(pe["modes"])

    se = doc["stand_enums"]
    assert task_mod.STAND_REQUEST_TYPES == set(se["request_types"])
    assert task_mod.STAND_PROFILES == set(se["profiles"])
    assert task_mod.STAND_RESULTS == set(se["results"])
    assert task_mod.STAND_STATUSES == set(se["statuses"])


def test_task_module_test_review_enums_match_schema() -> None:
    from greatminds.cli import task as task_mod
    bk = _load_schema()["block_kinds"]
    assert task_mod.TEST_RESULTS == set(bk["tests"]["allowed_test_results"])
    assert task_mod.GATE_CHECK_RESULTS == set(
        bk["tests"]["allowed_gate_check_results"])
    assert task_mod.REVIEW_OUTCOMES == set(
        bk["review"]["allowed_outcomes"])
    assert task_mod.READER_OUTCOMES == set(
        bk["reader_review"]["allowed_outcomes"])


# ---------- regression: no literal data dicts left in task.py ----------


def test_no_hardcoded_fsm_constants_in_task_py() -> None:
    """0174 regression pin: cli/task.py must NOT carry the prior
    hardcoded literal dicts. They have been replaced with
    ``_load_fsm_tables_from_schema()`` calls.

    Detects the prior literal blocks: ``STREAM_BLOCK_KINDS: dict[...]\n
    = {`` etc. The new form is ``STREAM_BLOCK_KINDS:  dict[...] =
    _FSM[...]`` which doesn't contain a ``{`` on the assignment line.
    """
    import inspect
    from greatminds.cli import task as task_mod
    source = inspect.getsource(task_mod)
    # Patterns that should NOT appear (they were the literal-dict
    # right-hand sides of the prior hardcoded constants).
    forbidden = [
        ("STREAM_BLOCK_KINDS", '"product": {'),
        ("BLOCK_KIND_ROLES", '"triage":'),
        ("QUEUE_BLOCK_KINDS", '"feature_inbox":         {"triage"'),
        ("PRODUCT_KINDS = {",  '"feature", "bugfix", "docs"'),
    ]
    for name, marker in forbidden:
        # The marker must NOT appear inline with the constant name
        # being assigned (the new form uses _FSM[...] lookup).
        # We accept the marker appearing in unrelated places (none
        # currently, but defensive).
        idx = source.find(f"{name}")
        if idx < 0:
            continue
        nearby = source[idx:idx + 400]
        assert marker not in nearby, (
            f"0174 regression: {name} still has hardcoded literal "
            f"definition in cli/task.py; should load from schema."
        )


# ---------- schema additions are extensible: adding a value flows in ----------


def test_schema_extension_flows_into_loaded_constant(tmp_path, monkeypatch):
    """0174 contract: extend ``product_enums.scopes`` in schema → the
    loaded ``PRODUCT_SCOPES`` set reflects the addition without any
    code change. Tests the live load path via the public
    ``_load_fsm_tables_from_schema`` helper with a custom canon.
    """
    from greatminds.cli import task as task_mod
    # Build a minimal canon dir with the doc augmented.
    canon = tmp_path / "canon"
    canon.mkdir()
    src_schema = find_canon_dir() / "schema.yaml"
    doc = yaml.safe_load(src_schema.read_text(encoding="utf-8")) or {}
    doc["product_enums"]["scopes"] = list(doc["product_enums"]["scopes"]) + [
        "newscope-0174-test"]
    (canon / "schema.yaml").write_text(
        yaml.safe_dump(doc), encoding="utf-8",
    )
    monkeypatch.setattr(task_mod, "find_canon_dir", lambda: canon)
    monkeypatch.setattr(task_mod, "_schema_cache", None)
    rebuilt = task_mod._load_fsm_tables_from_schema()
    assert "newscope-0174-test" in rebuilt["PRODUCT_SCOPES"]


def test_schema_missing_streams_section_raises():
    """If schema.yaml lacks ``streams:`` at module-import time,
    GreatMindsError fires immediately — no silent fallback."""
    from greatminds.cli import task as task_mod
    original_schema = task_mod._schema_cache
    try:
        task_mod._schema_cache = {"version": 1}  # no streams/block_kinds
        with pytest.raises(task_mod.GreatMindsError) as exc:
            task_mod._load_fsm_tables_from_schema()
        assert "streams" in str(exc.value) or "block_kinds" in str(exc.value)
    finally:
        task_mod._schema_cache = original_schema
