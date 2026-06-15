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
from greatminds.core.paths import coord_yaml_path, find_canon_dir, project_runtime_dir
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


def load_worktree_policy(project_dir: Path | None = None) -> WorktreePolicy:
    """Build the worktree policy: canon (package) schema defaults, then
    overlaid by the project's per-project override in ``coord.yaml``.

    The canon ``schema.yaml`` (read via ``find_canon_dir`` — the PACKAGE
    data dir) supplies host-wide defaults and is overwritten on every
    upgrade, so it is the WRONG place for a project to pin its own
    ``default_branch``. The per-project override lives in ``coord.yaml``
    (project-local, never overwritten by ``setup``/``upgrade``), e.g.::

        worktrees:
          default_branch: unify

    so a project can run its fleet on a branch other than main cleanly.
    When ``project_dir`` is None only the canon defaults apply.
    """
    schema_path = find_canon_dir() / "schema.yaml"
    raw: dict = {}
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
        if isinstance(doc.get("worktrees"), dict):
            raw = dict(doc["worktrees"])
    except (OSError, yaml.YAMLError):
        raw = {}
    # Per-project override from coord.yaml (durable across upgrades).
    if project_dir is not None:
        cand = coord_yaml_path(project_dir)
        if cand.is_file():
            try:
                cdoc = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                over = cdoc.get("worktrees")
                if isinstance(over, dict):
                    raw.update(over)  # project wins over canon defaults
            except (OSError, yaml.YAMLError):
                pass
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


def canonical_task_id(project_dir: Path, task_id: str) -> str:
    """Resolve any task-id form to the FULL canonical task id (0383).

    ``greatminds <cmd> <id>`` accepts either the short seq prefix
    (``0382``) or the full slug (``0382-full-deploy-...``). The worktree
    PATH, BRANCH, MERGE, and FINGERPRINT must ALL key off one value, or a
    short id resolves to ``.worktrees/0382`` / ``task/0382`` while the
    real worktree created at mv-time lives at ``.worktrees/0382-<slug>``
    / ``task/0382-<slug>`` — the split behind the recurring 0361/0365/
    0380 phantom (empty-branch) and empty-merge handoffs. This resolver
    maps the given id to the task file's stem (its full id) via
    ``find_task`` so every worktree operation converges on the SAME
    worktree/branch. Falls back to the given id when no task file is
    found (e.g. a brand-new task whose file isn't written yet, or a
    test fixture with no runtime tree)."""
    try:
        from greatminds.cli.task import find_task
    except ImportError:
        return task_id
    try:
        located = find_task(project_runtime_dir(project_dir), task_id)
    except Exception:
        return task_id
    if located:
        path, _queue = located
        stem = path.stem
        if stem:
            return stem
    return task_id


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
        coord = project_runtime_dir(project_dir)
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


def _task_stream_and_queue(project_dir: Path,
                           task_id: str) -> tuple[str | None, str | None]:
    """Return ``(stream, queue)`` for TASK_ID when it is visible to the FSM.

    Worktree refresh policy is deliberately narrower for ``review_session``
    tasks: those worktrees are deploy/probe snapshots, not implementer-owned
    code branches. If an old review-session branch predates newly verified
    blocker fixes, recreating its checkout must refresh the branch to the
    selected base instead of reusing stale history. Product implementation
    branches keep the old behavior (reuse existing branch) to avoid silently
    discarding implementer commits.
    """
    try:
        from greatminds.cli.task import find_task, load_task
    except ImportError:
        return None, None
    try:
        located = find_task(project_runtime_dir(project_dir), task_id)
    except Exception:
        return None, None
    if not located:
        return None, None
    path, queue = located
    try:
        doc = load_task(path)
    except Exception:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            doc = {}
    stream = doc.get("stream") if isinstance(doc, dict) else None
    return (stream if isinstance(stream, str) else None,
            queue if isinstance(queue, str) else None)


def _branch_exists(project_dir: Path, branch: str) -> bool:
    cp = _run_git(["rev-parse", "--verify", "--quiet", branch],
                  cwd=project_dir, check=False)
    return cp.returncode == 0


def _ensure_review_session_branch_contains_base(
        project_dir: Path, task_id: str, branch: str,
        base_commit: str) -> None:
    """0393: refresh stale review-session branches before worktree add.

    ``git worktree add <path> <existing-branch>`` reuses the branch exactly
    as-is. That is correct for product implementation branches, but wrong for
    review sessions after blocker dependencies have been verified on main:
    removing and recreating the checkout would still deploy the old branch.

    For review_session tasks only, if the existing branch does not contain the
    selected base commit (normally current main), force the branch name to the
    base before adding the checkout. No worktree exists at this point, so there
    are no uncommitted files to overwrite; if git refuses because the branch is
    checked out elsewhere, surface an actionable error instead of silently
    reusing stale code.
    """
    stream, queue = _task_stream_and_queue(project_dir, task_id)
    if stream != "review_session" and queue != "review_sessions":
        return
    anc = _run_git(["merge-base", "--is-ancestor", base_commit, branch],
                   cwd=project_dir, check=False)
    if anc.returncode == 0:
        return
    if anc.returncode not in (1,):
        # Indeterminate (e.g. missing object): fail open for safety.
        return
    cp = _run_git(["branch", "-f", branch, base_commit],
                  cwd=project_dir, check=False)
    if cp.returncode != 0:
        raise GreatMindsError(
            f"worktree create cannot refresh stale review-session branch "
            f"{branch!r} to {base_commit[:12]} for {task_id}: "
            f"{cp.stderr.strip()[:300]}. Remove any other checkout of the "
            "branch, then rerun: greatminds worktree remove --force "
            f"{task_id} && greatminds worktree create {task_id}",
            exit_code=2,
        )


def worktree_create(project_dir: Path, task_id: str,
                    base: str | None = None,
                    policy: WorktreePolicy | None = None) -> Path:
    """Create a worktree for ``task_id`` at the policy path.

    Idempotent: if the worktree already exists at the expected path,
    returns the path without error. The branch ``task/<task_id>`` is
    created off ``base_commit`` (plan default or explicit override).
    """
    policy = policy or load_worktree_policy(project_dir)
    task_id = canonical_task_id(project_dir, task_id)  # 0383: full id only
    wt_path = policy.worktree_path_for(project_dir, task_id)
    branch = policy.branch_for(task_id)

    if wt_path.is_dir() and (wt_path / ".git").exists():
        return wt_path  # idempotent no-op

    base_commit = _resolve_base_commit(project_dir, task_id, base,
                                       policy.default_branch)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = _branch_exists(project_dir, branch)
    if branch_exists:
        _ensure_review_session_branch_contains_base(
            project_dir, task_id, branch, base_commit)
    # ``git worktree add -b <branch> <path> <commit>`` creates the
    # branch if absent. If the branch already exists (leftover from
    # a prior aborted create), drop -b and add to the existing branch.
    if branch_exists:
        _run_git(
            ["worktree", "add", str(wt_path), branch],
            cwd=project_dir,
        )
        return wt_path
    cp = _run_git(
        ["worktree", "add", "-b", branch, str(wt_path), base_commit],
        cwd=project_dir, check=False,
    )
    if cp.returncode != 0:
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
    policy = policy or load_worktree_policy(project_dir)
    task_id = canonical_task_id(project_dir, task_id)  # 0383: full id only
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
    branch_rm = _run_git(["branch", "-D", branch],
                         cwd=project_dir, check=False)
    if branch_rm.returncode != 0 and force:
        # A stale administrative git-worktree record can make branch -D
        # fail even after a force remove. Prune and retry so the sanctioned
        # "remove --force; create" recovery path does not silently leave the
        # stale branch that create would otherwise reuse.
        _run_git(["worktree", "prune"], cwd=project_dir, check=False)
        branch_rm = _run_git(["branch", "-D", branch],
                             cwd=project_dir, check=False)
        if branch_rm.returncode != 0 and _branch_exists(project_dir, branch):
            raise GreatMindsError(
                f"worktree remove --force removed {wt_path} but could not "
                f"delete branch {branch!r}: "
                f"{branch_rm.stderr.strip()[:300]}. Do not rerun create "
                "until this branch is cleaned up; otherwise git would reuse "
                "stale task history.",
                exit_code=2,
            )
    return removed


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a worktree merge into main."""
    ok: bool
    conflicts: tuple[str, ...]
    message: str


def _commit_worktree_overlay(project_dir: Path, task_id: str,
                             policy: WorktreePolicy) -> bool:
    """0383: commit a per-task worktree's uncommitted overlay onto the
    task branch so the merge has a real git object to bring in.

    Returns True when an overlay was committed, False when the worktree
    is absent or already clean (nothing to capture). ``task_id`` must
    already be canonical (the caller resolves it). The commit covers
    tracked modifications AND new untracked files (``git add -A``), which
    is exactly the DEVELOPER overlay that ``git merge <branch>`` would
    otherwise miss. Raises GreatMindsError if the overlay exists but the
    commit cannot be created — an actionable failure beats a silent
    empty merge."""
    wt_path = policy.worktree_path_for(project_dir, task_id)
    if not (wt_path.is_dir() and (wt_path / ".git").exists()):
        return False
    status = _run_git(["status", "--porcelain"], cwd=wt_path, check=False)
    if status.returncode != 0 or not (status.stdout or "").strip():
        return False  # clean worktree: nothing uncommitted to capture
    _run_git(["add", "-A"], cwd=wt_path)
    commit_cp = _run_git(
        ["-c", "user.name=greatminds",
         "-c", "user.email=greatminds@localhost",
         "commit", "--no-verify",
         "-m", f"impl({task_id}): worktree overlay captured at merge"],
        cwd=wt_path, check=False,
    )
    if commit_cp.returncode != 0:
        raise GreatMindsError(
            f"0383: failed to commit the uncommitted worktree overlay for "
            f"{task_id} onto {policy.branch_for(task_id)} before merge "
            f"(exit {commit_cp.returncode}): "
            f"{(commit_cp.stderr or commit_cp.stdout or '').strip()[:300]}. "
            f"The implementation would otherwise be silently dropped by an "
            f"empty merge.",
            exit_code=4,
        )
    return True


def worktree_merge(project_dir: Path, task_id: str,
                   summary: str = "",
                   policy: WorktreePolicy | None = None) -> MergeResult:
    """Merge ``task/<task_id>`` into the policy's default_branch.

    On conflict: abort the merge (so the target branch stays clean), return
    ``MergeResult(ok=False, conflicts=[...])`` so the caller can
    hand back to ``conflict_handback_to`` per policy.
    """
    policy = policy or load_worktree_policy(project_dir)
    task_id = canonical_task_id(project_dir, task_id)  # 0383: full id only
    branch = policy.branch_for(task_id)
    target = policy.default_branch
    # 0383: capture the per-task worktree's UNCOMMITTED overlay as a real
    # commit on the task branch before merging. DEVELOPER works
    # uncommitted by design (the git-commit hook only permits
    # ARCHITECT-REVIEWER), so without this the task branch sits at
    # base_commit with no objects and `git merge task/<id>` brings in
    # NOTHING — the empty/phantom merge that silently dropped 0365/0380's
    # implementation and forced manual MAINTAINER branch repoints. The
    # commit runs in the REVIEWER mv-to-verified context (the only path
    # that reaches a merge); --no-verify skips the permission pre-commit
    # hook because this is the CLI's own sanctioned merge plumbing, not an
    # agent-typed commit.
    _commit_worktree_overlay(project_dir, task_id, policy)
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
    policy = policy or load_worktree_policy(project_dir)
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


@click.group(help="per-task git worktree lifecycle.")
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
    policy = load_worktree_policy(pd)
    task_id = canonical_task_id(pd, task_id)  # 0383: full id, never short
    click.echo(str(policy.worktree_path_for(pd, task_id)))


@worktree.command("assert-drained")
@click.option("--project-dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def cli_assert_drained(project_dir: Path | None) -> None:
    """Exit non-zero if any feature_* queue is non-empty.

    Deploying the worktree feature on top of a non-drained pipeline can
    produce unmergeable state. MAINTAINER runs this before rebuilding the
    wheel; non-zero refuses the run with the list of in-flight tasks per queue.
    """
    pd = project_dir or _default_project_dir()
    coord = project_runtime_dir(pd)
    if not coord.is_dir():
        err("no .greatminds/ runtime directory found")
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
    # Discover active task ids from the runtime queue store.
    coord = project_runtime_dir(pd)
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
