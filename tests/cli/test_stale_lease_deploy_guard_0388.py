"""Tests for task 0388: refuse to deploy a STALE lease worktree.

The avatar 0379 regression: a resumed review_session redeployed its old
base commit and rediscovered the very wedge whose fix had already been
verified upstream (0387), while the stand still reported "ready".

``stale_verified_deps_for_lease`` (stand.py) detects this: for every
``verified/<id>`` dependency the leasing task was blocked on, it checks
whether the dependency's verified review-commit is contained in the lease
worktree's git history. A commit that exists in the repo but is NOT an
ancestor of the worktree HEAD means the worktree predates the verified
fix → stale.

CONSERVATIVE by construction: missing task_id / worktree, a non-git
worktree, a dependency that isn't in ``verified/`` or has no review
commit, or a commit git can't resolve all yield ``[]`` (no false stale).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss
from greatminds.cli.coordd import REGISTRY_DIR


# ---------------------------------------------------------------------------
# git worktree fixture
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True).stdout.strip()


def _make_repo(path: Path) -> tuple[str, str]:
    """Init a repo with two linear commits; return (commit_a, commit_b)
    where B is the child of A (B contains A, A does not contain B)."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("a\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "A")
    commit_a = _git(path, "rev-parse", "HEAD")
    (path / "f.txt").write_text("b\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "B (verified fix)")
    commit_b = _git(path, "rev-parse", "HEAD")
    return commit_a, commit_b


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / REGISTRY_DIR).mkdir(parents=True)
    (coord / "verified").mkdir(parents=True)
    (coord / "review_sessions").mkdir(parents=True)
    return coord


def _write_task(coord: Path, queue: str, tid: str, data: dict) -> None:
    data.setdefault("id", tid)
    (coord / queue / f"{tid}.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8")


def _verified_dep(coord: Path, dep_id: str, commit: str | None) -> None:
    blocks = []
    if commit is not None:
        blocks.append({"kind": "review", "outcome": "approved",
                       "commit": commit})
    _write_task(coord, "verified", dep_id,
                {"stream": "product", "blocks": blocks})


def _leasing_task(coord: Path, tid: str, dep_refs: list[str]) -> None:
    _write_task(coord, "review_sessions", tid, {
        "stream": "product",
        "blocks": [{"kind": "blocked", "reason": "dep",
                    "dependencies": dep_refs,
                    "resume_to": "review_sessions"}],
    })


# ---------------------------------------------------------------------------
# positive: stale worktree (HEAD predates the verified commit)
# ---------------------------------------------------------------------------

def test_stale_when_worktree_predates_verified_commit(tmp_path):
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    commit_a, commit_b = _make_repo(wt)
    # worktree HEAD at A (old base); verified fix is B
    _git(wt, "checkout", "-q", commit_a)
    _verified_dep(coord, "0387-fix", commit_b)
    _leasing_task(coord, "0379-campaign", ["verified/0387-fix.yaml"])

    stale = stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt))
    assert stale == [("0387-fix", commit_b)]


def test_fresh_when_worktree_contains_verified_commit(tmp_path):
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    _commit_a, commit_b = _make_repo(wt)
    # worktree HEAD at B → contains the verified fix → not stale
    _verified_dep(coord, "0387-fix", commit_b)
    _leasing_task(coord, "0379-campaign", ["verified/0387-fix.yaml"])

    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt)) == []


# ---------------------------------------------------------------------------
# conservative / fail-open cases — must all return []
# ---------------------------------------------------------------------------

def test_no_task_id_or_worktree_is_not_stale(tmp_path):
    coord = _coord(tmp_path)
    assert stand_mod.stale_verified_deps_for_lease(coord, None, "/x") == []
    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", None) == []


def test_non_git_worktree_is_not_stale(tmp_path):
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    wt.mkdir(parents=True)  # plain dir, no git
    _verified_dep(coord, "0387-fix", "deadbeef" * 5)
    _leasing_task(coord, "0379-campaign", ["verified/0387-fix.yaml"])
    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt)) == []


def test_dep_not_in_verified_is_ignored(tmp_path):
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    commit_a, _commit_b = _make_repo(wt)
    _git(wt, "checkout", "-q", commit_a)
    # dependency points at feature_review/, not verified/ → not checked
    _leasing_task(coord, "0379-campaign", ["feature_review/0387-fix.yaml"])
    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt)) == []


def test_dep_without_review_commit_is_ignored(tmp_path):
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    commit_a, _commit_b = _make_repo(wt)
    _git(wt, "checkout", "-q", commit_a)
    _verified_dep(coord, "0387-fix", None)  # no review block / commit
    _leasing_task(coord, "0379-campaign", ["verified/0387-fix.yaml"])
    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt)) == []


def test_unresolvable_commit_is_ignored(tmp_path):
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    _make_repo(wt)
    # a commit sha that does not exist in this repo → can't test ancestry
    _verified_dep(coord, "0387-fix", "0" * 40)
    _leasing_task(coord, "0379-campaign", ["verified/0387-fix.yaml"])
    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt)) == []


def test_latest_review_commit_wins(tmp_path):
    # _verified_dep_commit takes the LAST review block's commit.
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "0379"
    commit_a, commit_b = _make_repo(wt)
    _git(wt, "checkout", "-q", commit_a)
    _write_task(coord, "verified", "0387-fix", {
        "stream": "product",
        "blocks": [
            {"kind": "review", "outcome": "changes_requested",
             "commit": commit_a},   # earlier (present in wt)
            {"kind": "review", "outcome": "approved",
             "commit": commit_b},   # latest (NOT present in wt) → stale
        ],
    })
    _leasing_task(coord, "0379-campaign", ["verified/0387-fix.yaml"])
    assert stand_mod.stale_verified_deps_for_lease(
        coord, "0379-campaign", str(wt)) == [("0387-fix", commit_b)]


def test_review_session_checks_all_verified_product_commits(tmp_path):
    """1008 regression: a final EXPLORER review-session lease can be
    created before later product work reaches verified. After product
    drain, deploying that old review-session worktree must be refused
    even when the review_session has no explicit blocked dependency on
    the later verified task."""
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "1004-review"
    commit_a, commit_b = _make_repo(wt)
    _git(wt, "checkout", "-q", commit_a)
    _verified_dep(coord, "1001-safe-divide", commit_b)
    _write_task(coord, "review_sessions", "1004-review", {
        "stream": "review_session",
        "kind": "review_session",
        "blocks": [
            {"kind": "session_iteration", "summary": "no explicit deps"},
        ],
    })

    assert stand_mod.stale_verified_deps_for_lease(
        coord, "1004-review", str(wt)) == [("1001-safe-divide", commit_b)]


def test_product_task_does_not_check_unrelated_verified_commits(tmp_path):
    """The broad all-verified check is review_session-only. Product task
    leases keep the narrower explicit-dependency behavior so unrelated
    verified work does not block their stand validation."""
    coord = _coord(tmp_path)
    wt = tmp_path / "proj" / ".worktrees" / "1001-product"
    commit_a, commit_b = _make_repo(wt)
    _git(wt, "checkout", "-q", commit_a)
    _verified_dep(coord, "1000-unrelated", commit_b)
    _write_task(coord, "review_sessions", "1001-product", {
        "stream": "product",
        "blocks": [
            {"kind": "blocked", "dependencies": [], "resume_to": "feature_test"},
        ],
    })

    assert stand_mod.stale_verified_deps_for_lease(
        coord, "1001-product", str(wt)) == []


def test_stale_deploy_message_names_truthful_refresh_path(
        tmp_path, monkeypatch):
    """0393: STALE DEPLOYMENT failure reason must name the sanctioned
    command sequence that refreshes review-session branches."""
    coord = _coord(tmp_path)
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        stand_mod, "stale_verified_deps_for_lease",
        lambda _coord, _task, _worktree: [("0390-auth", "7e976fbd7864")],
    )
    monkeypatch.setattr(
        stand_mod, "_file_inbox_info",
        lambda coord_, to_role, body, task_ref="": sent.append(
            (to_role, body, task_ref)
        ),
    )
    ss.update_stand_state(coord, lambda state: state.update({
        "state": "preparing",
        "active_lease": {
            "lease_id": "lease-1",
            "task": "0379-campaign",
            "worktree": "/tmp/proj/.worktrees/0379-campaign",
            "profile": "full-deploy",
            "holder_role": "EXPLORER",
        },
    }))

    rc, log = stand_mod.deploy_lease(coord, lease_id="lease-1")

    assert rc == stand_mod.DEPLOY_STALE_RC
    assert "greatminds worktree remove --force <id>" in log
    assert "greatminds worktree create <id>" in log
    assert "greatminds stand up --reason stale-worktree-refreshed" in log
    state = ss.read_stand_state(coord)
    assert state["state"] == "free"
    assert state["active_lease"] is None
    assert state.get("down_reason") is None
    assert "worktree create" in state["last_deploy_failure"]["reason"]
    assert sent and {row[0] for row in sent} == {
        "EXPLORER", "ARCHITECT-PLANNER"}
