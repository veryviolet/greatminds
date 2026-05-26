"""Tests for task 0233: stand deploy targets impl.base_commit.

Pre-0233 STAND-KEEPER deployed whatever main HEAD became between
``stand request`` filing and SK-run. When main moved (other tasks
merged), the stand tested a DIFFERENT commit than the requesting
task's impl described — gate_check would fail with the recurring
commit-drift class. Memory ``feedback_commit_drift_recovery``
documented the per-task refile workaround.

0233 closes the class systemically: ``stand request`` resolves
``evidence_for[0]``'s impl.base_commit and stores it as
``target_commit`` on the stand_request file. PROJECT.md's deploy
recipe reads ``target_commit`` and runs ``git checkout <sha>`` on
each host before deploy. Stand records that exact sha as
``stand_result.commit``; commit-identity holds by construction.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand as stand_mod


# ---------- schema pin ----------


def test_schema_has_stand_deploy_strategy_section() -> None:
    """0233 schema pin: ``stand.deploy_strategy`` carries the
    target_commit resolution + post-stand-revert toggle."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    stand = doc.get("stand") or {}
    ds = stand.get("deploy_strategy") or {}
    assert ds.get("target_commit_resolution") == (
        "evidence_for_impl_base_commit"
    )
    assert ds.get("post_stand_main_revert") is True


# ---------- _resolve_target_commit_from_evidence ----------


def _make_coord_with_task(tmp_path: Path, task_id: str,
                            queue: str, blocks: list) -> Path:
    """Build a minimal coordination tree with ``task_id`` in
    ``queue`` carrying ``blocks``. Returns the coord dir."""
    coord = tmp_path / "coordination"
    (coord / queue).mkdir(parents=True)
    (coord / queue / f"{task_id}.yaml").write_text(yaml.safe_dump({
        "id": task_id,
        "stream": "product",
        "kind": "feature",
        "scope": "backend",
        "title": "x",
        "reporter": "USER",
        "opened_at": "2026-05-26T00:00:00Z",
        "priority": "normal",
        "blocks": blocks,
    }), encoding="utf-8")
    return coord


def test_resolve_picks_latest_implementation_base_commit(
    tmp_path: Path,
) -> None:
    """Happy path: task has an impl block → return its base_commit."""
    coord = _make_coord_with_task(
        tmp_path, "0199-test-task", "feature_test",
        [
            {"kind": "plan", "base_commit": "plan_abc"},
            {"kind": "implementation", "base_commit": "impl_xyz"},
        ],
    )
    assert stand_mod._resolve_target_commit_from_evidence(
        coord, "0199-test-task",
    ) == "impl_xyz"


def test_resolve_picks_latest_impl_when_multiple(tmp_path: Path) -> None:
    """Iter-N case: multiple impl blocks → latest (refile) wins."""
    coord = _make_coord_with_task(
        tmp_path, "0199-test", "feature_test",
        [
            {"kind": "implementation", "base_commit": "iter1"},
            {"kind": "tests"},
            {"kind": "implementation", "base_commit": "iter2"},
        ],
    )
    assert stand_mod._resolve_target_commit_from_evidence(
        coord, "0199-test",
    ) == "iter2"


def test_resolve_falls_back_to_plan_when_no_impl(tmp_path: Path) -> None:
    """No impl block (e.g., docs task that skips impl) → use the
    plan block's base_commit. Important for docs/research stands."""
    coord = _make_coord_with_task(
        tmp_path, "0199-docs", "feature_docs_review",
        [{"kind": "plan", "base_commit": "plan_xyz"}],
    )
    assert stand_mod._resolve_target_commit_from_evidence(
        coord, "0199-docs",
    ) == "plan_xyz"


def test_resolve_returns_none_when_task_not_found(tmp_path: Path) -> None:
    coord = (tmp_path / "coordination")
    coord.mkdir()
    assert stand_mod._resolve_target_commit_from_evidence(
        coord, "0999-nonexistent",
    ) is None


def test_resolve_returns_none_when_no_base_commit_anywhere(
    tmp_path: Path,
) -> None:
    """Defensive: task exists but has no plan or impl block with
    base_commit → None. Caller leaves target_commit absent."""
    coord = _make_coord_with_task(
        tmp_path, "0199-bare", "feature_inbox",
        [{"kind": "triage"}],
    )
    assert stand_mod._resolve_target_commit_from_evidence(
        coord, "0199-bare",
    ) is None


# ---------- stand request CLI integration ----------


def test_stand_request_writes_target_commit_when_evidence_for(
    tmp_path: Path, monkeypatch,
) -> None:
    """0233 contract: ``greatminds stand request --evidence-for <id>``
    looks up that id's impl base_commit and stamps it onto the
    stand_request file's ``target_commit`` field."""
    from click.testing import CliRunner
    import subprocess

    # Build a project with a backend task carrying an impl block.
    project = tmp_path / "project"
    project.mkdir()
    coord = project / "coordination"
    coord.mkdir()
    for q in ("feature_test", "stand_requests", "intent"):
        (coord / q).mkdir()
    (coord / "feature_test" / "0199-test.yaml").write_text(yaml.safe_dump({
        "id": "0199-test",
        "stream": "product",
        "kind": "feature",
        "scope": "backend",
        "title": "x",
        "reporter": "USER",
        "opened_at": "2026-05-26T00:00:00Z",
        "priority": "normal",
        "blocks": [
            {"kind": "implementation", "base_commit": "abc123target"},
        ],
    }), encoding="utf-8")

    # Pretend we're TESTER in this project.
    monkeypatch.chdir(project)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")

    runner = CliRunner()
    result = runner.invoke(
        stand_mod.stand,
        ["request", "--request-type", "deploy",
         "--profile", "full-deploy",
         "--title", "verify 0199",
         "--hosts", "host-1",
         "--evidence-for", "0199-test"],
    )
    assert result.exit_code == 0, result.output

    requests = [
        f for f in (coord / "stand_requests").glob("*.yaml")
        if not f.name.startswith("_TEMPLATE")
    ]
    assert len(requests) == 1
    doc = yaml.safe_load(requests[0].read_text(encoding="utf-8"))
    assert doc.get("target_commit") == "abc123target", (
        "0233: stand_request must record evidence_for[0]'s "
        "implementation.base_commit as target_commit"
    )


def test_stand_request_no_target_commit_when_no_evidence_for(
    tmp_path: Path, monkeypatch,
) -> None:
    """Infra-only stand op (no evidence_for) → no target_commit field
    (no specific commit to target; SK deploys main HEAD)."""
    from click.testing import CliRunner

    project = tmp_path / "project"
    coord = project / "coordination"
    for q in ("stand_requests", "intent"):
        (coord / q).mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")

    runner = CliRunner()
    result = runner.invoke(
        stand_mod.stand,
        ["request", "--request-type", "smoke",
         "--profile", "full-deploy",
         "--title", "infra smoke",
         "--hosts", "host-1"],
    )
    assert result.exit_code == 0, result.output
    requests = [
        f for f in (coord / "stand_requests").glob("*.yaml")
        if not f.name.startswith("_TEMPLATE")
    ]
    doc = yaml.safe_load(requests[0].read_text(encoding="utf-8"))
    assert "target_commit" not in doc


def test_stand_request_target_commit_absent_when_evidence_task_has_no_impl(
    tmp_path: Path, monkeypatch,
) -> None:
    """Defensive: evidence_for task exists but has no impl/plan with
    base_commit → request file written without target_commit. SK
    falls back to main HEAD (existing behavior)."""
    from click.testing import CliRunner

    project = tmp_path / "project"
    coord = project / "coordination"
    for q in ("feature_inbox", "stand_requests", "intent"):
        (coord / q).mkdir(parents=True)
    (coord / "feature_inbox" / "0099-triage.yaml").write_text(
        yaml.safe_dump({
            "id": "0099-triage",
            "stream": "product",
            "kind": "feature",
            "scope": "backend",
            "title": "x",
            "reporter": "USER",
            "opened_at": "2026-05-26T00:00:00Z",
            "priority": "normal",
            "blocks": [{"kind": "triage"}],
        }), encoding="utf-8",
    )

    monkeypatch.chdir(project)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()
    result = runner.invoke(
        stand_mod.stand,
        ["request", "--request-type", "deploy",
         "--profile", "full-deploy",
         "--title", "no-impl case",
         "--hosts", "host-1",
         "--evidence-for", "0099-triage"],
    )
    assert result.exit_code == 0, result.output
    requests = [
        f for f in (coord / "stand_requests").glob("*.yaml")
        if not f.name.startswith("_TEMPLATE")
    ]
    doc = yaml.safe_load(requests[0].read_text(encoding="utf-8"))
    assert "target_commit" not in doc
