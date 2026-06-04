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
