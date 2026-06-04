"""greatminds worktree — per-task git worktree lifecycle (0185).

Replaces the 0115/0166 file-lock band-aid. Each task gets its own
working tree under ``<project_dir>/.worktrees/<task-id>/`` on a
branch ``task/<task-id>``. Implementer agents ``cd`` into their
worktree at claim time and edit without contention with other tasks
that touch the same files.

Subcommands::

    greatminds worktree create <task-id> [--base <sha>]
    greatminds worktree remove <task-id> [--force]
    greatminds worktree merge  <task-id>
    greatminds worktree list
    greatminds worktree prune

The policy (base_path, branch_prefix, merge_strategy, cleanup_on_*,
conflict_handback_to, required_for_task_kinds) lives in
``schema.yaml > worktrees:`` per the USER's machine-readable-first
directive. This module reads those values at runtime; it does not
encode policy in code.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, ok, warn


# ---------------------------------------------------------------------------
# Policy load (schema.yaml > worktrees:)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreePolicy:
    """Machine-readable worktree policy from schema.yaml.

    Defaults match the plan defaults so missing-section schemas still
    yield a sensible behavior (rather than crashing the CLI).
    """
    base_path: str = ".worktrees"
    branch_prefix: str = "task/"
    # Branch that task worktrees base off (last-resort) and merge back
    # into. Configurable so a project can run its coordination on a
    # branch other than main; defaults to main.
    default_branch: str = "main"
    merge_strategy: str = "--no-ff"
    cleanup_on_archive: bool = True
    cleanup_on_verified: bool = True
    conflict_handback_to: str = "feature_dev"
    required_for_task_kinds: tuple[str, ...] = ("feature", "bugfix", "ops")

    def branch_for(self, task_id: str) -> str:
        return f"{self.branch_prefix}{task_id}"

    def worktree_path_for(self, project_dir: Path, task_id: str) -> Path:
        return project_dir / self.base_path / task_id


def load_worktree_policy() -> WorktreePolicy:
    """Read schema.yaml's ``worktrees:`` section into a typed policy."""
    schema_path = find_canon_dir() / "schema.yaml"
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return WorktreePolicy()
    raw = doc.get("worktrees") or {}
    return WorktreePolicy(
        base_path=str(raw.get("base_path") or ".worktrees"),
        branch_prefix=str(raw.get("branch_prefix") or "task/"),
        default_branch=str(raw.get("default_branch") or "main"),
        merge_strategy=str(raw.get("merge_strategy") or "--no-ff"),
        cleanup_on_archive=bool(raw.get("cleanup_on_archive", True)),
        cleanup_on_verified=bool(raw.get("cleanup_on_verified", True)),
        conflict_handback_to=str(
            raw.get("conflict_handback_to") or "feature_dev"),
        required_for_task_kinds=tuple(
            raw.get("required_for_task_kinds") or
            ["feature", "bugfix", "ops"]),
    )


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def _run_git(cmd: list[str], cwd: Path | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git`` and return CompletedProcess. Raises GreatMindsError
    on non-zero unless ``check=False`` (caller wants to inspect)."""
    cp = subprocess.run(
        ["git", *cmd], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if check and cp.returncode != 0:
        raise GreatMindsError(
            f"git {' '.join(cmd)} failed (exit {cp.returncode}): "
            f"{cp.stderr.strip()[:300]}"
        )
    return cp


def _resolve_base_commit(project_dir: Path, task_id: str,
                         explicit: str | None,
                         default_branch: str = "main") -> str:
    """Determine the base_commit for a worktree.

    Priority: explicit ``--base`` arg → task's plan.base_commit (via
    find_task) → ``default_branch`` HEAD (last-resort fallback).
    """
    if explicit:
        return explicit
    try:
        from greatminds.cli.task import find_task
    except ImportError:
        find_task = None
    if find_task is not None:
        coord = project_dir / "coordination"
        located = find_task(coord, task_id)
        if located:
            path, _queue = located
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                doc = {}
            for block in (doc.get("blocks") or []):
                if isinstance(block, dict) and block.get("kind") == "plan":
                    bc = block.get("base_commit")
                    if isinstance(bc, str) and bc:
                        return bc
    # Last-resort fallback: default_branch HEAD.
    cp = _run_git(["rev-parse", default_branch], cwd=project_dir, check=False)
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    raise GreatMindsError(
        f"cannot resolve base_commit for {task_id}: pass --base explicitly"
    )


def worktree_create(project_dir: Path, task_id: str,
                    base: str | None = None,
                    policy: WorktreePolicy | None = None) -> Path:
    """Create a worktree for ``task_id`` at the policy path.

    Idempotent: if the worktree already exists at the expected path,
    returns the path without error. The branch ``task/<task_id>`` is
    created off ``base_commit`` (plan default or explicit override).
    """
    policy = policy or load_worktree_policy()
    wt_path = policy.worktree_path_for(project_dir, task_id)
    branch = policy.branch_for(task_id)

    if wt_path.is_dir() and (wt_path / ".git").exists():
        return wt_path  # idempotent no-op

    base_commit = _resolve_base_commit(project_dir, task_id, base,
                                       policy.default_branch)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    # ``git worktree add -b <branch> <path> <commit>`` creates the
    # branch if absent. If the branch already exists (leftover from
    # a prior aborted create), drop -b and add to the existing branch.
    cp = _run_git(
        ["worktree", "add", "-b", branch, str(wt_path), base_commit],
        cwd=project_dir, check=False,
    )
    if cp.returncode != 0 and "already exists" in (cp.stderr or ""):
        _run_git(
            ["worktree", "add", str(wt_path), branch],
            cwd=project_dir,
        )
    elif cp.returncode != 0:
        raise GreatMindsError(
            f"git worktree add failed: {cp.stderr.strip()[:300]}"
        )
    return wt_path


def worktree_remove(project_dir: Path, task_id: str,
                    force: bool = False,
                    policy: WorktreePolicy | None = None) -> bool:
    """Remove a worktree + its branch.

    Returns True if anything was removed, False if the worktree
    didn't exist (idempotent no-op).
    """
    policy = policy or load_worktree_policy()
    wt_path = policy.worktree_path_for(project_dir, task_id)
    branch = policy.branch_for(task_id)
    removed = False

    if wt_path.is_dir():
        rm_args = ["worktree", "remove"]
        if force:
            rm_args.append("--force")
        rm_args.append(str(wt_path))
        _run_git(rm_args, cwd=project_dir, check=False)
        removed = True
    # Drop the branch even if the worktree was already gone (best
    # effort: branch may already be deleted or never created).
    _run_git(["branch", "-D", branch], cwd=project_dir, check=False)
    return removed


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a worktree merge into main."""
    ok: bool
    conflicts: tuple[str, ...]
    message: str


def worktree_merge(project_dir: Path, task_id: str,
                   summary: str = "",
                   policy: WorktreePolicy | None = None) -> MergeResult:
    """Merge ``task/<task_id>`` into the policy's default_branch.

    On conflict: abort the merge (so the target branch stays clean), return
    ``MergeResult(ok=False, conflicts=[...])`` so the caller can
    hand back to ``conflict_handback_to`` per policy.
    """
    policy = policy or load_worktree_policy()
    branch = policy.branch_for(task_id)
    target = policy.default_branch
    # 0300 (upstream issue #6): merge direction MUST be
    # ``checkout <target> → merge task/<id>``. Pre-0300 the upstream
    # reporter saw `git log <target>` never advance because the merge
    # ran from the task branch instead (first parent = task work,
    # second parent = origin/<target> — the wrong direction; the
    # target was left orphaned). The current code is correct; the
    # regression tests added in 0300 pin the order. ``target`` is the
    # configurable default_branch (was hardcoded ``main``) so a project
    # can run its coordination on another branch.
    _run_git(["checkout", target], cwd=project_dir)
    # 0300: fast-forward local <target> against origin/<target> BEFORE
    # the merge so REVIEWER's merge commit lands on top of the latest
    # remote state, not a stale snapshot. Best-effort — when the
    # remote is unreachable or <target> has diverged, we still merge
    # against the current local <target> rather than blocking.
    _run_git(["pull", "--ff-only", "origin", target],
              cwd=project_dir, check=False)
    msg = summary or f"merge({task_id})"
    cp = _run_git(
        ["merge", policy.merge_strategy, "-m", msg, branch],
        cwd=project_dir, check=False,
    )
    if cp.returncode == 0:
        return MergeResult(ok=True, conflicts=(), message=cp.stdout.strip())
    # Merge conflict — collect unmerged paths, abort.
    conflicts_cp = _run_git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=project_dir, check=False,
    )
    conflicts = tuple(
        line.strip() for line in (conflicts_cp.stdout or "").splitlines()
        if line.strip()
    )
    _run_git(["merge", "--abort"], cwd=project_dir, check=False)
    return MergeResult(
        ok=False, conflicts=conflicts,
        message=f"merge {branch} into main conflicted on "
                f"{len(conflicts)} file(s): {', '.join(conflicts[:5])}",
    )


def worktree_list(project_dir: Path) -> list[dict[str, Any]]:
    """Return active worktrees as a list of dicts (path, branch, head)."""
    cp = _run_git(["worktree", "list", "--porcelain"], cwd=project_dir,
                  check=False)
    if cp.returncode != 0:
        return []
    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in (cp.stdout or "").splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = line[len("worktree "):].strip()
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip()
    if cur:
        entries.append(cur)
    return entries


def worktree_prune(project_dir: Path,
                   active_task_ids: set[str],
                   policy: WorktreePolicy | None = None) -> list[str]:
    """Remove worktrees whose task_id is NOT in ``active_task_ids``.

    Returns the list of pruned task_ids. Used by the watchdog's
    orphan-sweep tick. Idempotent — repeated calls with the same
    active set yield no further removals.
    """
    policy = policy or load_worktree_policy()
    base = project_dir / policy.base_path
    if not base.is_dir():
        return []
    pruned: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        task_id = child.name
        if task_id in active_task_ids:
            continue
        if worktree_remove(project_dir, task_id, force=True, policy=policy):
            pruned.append(task_id)
    return pruned


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


def _default_project_dir() -> Path:
    return Path.cwd()


@click.group(help="Per-task git worktree lifecycle (0185).")
def worktree() -> None:
    pass


@worktree.command("create")
@click.argument("task_id")
@click.option("--base", default=None, help="Override base commit "
              "(default: task's plan.base_commit, then main HEAD).")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_create(task_id: str, base: str | None,
               project_dir: Path | None) -> None:
    """Create a worktree for TASK_ID (idempotent)."""
    pd = project_dir or _default_project_dir()
    try:
        path = worktree_create(pd, task_id, base=base)
    except GreatMindsError as exc:
        err(str(exc))
        raise SystemExit(2)
    ok(f"worktree {task_id} at {path}")


@worktree.command("remove")
@click.argument("task_id")
@click.option("--force", is_flag=True, default=False)
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_remove(task_id: str, force: bool,
               project_dir: Path | None) -> None:
    """Remove the worktree for TASK_ID."""
    pd = project_dir or _default_project_dir()
    removed = worktree_remove(pd, task_id, force=force)
    if removed:
        ok(f"removed worktree {task_id}")
    else:
        info(f"no worktree at {task_id} (already gone)")


@worktree.command("merge")
@click.argument("task_id")
@click.option("--summary", default="", help="Commit message suffix.")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_merge(task_id: str, summary: str,
              project_dir: Path | None) -> None:
    """Merge task/TASK_ID into main with the policy strategy."""
    pd = project_dir or _default_project_dir()
    result = worktree_merge(pd, task_id, summary=summary)
    if result.ok:
        ok(f"merged task/{task_id} into main")
    else:
        warn(result.message)
        for f in result.conflicts:
            click.echo(f"  conflict: {f}")
        raise SystemExit(3)


@worktree.command("list")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_list(project_dir: Path | None) -> None:
    """List active worktrees."""
    pd = project_dir or _default_project_dir()
    for entry in worktree_list(pd):
        click.echo(
            f"{entry.get('path','?')}\t"
            f"{entry.get('branch','(detached)')}\t"
            f"{entry.get('head','?')[:12]}"
        )


@worktree.command("path")
@click.argument("task_id")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_path(task_id: str, project_dir: Path | None) -> None:
    """Print the worktree path for TASK_ID.

    Self-contained substitute for ``$GREATMINDS_TASK_WORKTREE``: an
    implementer agent or STAND-KEEPER's rsync wrapper invokes
    ``cd "$(greatminds worktree path <task-id>)"`` to enter the task's
    isolated tree.
    """
    pd = project_dir or _default_project_dir()
    policy = load_worktree_policy()
    click.echo(str(policy.worktree_path_for(pd, task_id)))


@worktree.command("assert-drained")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_assert_drained(project_dir: Path | None) -> None:
    """Exit non-zero if any feature_* queue is non-empty.

    0185 cutover safety: deploying the worktree feature on top of a
    non-drained pipeline would mix lock-era tasks with worktree-era
    tasks and produce unmergeable state. MAINTAINER runs this before
    rebuilding the wheel; non-zero refuses the cutover with the list
    of in-flight tasks per queue.
    """
    pd = project_dir or _default_project_dir()
    coord = pd / "coordination"
    if not coord.is_dir():
        err("no coordination/ directory found")
        raise SystemExit(2)
    drain_queues = (
        "feature_inbox", "feature_plan", "feature_dev",
        "feature_ui_dev", "feature_docs", "feature_test",
        "feature_docs_review", "feature_review", "feature_blocked",
    )
    in_flight: list[tuple[str, list[str]]] = []
    for q in drain_queues:
        qdir = coord / q
        if not qdir.is_dir():
            continue
        tasks = sorted(
            f.name for f in qdir.iterdir()
            if f.suffix in (".yaml", ".md")
            and not f.name.startswith("_TEMPLATE")
        )
        if tasks:
            in_flight.append((q, tasks))
    if in_flight:
        err("pipeline NOT drained — refuse cutover:")
        for q, tasks in in_flight:
            err(f"  {q}: {len(tasks)} task(s): "
                f"{', '.join(t[:60] for t in tasks[:3])}"
                + (" …" if len(tasks) > 3 else ""))
        raise SystemExit(3)
    ok("pipeline drained — safe to cut over")


@worktree.command("prune")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_prune(project_dir: Path | None) -> None:
    """Remove worktrees whose task_id is not in any active queue."""
    pd = project_dir or _default_project_dir()
    # Discover active task ids from coordination/.
    coord = pd / "coordination"
    active: set[str] = set()
    if coord.is_dir():
        for q in coord.iterdir():
            if not q.is_dir() or q.name.startswith("."):
                continue
            if q.name in ("verified", "archive", "stand_done"):
                continue  # terminal; their worktrees are eligible for prune
            if q.name in ("inbox", "intent"):
                continue
            for f in q.iterdir():
                if f.suffix in (".yaml", ".md"):
                    active.add(f.stem)
                    # Also support short-id (first 4 chars) prune match
                    if len(f.stem) > 4:
                        active.add(f.stem[:4])
    pruned = worktree_prune(pd, active)
    if not pruned:
        info("no orphan worktrees to prune")
    else:
        ok(f"pruned {len(pruned)}: {', '.join(pruned)}")
