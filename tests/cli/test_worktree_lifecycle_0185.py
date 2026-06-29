"""Tests for task 0185: per-task git worktree lifecycle.

Replaces the 0115/0166 file-lock band-aid. Each task gets its own
working tree on a ``task/<task-id>`` branch. The CLI exposes
``greatminds worktree {create|remove|merge|list|prune}``.

These tests exercise the helpers directly with a real ephemeral git
repo at ``tmp_path``. They avoid the production /opt/greatminds repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from greatminds.cli import worktree as wt_mod
from greatminds.core.errors import GreatMindsError


# ---------- fixtures ----------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Bare-bones git repo with an initial commit on ``main``."""
    project = tmp_path / "project"
    project.mkdir()
    _git(["init", "-b", "main"], project)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "user.name", "Test"], project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "README.md"], project)
    _git(["commit", "-m", "initial"], project)
    return project


# ---------- WorktreePolicy ----------


def test_policy_defaults() -> None:
    """Without schema.yaml the loader returns the plan defaults so
    setup doesn't crash on greenfield projects."""
    p = wt_mod.WorktreePolicy()
    assert p.base_path == ".worktrees"
    assert p.branch_prefix == "task/"
    assert p.merge_strategy == "--no-ff"
    assert p.cleanup_on_archive is True
    assert p.required_for_task_kinds == ("feature", "bugfix", "ops")


def test_policy_loads_from_canon_schema() -> None:
    """0185 contract: production schema.yaml MUST carry the
    ``worktrees:`` section so the helper picks up policy."""
    p = wt_mod.load_worktree_policy()
    assert p.base_path == ".worktrees"
    assert p.branch_prefix == "task/"
    assert "feature" in p.required_for_task_kinds


def test_policy_branch_and_path_derivation() -> None:
    p = wt_mod.WorktreePolicy(base_path=".wt", branch_prefix="t/")
    assert p.branch_for("0123-foo") == "t/0123-foo"
    assert p.worktree_path_for(Path("/tmp/proj"), "0123-foo") == (
        Path("/tmp/proj/.wt/0123-foo")
    )


# ---------- worktree_create ----------


def test_create_makes_worktree_and_branch(repo: Path) -> None:
    path = wt_mod.worktree_create(repo, "0185-test", base="main")
    assert path.exists()
    assert (path / ".git").exists()
    # Branch exists.
    cp = subprocess.run(
        ["git", "branch", "--list", "task/0185-test"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert "task/0185-test" in cp.stdout


def test_create_is_idempotent(repo: Path) -> None:
    """Calling create twice for the same task is a no-op on the second
    call — important because mv-to-implementer-queue invokes it but
    the agent may also do so directly."""
    p1 = wt_mod.worktree_create(repo, "0185-test", base="main")
    p2 = wt_mod.worktree_create(repo, "0185-test", base="main")
    assert p1 == p2
    assert p1.exists()


def test_create_uses_explicit_base_when_given(repo: Path) -> None:
    """The --base arg overrides any plan.base_commit lookup."""
    (repo / "f1.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "f1.txt"], repo)
    _git(["commit", "-m", "c1"], repo)
    main_sha = _git(["rev-parse", "main~1"], repo).stdout.strip()
    wt_mod.worktree_create(repo, "0185-test", base=main_sha)
    # The new branch should point at main_sha, not current main HEAD.
    cp = _git(["rev-parse", "task/0185-test"], repo)
    assert cp.stdout.strip() == main_sha


def test_create_resolves_base_from_plan_block(repo: Path,
                                                tmp_path: Path) -> None:
    """No explicit --base + plan block carries base_commit → uses it."""
    # Build a fake coordination/feature_dev/<task>.yaml with a plan
    # block carrying base_commit.
    main_sha = _git(["rev-parse", "main"], repo).stdout.strip()
    coord = repo / "coordination" / "feature_dev"
    coord.mkdir(parents=True)
    task_doc = {
        "id": "0185-test",
        "blocks": [
            {"kind": "plan", "base_commit": main_sha},
        ],
    }
    (coord / "0185-test.yaml").write_text(
        yaml.safe_dump(task_doc), encoding="utf-8",
    )
    wt_mod.worktree_create(repo, "0185-test", base=None)
    cp = _git(["rev-parse", "task/0185-test"], repo)
    assert cp.stdout.strip() == main_sha


def test_create_falls_back_to_main_head_when_no_plan(repo: Path) -> None:
    """Last-resort fallback: no plan block, no --base → main HEAD.
    Without this, fresh-toy bootstraps would crash on the first
    auto-create from mv."""
    main_sha = _git(["rev-parse", "main"], repo).stdout.strip()
    wt_mod.worktree_create(repo, "0185-orphan", base=None)
    cp = _git(["rev-parse", "task/0185-orphan"], repo)
    assert cp.stdout.strip() == main_sha


# ---------- worktree_refresh ----------


def test_refresh_merges_current_main_and_preserves_overlay(repo: Path) -> None:
    """An old task worktree can pick up infra/profile fixes committed to
    default_branch after it was created, without losing uncommitted work."""
    wt_path = wt_mod.worktree_create(repo, "0185-refresh", base="main")
    (repo / "profile.yaml").write_text("fixed-profile\n", encoding="utf-8")
    _git(["add", "profile.yaml"], repo)
    _git(["commit", "-m", "fix profile"], repo)
    (wt_path / "feature.txt").write_text("uncommitted overlay\n",
                                         encoding="utf-8")

    result = wt_mod.worktree_refresh(repo, "0185-refresh")

    assert result.ok is True
    assert result.changed is True
    assert (wt_path / "profile.yaml").read_text(
        encoding="utf-8") == "fixed-profile\n"
    assert (wt_path / "feature.txt").read_text(
        encoding="utf-8") == "uncommitted overlay\n"
    status = _git(["status", "--porcelain"], wt_path).stdout
    assert "feature.txt" in status


def test_cli_refresh_reports_success(repo: Path) -> None:
    from click.testing import CliRunner
    wt_mod.worktree_create(repo, "0185-refresh-cli", base="main")
    (repo / "base.txt").write_text("new\n", encoding="utf-8")
    _git(["add", "base.txt"], repo)
    _git(["commit", "-m", "advance main"], repo)

    res = CliRunner().invoke(
        wt_mod.worktree,
        ["refresh", "0185-refresh-cli", "--project-dir", str(repo)],
    )

    assert res.exit_code == 0, res.output
    assert "refreshed" in res.output


# ---------- worktree_remove ----------


def test_remove_drops_worktree_and_branch(repo: Path) -> None:
    wt_mod.worktree_create(repo, "0185-test", base="main")
    assert wt_mod.worktree_remove(repo, "0185-test") is True
    assert not (repo / ".worktrees" / "0185-test").exists()
    cp = subprocess.run(
        ["git", "branch", "--list", "task/0185-test"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert "task/0185-test" not in cp.stdout


def test_remove_missing_is_noop(repo: Path) -> None:
    """Removing a non-existent worktree returns False (signal: nothing
    to do), no crash."""
    assert wt_mod.worktree_remove(repo, "0185-never-existed") is False


# ---------- worktree_merge ----------


def test_merge_clean_into_main(repo: Path) -> None:
    """Happy path: task branch with no conflicts merges --no-ff into
    main. Resulting main HEAD must be a merge commit (2 parents)."""
    wt_mod.worktree_create(repo, "0185-test", base="main")
    wt_dir = repo / ".worktrees" / "0185-test"
    (wt_dir / "feature.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "feature.txt"], wt_dir)
    _git(["commit", "-m", "feat"], wt_dir)

    result = wt_mod.worktree_merge(repo, "0185-test", summary="feat: 0185-test")
    assert result.ok is True
    assert result.conflicts == ()
    # --no-ff guarantees a merge commit; main HEAD has 2 parents.
    parents = _git(["log", "-1", "--format=%P"], repo).stdout.strip().split()
    assert len(parents) == 2


def test_merge_conflict_returns_conflict_list_and_aborts(repo: Path) -> None:
    """Conflicting branches: merge fails, conflicts listed, main is
    restored (no half-merged state)."""
    # Both main and task branch edit the same file.
    wt_mod.worktree_create(repo, "0185-test", base="main")
    wt_dir = repo / ".worktrees" / "0185-test"
    (wt_dir / "conflict.txt").write_text("branch-version\n", encoding="utf-8")
    _git(["add", "conflict.txt"], wt_dir)
    _git(["commit", "-m", "task edits"], wt_dir)
    (repo / "conflict.txt").write_text("main-version\n", encoding="utf-8")
    _git(["add", "conflict.txt"], repo)
    _git(["commit", "-m", "main edits"], repo)

    result = wt_mod.worktree_merge(repo, "0185-test")
    assert result.ok is False
    assert "conflict.txt" in result.conflicts
    # Main must be intact (merge aborted, no MERGE_HEAD).
    assert not (repo / ".git" / "MERGE_HEAD").exists()


# ---------- worktree_list + prune ----------


def test_list_reports_active_worktrees(repo: Path) -> None:
    wt_mod.worktree_create(repo, "0185-a", base="main")
    wt_mod.worktree_create(repo, "0185-b", base="main")
    entries = wt_mod.worktree_list(repo)
    paths = {e.get("path") for e in entries}
    # Includes the main repo itself + the two added worktrees.
    assert any("0185-a" in p for p in paths if p)
    assert any("0185-b" in p for p in paths if p)


def test_prune_removes_orphans_only(repo: Path) -> None:
    """Prune respects the active-task-ids guard — worktrees whose id
    is still in an active queue must NOT be pruned (otherwise the
    watchdog would yank work out from under an agent)."""
    wt_mod.worktree_create(repo, "0185-active", base="main")
    wt_mod.worktree_create(repo, "0185-orphan", base="main")

    pruned = wt_mod.worktree_prune(repo, active_task_ids={"0185-active"})
    assert pruned == ["0185-orphan"]
    assert (repo / ".worktrees" / "0185-active").exists()
    assert not (repo / ".worktrees" / "0185-orphan").exists()


def test_prune_with_empty_active_set_removes_all(repo: Path) -> None:
    """When every task is in verified/archive (terminal queues are
    excluded from the active set), all worktrees are eligible."""
    wt_mod.worktree_create(repo, "0185-a", base="main")
    wt_mod.worktree_create(repo, "0185-b", base="main")
    pruned = sorted(wt_mod.worktree_prune(repo, active_task_ids=set()))
    assert pruned == ["0185-a", "0185-b"]


# ---------- worktree path + assert-drained subcommands ----------


def test_cli_path_prints_task_worktree_path(repo: Path) -> None:
    """0185 follow-up: ``greatminds worktree path <task-id>`` prints
    the resolved worktree path. Implementers + STAND-KEEPER use this
    as the self-contained substitute for $GREATMINDS_TASK_WORKTREE."""
    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(
        wt_mod.worktree, ["path", "0185-test",
                          "--project-dir", str(repo)],
    )
    assert result.exit_code == 0
    expected = wt_mod.WorktreePolicy().worktree_path_for(repo, "0185-test")
    assert str(expected) in result.output


def test_cli_assert_drained_passes_when_pipeline_empty(repo: Path) -> None:
    """0185 cutover gate: drain-to-zero passes when no feature_*
    queue holds any task file."""
    from click.testing import CliRunner
    (repo / "coordination").mkdir()
    for q in ("feature_dev", "feature_test", "feature_review",
              "feature_blocked"):
        (repo / "coordination" / q).mkdir()
    runner = CliRunner()
    result = runner.invoke(
        wt_mod.worktree, ["assert-drained",
                          "--project-dir", str(repo)],
    )
    assert result.exit_code == 0, result.output


def test_cli_assert_drained_fails_when_in_flight(repo: Path) -> None:
    """Refuses cutover with the in-flight queue listed in stderr,
    exit code 3 (matches the plan's handback convention)."""
    from click.testing import CliRunner
    (repo / "coordination" / "feature_dev").mkdir(parents=True)
    (repo / "coordination" / "feature_dev" /
     "0099-blocking.yaml").write_text("id: 0099\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        wt_mod.worktree, ["assert-drained",
                          "--project-dir", str(repo)],
    )
    assert result.exit_code == 3
    assert "feature_dev" in result.output
    assert "0099-blocking.yaml" in result.output


# ---------- schema.yaml carries worktrees: section ----------


def test_schema_has_worktrees_section() -> None:
    """0185 schema pin: ``data/schema.yaml`` carries the worktrees:
    section with the load-bearing fields."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    wt = doc.get("worktrees")
    assert wt is not None, "0185: schema.yaml missing 'worktrees:' section"
    assert wt["base_path"] == ".worktrees"
    assert wt["branch_prefix"] == "task/"
    assert wt["merge_strategy"] == "--no-ff"
    assert wt["cleanup_on_verified"] is True
    assert wt["conflict_handback_to"] == "feature_dev"
    assert "feature" in wt["required_for_task_kinds"]
