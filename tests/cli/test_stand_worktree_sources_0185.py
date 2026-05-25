"""Tests for 0185 iter-2/3: ``greatminds stand request --evidence-for
<task-id>`` embeds the per-task worktree path into the request file
under ``worktree_sources``.

REVIEWER's iter-1 bounce (info-1779746111) made the timing concrete:
stand requests fire at feature_test (TESTER), BEFORE REVIEWER merges
the worktree branch into main. So sourcing the rsync from main HEAD
would verify stale code that doesn't yet carry the task's fix. The
request must record where the per-task tree lives so SK's rsync
wrapper picks the right source.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.cli import worktree as wt_mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """Minimal project: git repo + coordination/ + a fake task file in
    feature_test (so task_exists_in_active passes) + a worktree."""
    p = tmp_path / "project"
    p.mkdir()
    _git(["init", "-b", "main"], p)
    _git(["config", "user.email", "test@example.com"], p)
    _git(["config", "user.name", "Test"], p)
    (p / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "README.md"], p)
    _git(["commit", "-m", "initial"], p)

    coord = p / "coordination"
    for q in ("feature_test", "stand_requests", "intent"):
        (coord / q).mkdir(parents=True)
    # Fake task in feature_test (the evidence_for target).
    (coord / "feature_test" / "0185-test.yaml").write_text(
        "id: 0185-test\nstream: product\nkind: feature\nscope: backend\n"
        "title: t\nreporter: USER\nopened_at: '2026-05-26T00:00:00Z'\n"
        "priority: normal\n", encoding="utf-8",
    )
    # Worktree for that task — this is what SK should rsync from.
    wt_mod.worktree_create(p, "0185-test", base="main")

    # Make the CLI find this project as cwd.
    monkeypatch.chdir(p)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    return p


def test_stand_request_embeds_worktree_source_for_evidence_for_task(
    project: Path,
) -> None:
    """0185: ``stand request --evidence-for <id>`` writes
    ``worktree_sources: {<id>: <path>}`` to the request file."""
    runner = CliRunner()
    result = runner.invoke(
        stand_mod.stand,
        ["request", "--request-type", "deploy",
         "--profile", "full-deploy",
         "--title", "verify 0185-test",
         "--hosts", "host-1",
         "--evidence-for", "0185-test"],
    )
    assert result.exit_code == 0, result.output

    requests = list((project / "coordination" / "stand_requests").glob("*.yaml"))
    requests = [r for r in requests if not r.name.startswith("_TEMPLATE")]
    assert len(requests) == 1
    doc = yaml.safe_load(requests[0].read_text(encoding="utf-8"))
    assert "worktree_sources" in doc, (
        "0185: stand_request must carry worktree_sources for "
        "evidence_for tasks so SK rsyncs from the per-task tree"
    )
    assert "0185-test" in doc["worktree_sources"]
    assert ".worktrees/0185-test" in doc["worktree_sources"]["0185-test"]


def test_stand_request_no_worktree_sources_for_infra_only_request(
    project: Path,
) -> None:
    """Infra-only stand op (no evidence_for) → no worktree_sources
    field. SK rsyncs the main project tree as today."""
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
        r for r in (project / "coordination" / "stand_requests").glob("*.yaml")
        if not r.name.startswith("_TEMPLATE")
    ]
    assert len(requests) == 1
    doc = yaml.safe_load(requests[0].read_text(encoding="utf-8"))
    assert "worktree_sources" not in doc


def test_stand_request_skips_missing_worktrees_silently(
    tmp_path: Path, monkeypatch,
) -> None:
    """Defensive: evidence_for task exists but has no worktree (kind
    not in required_for_task_kinds, e.g. ``research``) → request is
    created without worktree_sources. SK falls back to main tree."""
    p = tmp_path / "project"
    p.mkdir()
    _git(["init", "-b", "main"], p)
    _git(["config", "user.email", "x@y"], p)
    _git(["config", "user.name", "x"], p)
    (p / "f").write_text("x", encoding="utf-8")
    _git(["add", "f"], p)
    _git(["commit", "-m", "i"], p)

    coord = p / "coordination"
    for q in ("feature_test", "stand_requests", "intent"):
        (coord / q).mkdir(parents=True)
    (coord / "feature_test" / "0099-research.yaml").write_text(
        "id: 0099-research\nstream: product\nkind: research\nscope: research\n"
        "title: t\nreporter: USER\nopened_at: '2026-05-26T00:00:00Z'\n"
        "priority: normal\n", encoding="utf-8",
    )

    monkeypatch.chdir(p)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()
    result = runner.invoke(
        stand_mod.stand,
        ["request", "--request-type", "deploy",
         "--profile", "full-deploy",
         "--title", "no worktree case",
         "--hosts", "host-1",
         "--evidence-for", "0099-research"],
    )
    assert result.exit_code == 0, result.output
    requests = [
        r for r in (coord / "stand_requests").glob("*.yaml")
        if not r.name.startswith("_TEMPLATE")
    ]
    doc = yaml.safe_load(requests[0].read_text(encoding="utf-8"))
    # No worktree was created for 0099-research → no embedded source.
    assert "worktree_sources" not in doc
