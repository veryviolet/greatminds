"""Tests for task 0166: 0115 file-lock release normalizes task_id.

Pre-0166 ``_acquire_file_locks_for_task`` stored the caller's
``task_id`` verbatim. Callers passed a short id (``0158``) at
append-block time and the full slug
(``0158-codex-per-role-instructions-...``) at mv-to-verified time.
``_release_file_locks_for_task``'s exact-match never matched → locks
accumulated. Today's session has seen this blow up 5+ times across
0137 / 0143 / 0144 / 0147 / 0149 / 0150 / 0152 / 0153 / 0158 / 0160.

0166 canonicalizes at BOTH ends (via the unified ``find_task`` 0114
helper) so the stored holder is always the full slug; release also
accepts either-form / numeric-prefix matches as a defensive fallback
for pre-0166 stale locks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import task as task_mod


def _make_task(coord: Path, queue: str, stem: str) -> Path:
    qd = coord / queue
    qd.mkdir(parents=True, exist_ok=True)
    p = qd / f"{stem}.yaml"
    p.write_text(f"id: {stem}\n", encoding="utf-8")
    return p


# ---------- acquire stores canonical (full slug) ----------


def test_acquire_with_short_id_stores_full_slug(tmp_path: Path) -> None:
    """0166: caller passes ``0099``; lock content holds
    ``0099-some-feature``. Without this, the prior bug returns the
    moment two tasks touch the same file across the
    short/long-id boundary."""
    coord = tmp_path
    _make_task(coord, "feature_dev", "0099-some-feature")
    task_mod._acquire_file_locks_for_task(
        coord, "0099", ["src/foo.py"],
    )
    lp = task_mod._file_lock_path(coord, "src/foo.py")
    assert lp.read_text(encoding="utf-8").strip() == "0099-some-feature"


def test_acquire_with_full_slug_stores_full_slug(tmp_path: Path) -> None:
    """Idempotent: caller already passes the canonical form → no change."""
    coord = tmp_path
    _make_task(coord, "feature_dev", "0099-some-feature")
    task_mod._acquire_file_locks_for_task(
        coord, "0099-some-feature", ["src/foo.py"],
    )
    lp = task_mod._file_lock_path(coord, "src/foo.py")
    assert lp.read_text(encoding="utf-8").strip() == "0099-some-feature"


def test_acquire_falls_back_to_input_when_task_missing(tmp_path: Path) -> None:
    """Defensive: when the task isn't found in any queue (e.g. acquire
    called from a context where the file doesn't exist yet — unlikely
    but possible during intake races), store the input verbatim.
    Without this, a transient missing-task would write empty content
    which both helpers treat as 'unowned' and the lock becomes
    meaningless."""
    coord = tmp_path
    (coord / "feature_dev").mkdir(parents=True)
    # No task file seeded.
    task_mod._acquire_file_locks_for_task(
        coord, "0099-orphan", ["src/foo.py"],
    )
    lp = task_mod._file_lock_path(coord, "src/foo.py")
    assert lp.read_text(encoding="utf-8").strip() == "0099-orphan"


# ---------- release matches across id forms ----------


def test_release_full_slug_clears_short_id_lock(tmp_path: Path) -> None:
    """The 0166 canonical case + defensive fallback: a pre-fix short-id
    lock written by an older install gets cleaned up when the release
    side passes the full slug (post-fix). Without the fallback, the
    stale lock stays forever."""
    coord = tmp_path
    _make_task(coord, "feature_dev", "0099-some-feature")
    # Hand-write a pre-0166 short-id lock to simulate an upgrade scenario.
    lp = task_mod._file_lock_path(coord, "src/foo.py")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("0099", encoding="utf-8")

    task_mod._release_file_locks_for_task(coord, "0099-some-feature")
    assert not lp.exists()


def test_release_short_id_clears_full_slug_lock(tmp_path: Path) -> None:
    """Symmetric: an old caller still using the short-id at release
    must clean up canonical-id locks too. Defensive bidirectional
    match means neither side has to be upgraded for the release to
    succeed."""
    coord = tmp_path
    _make_task(coord, "feature_dev", "0099-some-feature")
    lp = task_mod._file_lock_path(coord, "src/foo.py")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("0099-some-feature", encoding="utf-8")

    task_mod._release_file_locks_for_task(coord, "0099")
    assert not lp.exists()


def test_release_full_slug_clears_full_slug_lock(tmp_path: Path) -> None:
    """Sanity pin: the boring same-form case still works."""
    coord = tmp_path
    _make_task(coord, "feature_dev", "0099-some-feature")
    lp = task_mod._file_lock_path(coord, "src/foo.py")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("0099-some-feature", encoding="utf-8")

    task_mod._release_file_locks_for_task(coord, "0099-some-feature")
    assert not lp.exists()


def test_release_does_not_clear_unrelated_locks(tmp_path: Path) -> None:
    """Negative pin: numeric-prefix fallback must not over-match.
    A lock owned by task ``0042-...`` must NOT be released when the
    release call is for ``0099-...``. Without this guard the 4-digit
    overlap (e.g. ``0099`` short vs ``0099`` numeric in a different
    full slug) wouldn't be a problem here because both share the
    same prefix — but a release for ``0099`` must NOT touch ``0042``.
    """
    coord = tmp_path
    _make_task(coord, "feature_dev", "0042-other")
    _make_task(coord, "feature_dev", "0099-some-feature")
    lp_other = task_mod._file_lock_path(coord, "src/other.py")
    lp_other.parent.mkdir(parents=True, exist_ok=True)
    lp_other.write_text("0042-other", encoding="utf-8")

    task_mod._release_file_locks_for_task(coord, "0099-some-feature")
    assert lp_other.is_file(), "0166: must NOT release unrelated locks"


# ---------- end-to-end: short-id acquire → mv-to-verified releases ----------


def test_e2e_short_id_acquire_releases_on_verify(tmp_path: Path,
                                                  monkeypatch) -> None:
    """The full bug scenario as it materialized in production: acquire
    with short id, release at mv-to-verified time (which passes the
    full slug). Pre-0166 the release matched nothing. Post-0166 the
    lock is gone."""
    coord = tmp_path
    _make_task(coord, "feature_dev", "0099-prod-bug")

    task_mod._acquire_file_locks_for_task(
        coord, "0099", ["src/foo.py"],
    )
    # mv-to-verified would call _release_file_locks_for_task with the
    # full slug (the move helper resolves task_id earlier and passes
    # the slug onward).
    task_mod._release_file_locks_for_task(coord, "0099-prod-bug")

    lp = task_mod._file_lock_path(coord, "src/foo.py")
    assert not lp.exists(), (
        "0166: short-id acquire + full-slug release must leave NO "
        "stale locks; production blocker on every shipped task"
    )
