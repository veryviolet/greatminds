"""coordd startup reconcile: a task already sitting in a queue when
coordd (re)starts must be driven — coordd is otherwise inotify-reactive,
so a daemon restart (e.g. after `update`) would otherwise strand it.
"""
from __future__ import annotations

from pathlib import Path

from greatminds.cli import coordd as cd


def _mk_coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / "feature_test").mkdir(parents=True)
    (coord / "inbox" / "tester").mkdir(parents=True)
    return coord


# ---------- _role_has_pending_task ----------


def test_pending_task_detects_yaml_in_claim_queue(tmp_path):
    coord = _mk_coord(tmp_path)
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    assert cd._role_has_pending_task(
        coord, {"claims_from": ["feature_test"]}, "tester") is True


def test_pending_task_ignores_template_and_processed(tmp_path):
    coord = _mk_coord(tmp_path)
    (coord / "feature_test" / "_TEMPLATE.yaml").write_text("id: t\n")
    (coord / "feature_test" / "processed-old.yaml").write_text("id: p\n")
    assert cd._role_has_pending_task(
        coord, {"claims_from": ["feature_test"]}, "tester") is False


def test_pending_task_detects_inbox_message(tmp_path):
    coord = _mk_coord(tmp_path)
    (coord / "inbox" / "tester" / "info-1-a.yaml").write_text("k: v\n")
    assert cd._role_has_pending_task(coord, {"claims_from": []}, "tester") is True


def test_pending_task_ignores_processed_inbox(tmp_path):
    """The bug: a role with ONLY processed (acked) inbox messages and empty
    claim queues was counted as pending (old processed_ok=True), so coordd
    re-drove it every reconcile forever. A processed marker is handled work,
    not pending."""
    coord = _mk_coord(tmp_path)
    (coord / "inbox" / "tester" / "processed-wake-1-feature_test.md").write_text(
        "done\n")
    assert cd._role_has_pending_task(coord, {"claims_from": []}, "tester") is False
    # an UNPROCESSED message alongside the processed one is still pending
    (coord / "inbox" / "tester" / "ask-2-b.yaml").write_text("k: v\n")
    assert cd._role_has_pending_task(coord, {"claims_from": []}, "tester") is True


def test_pending_signature_changes_with_backlog(tmp_path):
    """The signature is the SET of actionable files, so distinct backlogs
    produce distinct signatures (drives the periodic de-dup)."""
    coord = _mk_coord(tmp_path)
    meta = {"claims_from": ["feature_test"]}
    assert cd._role_pending_signature(coord, meta, "tester") == frozenset()
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    sig1 = cd._role_pending_signature(coord, meta, "tester")
    (coord / "feature_test" / "0002-verify.yaml").write_text("id: y\n")
    sig2 = cd._role_pending_signature(coord, meta, "tester")
    assert sig1 and sig2 and sig1 != sig2 and sig1 < sig2


# ---------- _reconcile_driven_backlog ----------


def _patch_driven_tester(monkeypatch, calls):
    monkeypatch.setattr(cd, "load_schema_roles",
                        lambda _c: {"TESTER": {"claims_from": ["feature_test"]}})
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda _p: {"session": "x"})
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda _d, _r: "driven")
    monkeypatch.setattr(cd, "_window_and_tool_for_role",
                        lambda _d, _r: ("tester", "claude"))
    monkeypatch.setattr(
        cd, "_maybe_drive_driven_role",
        lambda *a, **k: calls.append((a[4], k.get("trigger"))))


def test_reconcile_drives_role_with_pending_task(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    calls: list = []
    _patch_driven_tester(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls and calls[0][0] == "TESTER"
    assert "reconcile" in (calls[0][1] or "")


def test_reconcile_skips_when_no_pending(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)  # feature_test empty
    calls: list = []
    _patch_driven_tester(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls == []


def test_reconcile_skips_when_turn_in_flight(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    (coord / ".locks").mkdir()
    (coord / ".locks" / "driven-tester.lock").write_text("")  # mid-turn
    calls: list = []
    _patch_driven_tester(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls == []


# ---------- periodic reconcile de-dup (no timer polling) ----------


def test_periodic_reconcile_drives_once_per_backlog(tmp_path, monkeypatch):
    """With a ``seen`` dict (the periodic path), an UNCHANGED backlog is
    driven once, not every tick — the idle-role timer-poll bug. A new task
    (changed backlog) re-drives; clearing the backlog then re-driving an
    identical one drives again."""
    coord = _mk_coord(tmp_path)
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    calls: list = []
    _patch_driven_tester(monkeypatch, calls)
    seen: dict = {}
    canon = tmp_path / "canon"

    # tick 1: pending work → drive
    cd._reconcile_driven_backlog(coord, canon, verbose=False, seen=seen)
    assert len(calls) == 1
    # tick 2: same backlog → NO re-drive
    cd._reconcile_driven_backlog(coord, canon, verbose=False, seen=seen)
    assert len(calls) == 1, "unchanged backlog must not re-drive every cycle"

    # a new task changes the backlog → drive again
    (coord / "feature_test" / "0002-verify.yaml").write_text("id: y\n")
    cd._reconcile_driven_backlog(coord, canon, verbose=False, seen=seen)
    assert len(calls) == 2

    # backlog clears → seen entry reset; an identical backlog later re-drives
    (coord / "feature_test" / "0001-verify.yaml").unlink()
    (coord / "feature_test" / "0002-verify.yaml").unlink()
    cd._reconcile_driven_backlog(coord, canon, verbose=False, seen=seen)
    assert len(calls) == 2 and "tester" not in seen
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    cd._reconcile_driven_backlog(coord, canon, verbose=False, seen=seen)
    assert len(calls) == 3


def test_periodic_reconcile_does_not_drive_processed_only_inbox(
        tmp_path, monkeypatch):
    """End-to-end of the compounding bug: a role whose ONLY inbox content is
    processed markers (and empty claim queues) is never driven by the
    periodic reconcile."""
    coord = _mk_coord(tmp_path)
    (coord / "inbox" / "tester" / "processed-wake-9-feature_test.md").write_text(
        "done\n")
    calls: list = []
    _patch_driven_tester(monkeypatch, calls)
    seen: dict = {}
    cd._reconcile_driven_backlog(coord, tmp_path / "canon",
                                 verbose=False, seen=seen)
    assert calls == [] and seen == {}


# ---------- _clear_stale_driven_locks ----------


def test_clear_stale_driven_locks_removes_lock_and_pending(tmp_path):
    coord = _mk_coord(tmp_path)
    locks = coord / ".locks"
    locks.mkdir()
    (locks / "driven-tester.lock").write_text("")
    (locks / "driven-tester.pending").write_text("")
    (locks / "driven-developer.lock").write_text("")
    (locks / "keep.other").write_text("")     # unrelated → untouched

    n = cd._clear_stale_driven_locks(coord, verbose=False)

    assert n == 3
    assert not (locks / "driven-tester.lock").exists()
    assert not (locks / "driven-tester.pending").exists()
    assert not (locks / "driven-developer.lock").exists()
    assert (locks / "keep.other").exists()


def test_clear_then_reconcile_drives_role_despite_prior_lock(tmp_path, monkeypatch):
    """A stale lock from a killed coordd must NOT permanently strand the
    role: clearing it first lets the reconcile drive the pending task."""
    coord = _mk_coord(tmp_path)
    (coord / "feature_test" / "0001-verify.yaml").write_text("id: x\n")
    locks = coord / ".locks"
    locks.mkdir()
    (locks / "driven-tester.lock").write_text("")   # stale, from a killed coordd
    calls: list = []
    _patch_driven_tester(monkeypatch, calls)

    cd._clear_stale_driven_locks(coord, verbose=False)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)

    assert calls and calls[0][0] == "TESTER"
