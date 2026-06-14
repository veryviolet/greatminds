"""0391: a driven turn that fails/times out/SIGTERM-dies must release the
run-lock and stay recoverable.

The observed strand: a DEVELOPER driven Claude turn exited (rc=143 SIGTERM,
then a 600s timeout) but ``coordination/.locks/driven-developer.lock``
remained with no live subprocess, so feature_dev backlog was hidden behind
the stale lock forever.

These tests pin the exit-path contract of ``_finalize_driven_turn`` — the
single funnel both driven drivers (claude / codex) now run in their
``finally``:

  1. the run-lock is ALWAYS unlinked, for every exit class;
  2. ``_note_turn_outcome`` (retry scheduling) ALWAYS runs, even when an
     earlier bookkeeping step raises — so the backlog stays recoverable;
  3. a clean turn with a pending marker re-fires; a failed turn does not.
"""
from __future__ import annotations

import pytest

from greatminds.cli import coordd as cd


@pytest.fixture(autouse=True)
def _clean_retry_state():
    cd._DRIVEN_RETRY.clear()
    yield
    cd._DRIVEN_RETRY.clear()


def _coord(tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    return coord


def _held_lock(coord, role_lower="developer"):
    lock = cd._driven_run_lock_path(coord, role_lower)
    lock.touch()
    return lock


# ---------------- lock release across every exit class ----------------


@pytest.mark.parametrize("klass", ["error", "timeout", "rate_limit", "ok"])
def test_finalize_always_releases_lock(tmp_path, klass, monkeypatch):
    """rc=143 (error), TimeoutExpired (timeout), rate_limit, and clean ok
    all release the run-lock — the core 0391 invariant."""
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._finalize_driven_turn(coord, "developer", lock, klass, "boom", False)
    assert not lock.exists()


def test_rc143_error_releases_lock_and_schedules_retry(tmp_path, monkeypatch):
    """A SIGTERM/rc=143 turn (classified ``error``) frees the lock AND leaves
    a retry scheduled so the role re-drives instead of stranding."""
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._finalize_driven_turn(coord, "developer", lock, "error",
                             "rc=143 SIGTERM", False)
    assert not lock.exists()
    st = cd._DRIVEN_RETRY["developer"]
    assert st["klass"] == "error" and st["attempts"] == 1
    assert st["next_at"] > 0          # a retry is due → recoverable


def test_timeout_releases_lock_and_schedules_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._finalize_driven_turn(coord, "developer", lock, "timeout",
                             "turn exceeded 600s", False)
    assert not lock.exists()
    st = cd._DRIVEN_RETRY["developer"]
    assert st["klass"] == "timeout" and st["attempts"] == 1


# ---------------- bookkeeping raises mid-finalize ----------------


def test_record_raise_still_releases_lock_and_notes_outcome(
        tmp_path, monkeypatch):
    """The reported failure mode: a post-run bookkeeping step raises after
    the subprocess exits. The lock must STILL be gone and the retry must
    STILL be scheduled (the earlier unguarded chain skipped both)."""
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)

    def _boom():
        raise OSError("registry write failed (ENOSPC)")

    cd._finalize_driven_turn(coord, "developer", lock, "error", "boom",
                             False, record=_boom)
    assert not lock.exists()
    # _note_turn_outcome ran despite the record() raise → recoverable.
    assert cd._DRIVEN_RETRY["developer"]["attempts"] == 1


def test_enrich_raise_still_notes_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)

    def _boom_enrich(detail, coord):
        raise RuntimeError("enrich blew up")

    monkeypatch.setattr(cd, "_enrich_failure_detail", _boom_enrich)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._finalize_driven_turn(coord, "developer", lock, "error", "boom", False)
    assert not lock.exists()
    assert cd._DRIVEN_RETRY["developer"]["attempts"] == 1


def test_note_raise_still_releases_lock(tmp_path, monkeypatch):
    """Even if the outcome note itself raises, the lock is already gone."""
    def _boom_note(*a, **k):
        raise RuntimeError("note blew up")

    monkeypatch.setattr(cd, "_note_turn_outcome", _boom_note)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._finalize_driven_turn(coord, "developer", lock, "error", "boom", False)
    assert not lock.exists()


# ---------------- pending re-fire policy ----------------


def test_clean_turn_with_pending_refires(tmp_path):
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._driven_pending_path(coord, "developer").touch()
    fired = []
    cd._finalize_driven_turn(coord, "developer", lock, "ok", "", False,
                             refire=lambda: fired.append(True))
    assert fired == [True]
    assert not cd._driven_pending_path(coord, "developer").exists()
    assert "developer" not in cd._DRIVEN_RETRY  # ok cleared state


def test_clean_turn_with_stale_pending_does_not_refire(tmp_path):
    """A mid-turn event marker is stale if the completed turn already moved
    all work out of the role's claim queues. Do not spend a second turn just
    because ``driven-<role>.pending`` exists."""
    coord = _coord(tmp_path)
    (coord / "feature_dev").mkdir()
    (coord / "inbox" / "developer").mkdir(parents=True)
    (coord / "schema.yaml").write_text(
        "roles:\n"
        "  DEVELOPER:\n"
        "    claims_from: [feature_dev]\n",
        encoding="utf-8",
    )
    lock = _held_lock(coord)
    cd._driven_pending_path(coord, "developer").touch()
    fired = []

    cd._finalize_driven_turn(coord, "developer", lock, "ok", "", False,
                             refire=lambda: fired.append(True))

    assert fired == []
    assert not cd._driven_pending_path(coord, "developer").exists()


def test_clean_turn_with_pending_and_current_work_refires(tmp_path):
    coord = _coord(tmp_path)
    (coord / "feature_dev").mkdir()
    (coord / "inbox" / "developer").mkdir(parents=True)
    (coord / "feature_dev" / "0001-implement.yaml").write_text("id: x\n")
    (coord / "schema.yaml").write_text(
        "roles:\n"
        "  DEVELOPER:\n"
        "    claims_from: [feature_dev]\n",
        encoding="utf-8",
    )
    lock = _held_lock(coord)
    cd._driven_pending_path(coord, "developer").touch()
    fired = []

    cd._finalize_driven_turn(coord, "developer", lock, "ok", "", False,
                             refire=lambda: fired.append(True))

    assert fired == [True]
    assert not cd._driven_pending_path(coord, "developer").exists()


def test_failed_turn_with_pending_does_not_refire(tmp_path, monkeypatch):
    """A failed turn consumes the pending marker but does NOT re-fire — the
    retry scheduler owns re-dispatch (no double-spawn)."""
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._driven_pending_path(coord, "developer").touch()
    fired = []
    cd._finalize_driven_turn(coord, "developer", lock, "error", "boom", False,
                             refire=lambda: fired.append(True))
    assert fired == []
    assert not cd._driven_pending_path(coord, "developer").exists()
    # still recoverable via the retry scheduler
    assert cd._DRIVEN_RETRY["developer"]["attempts"] == 1


def test_refire_raise_is_contained(tmp_path):
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._driven_pending_path(coord, "developer").touch()

    def _boom():
        raise RuntimeError("re-spawn failed")

    # must not propagate
    cd._finalize_driven_turn(coord, "developer", lock, "ok", "", False,
                             refire=_boom)
    assert not lock.exists()


# ---------------- end-to-end recoverability after a failed turn ----------------


def test_failed_turn_then_retry_redispatches(tmp_path, monkeypatch):
    """After a failed finalize, _process_due_retries re-drives the role once
    the backoff is due — proving the backlog is not stranded."""
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    coord = _coord(tmp_path)
    lock = _held_lock(coord)
    cd._finalize_driven_turn(coord, "developer", lock, "error", "boom", False)
    # force the scheduled retry due now
    cd._DRIVEN_RETRY["developer"]["next_at"] = 1.0
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda p: {"windows": []})
    monkeypatch.setattr(cd, "_window_and_tool_for_role",
                        lambda d, r: ("", "claude"))
    driven = []
    monkeypatch.setattr(cd, "_maybe_drive_driven_role",
                        lambda *a, **k: driven.append(a[4]))
    cd._process_due_retries(coord, tmp_path, False)
    assert "DEVELOPER" in driven
