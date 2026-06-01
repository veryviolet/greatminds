"""Tests for task 0300 (upstream issue #6): REVIEWER's
verified-merge MUST run from main checked out + merge in
``task/<id>``, never the reverse.

Upstream report (nginarea 1.3.8): main never advanced because
the merge ran from the task branch (first parent = task work,
second parent = origin/main → wrong direction; main left
orphaned). The current code is correct; these tests pin the
order so a regression can't sneak the bug back in.

0300 also adds a ``git pull --ff-only origin main`` between
checkout and merge so REVIEWER's merge commit lands on top of
the latest remote state.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from greatminds.cli import worktree as wt_mod


def test_merge_runs_checkout_main_first(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pin the merge direction: the FIRST git invocation must be
    ``git checkout main``. Anything else means the merge runs
    from the wrong branch."""
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None, check=True):
        calls.append(list(args))
        # Success for every call so the function reaches the
        # subprocess.run inside ``merge``.
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)

    result = wt_mod.worktree_merge(tmp_path, "0300-probe")
    assert result.ok
    assert calls[0][0] == "checkout"
    assert calls[0][1] == "main", (
        f"0300: first git call must be `checkout main`; got "
        f"{calls[0]!r}"
    )


def test_merge_pulls_before_merging(
    tmp_path: Path, monkeypatch,
) -> None:
    """0300: after checkout, run ``pull --ff-only origin main`` so
    local main is current with origin before the merge commit
    lands. Without this step, REVIEWER's merge can ride on top of
    a stale snapshot and the push doesn't fast-forward remote."""
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None, check=True):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)

    wt_mod.worktree_merge(tmp_path, "0300-probe")

    pull_idx = next(
        (i for i, c in enumerate(calls) if c and c[0] == "pull"),
        None,
    )
    assert pull_idx is not None, (
        "0300: missing `git pull --ff-only origin main` step"
    )
    assert calls[pull_idx][:4] == ["pull", "--ff-only",
                                     "origin", "main"]


def test_merge_target_is_task_branch_not_origin_main(
    tmp_path: Path, monkeypatch,
) -> None:
    """The actual ``git merge`` target MUST be ``task/<id>``, NOT
    ``origin/main``. The upstream bug had the inverse direction
    which produced the orphan main."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        wt_mod, "_run_git",
        lambda args, cwd=None, check=True: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="")
        ),
    )

    wt_mod.worktree_merge(tmp_path, "0300-probe")
    merge_calls = [c for c in calls
                   if c and c[0] == "merge" and "--abort" not in c]
    assert len(merge_calls) == 1
    merge_cmd = merge_calls[0]
    assert "task/0300-probe" in merge_cmd
    assert "origin/main" not in merge_cmd, (
        f"0300: merge target must NOT be origin/main (the upstream "
        f"bug). Got: {merge_cmd}"
    )


def test_merge_call_order_is_checkout_then_pull_then_merge(
    tmp_path: Path, monkeypatch,
) -> None:
    """Strict ordering: checkout → pull → merge. A regression
    that reorders these (e.g. merge first, then checkout) would
    re-introduce the upstream bug."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        wt_mod, "_run_git",
        lambda args, cwd=None, check=True: (
            calls.append(list(args))
            or subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="")
        ),
    )

    wt_mod.worktree_merge(tmp_path, "0300-probe")

    # Find indices of the first checkout / pull / merge.
    def idx(verb: str, exclude=()) -> int:
        for i, c in enumerate(calls):
            if c and c[0] == verb and not any(
                e in c for e in exclude
            ):
                return i
        return -1

    chk = idx("checkout")
    pul = idx("pull")
    mrg = idx("merge", exclude=("--abort",))
    assert chk < pul < mrg, (
        f"0300: ordering must be checkout({chk}) < pull({pul}) "
        f"< merge({mrg}). Calls: {calls!r}"
    )


def test_merge_runs_from_project_dir_not_worktree(
    tmp_path: Path, monkeypatch,
) -> None:
    """0300 invariant: ALL git invocations in worktree_merge must
    use ``cwd=project_dir`` (the main checkout), never the
    per-task worktree. Running from the worktree would lose the
    ``checkout main`` semantics (you can't check out main inside
    a worktree where it's already checked out elsewhere)."""
    cwds: list = []

    def fake_run_git(args, cwd=None, check=True):
        cwds.append(cwd)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)

    wt_mod.worktree_merge(tmp_path, "0300-probe")
    for cwd in cwds:
        assert cwd == tmp_path, (
            f"0300: every git call must use cwd={tmp_path!r} "
            f"(project_dir); got {cwd!r}"
        )


def test_merge_conflict_aborts_on_wrong_direction_attempt(
    tmp_path: Path, monkeypatch,
) -> None:
    """Conflict path: merge returns nonzero → diff --name-only
    --diff-filter=U lists unmerged → merge --abort fires. Pin
    the ordering so the abort happens BEFORE the function
    returns (otherwise main would carry the partial merge)."""
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None, check=True):
        calls.append(list(args))
        # Return nonzero for the actual merge step; success for
        # everything else.
        if args[0] == "merge" and "--abort" not in args:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="conflict")
        if args[0] == "diff":
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="src/x.py\nsrc/y.py\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)

    result = wt_mod.worktree_merge(tmp_path, "0300-probe")
    assert not result.ok
    assert "src/x.py" in result.conflicts
    # Ordering check: --abort fires AFTER the failed merge.
    abort_idx = next(
        (i for i, c in enumerate(calls)
         if c and c[0] == "merge" and "--abort" in c),
        None,
    )
    fail_merge_idx = next(
        (i for i, c in enumerate(calls)
         if c and c[0] == "merge" and "--abort" not in c),
        None,
    )
    assert fail_merge_idx is not None and abort_idx is not None
    assert fail_merge_idx < abort_idx, (
        "0300: failed merge must be ABORTED on conflict, not "
        "left on main"
    )
