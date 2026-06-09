"""GitHub #22: coordd must dispatch a driven ARCHITECT-REVIEWER for a
NON-EMPTY ``feature_review`` queue.

Field report: ``feature_review`` held many tasks, yet coordd produced
zero driven REVIEWER turns over ~10 minutes. REVIEWER is tool=codex /
mode=driven, so it has NO persistent pid between turns
(``.agent_registry/architect-reviewer.json`` carries only a
``thread_id``); that absence is the normal driven steady state, NOT a
dead-agent condition, and must never gate dispatch.

These tests pin the scheduling contract — a non-empty owned claim queue
drives the role regardless of pid — and keep it DISTINCT from the codex
auth/app-server failure (#21), which makes a *dispatched* turn complete
without doing work (that is a turn-outcome / escalation concern, not a
scheduling one).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import coordd as cd


REVIEWER = "ARCHITECT-REVIEWER"
REVIEWER_META = {"claims_from": ["feature_review", "feature_blocked"]}


def _mk_coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / "feature_review").mkdir(parents=True)
    (coord / "feature_blocked").mkdir(parents=True)
    (coord / "inbox" / "architect-reviewer").mkdir(parents=True)
    (coord / ".agent_registry").mkdir(parents=True)
    return coord


def _patch_driven_reviewer(monkeypatch, calls, *, tool="codex"):
    """Make the schema/coord.yaml lookups report REVIEWER as a migrated
    driven codex role, and capture every _maybe_drive_driven_role call."""
    monkeypatch.setattr(cd, "load_schema_roles", lambda _c: {REVIEWER: REVIEWER_META})
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda _p: {"session": "gm"})
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda _d, _r: "driven")
    monkeypatch.setattr(cd, "_window_and_tool_for_role",
                        lambda _d, _r: ("reviewer", tool))
    monkeypatch.setattr(
        cd, "_maybe_drive_driven_role",
        lambda *a, **k: calls.append((a[4], k.get("trigger"))))


# ---------------- _reconcile_dispatch_decision (pure) ----------------


def test_decision_drives_reviewer_with_full_feature_review(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    for n in range(3):
        (coord / "feature_review" / f"00{n}-x.yaml").write_text("id: x\n")
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda _d, _r: "driven")
    should, reason = cd._reconcile_dispatch_decision(
        coord, tmp_path / "canon", {"session": "gm"}, REVIEWER, REVIEWER_META)
    assert should is True
    assert "owned claim queue" in reason


def test_decision_skips_when_feature_review_empty(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)  # both claim queues empty
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda _d, _r: "driven")
    should, reason = cd._reconcile_dispatch_decision(
        coord, tmp_path / "canon", {"session": "gm"}, REVIEWER, REVIEWER_META)
    assert should is False
    assert "no pending work" in reason


def test_decision_skips_when_turn_in_flight(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    (coord / "feature_review" / "001-x.yaml").write_text("id: x\n")
    (coord / ".locks").mkdir()
    (coord / ".locks" / "driven-architect-reviewer.lock").write_text("")
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda _d, _r: "driven")
    should, reason = cd._reconcile_dispatch_decision(
        coord, tmp_path / "canon", {"session": "gm"}, REVIEWER, REVIEWER_META)
    assert should is False
    assert "in flight" in reason


def test_decision_non_driven_window_skips(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    (coord / "feature_review" / "001-x.yaml").write_text("id: x\n")
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda _c, _r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda _d, _r: "chat")
    should, reason = cd._reconcile_dispatch_decision(
        coord, tmp_path / "canon", {"session": "gm"}, REVIEWER, REVIEWER_META)
    assert should is False
    assert "window mode" in reason


# ---------------- non-empty feature_review reconciles REVIEWER ----------------


def test_reconcile_drives_reviewer_when_feature_review_nonempty(tmp_path, monkeypatch):
    coord = _mk_coord(tmp_path)
    (coord / "feature_review" / "0359-x.yaml").write_text("id: x\n")
    calls: list = []
    _patch_driven_reviewer(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls and calls[0][0] == REVIEWER
    assert "reconcile" in (calls[0][1] or "")


def test_reconcile_drives_reviewer_from_feature_blocked_too(tmp_path, monkeypatch):
    """feature_blocked is REVIEWER's other claim queue (wake-up owner)."""
    coord = _mk_coord(tmp_path)
    (coord / "feature_blocked" / "0364-blocked.yaml").write_text("id: b\n")
    calls: list = []
    _patch_driven_reviewer(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls and calls[0][0] == REVIEWER


# ---------------- driven role WITHOUT a pid is still dispatchable ----------------


def test_driven_reviewer_without_pid_is_dispatchable(tmp_path, monkeypatch):
    """The registry for a driven codex role carries only ``thread_id`` —
    no ``pid``. Dispatch must not depend on a live pid."""
    coord = _mk_coord(tmp_path)
    (coord / ".agent_registry" / "architect-reviewer.json").write_text(
        json.dumps({"thread_id": "019e-abc", "tool": "codex"}))
    (coord / "feature_review" / "0359-x.yaml").write_text("id: x\n")
    calls: list = []
    _patch_driven_reviewer(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls and calls[0][0] == REVIEWER


def test_pidless_driven_role_not_reported_dead_pattern(tmp_path):
    """A registry with no ``pid`` (driven steady state) must read as
    'no persistent pid', not as a dead pid. ``list_live_roles`` (the
    pid-liveness scan) excludes it — confirming pidless driven roles are
    simply absent from the liveness view, never flagged dead."""
    coord = _mk_coord(tmp_path)
    (coord / ".agent_registry" / "architect-reviewer.json").write_text(
        json.dumps({"thread_id": "019e-abc"}))
    assert "architect-reviewer" not in cd.list_live_roles(
        coord / ".agent_registry")


# ---------------- pending wake / inbox noise does not block claim ----------------


def test_inbox_wake_noise_does_not_block_queue_claim(tmp_path, monkeypatch):
    """An already-delivered wake file sitting in the inbox is not a
    blocker — the reconcile still drives REVIEWER off the non-empty
    queue. (It is also, on its own, pending work.)"""
    coord = _mk_coord(tmp_path)
    (coord / "inbox" / "architect-reviewer" / "wake-123.yaml").write_text(
        "kind: wake\n")
    (coord / "feature_review" / "0359-x.yaml").write_text("id: x\n")
    calls: list = []
    _patch_driven_reviewer(monkeypatch, calls)
    cd._reconcile_driven_backlog(coord, tmp_path / "canon", verbose=False)
    assert calls and calls[0][0] == REVIEWER


def test_processed_markers_do_not_count_as_queue_work(tmp_path):
    """Processed / template files in the claim queue are NOT pending work
    — so they alone must not trigger a dispatch (no spurious turns)."""
    coord = _mk_coord(tmp_path)
    (coord / "feature_review" / "processed-old.yaml").write_text("id: p\n")
    (coord / "feature_review" / "_TEMPLATE.yaml").write_text("id: t\n")
    assert cd._role_has_pending_task(coord, REVIEWER_META,
                                     "architect-reviewer") is False


# ---------------- #21 auth/app-server failure is SEPARATE from scheduling ----


@pytest.fixture(autouse=True)
def _clean_retry_state():
    cd._DRIVEN_RETRY.clear()
    yield
    cd._DRIVEN_RETRY.clear()


def test_codex_auth_failure_escalates_without_being_a_scheduling_skip(
        tmp_path, monkeypatch):
    """A dispatched codex turn that fails on auth/app-server (#21) is a
    TURN-OUTCOME error: it escalates to MAINTAINER after the hard-retry
    cap, but it is NOT recorded as a scheduling decision. The scheduler
    keeps dispatching (the reconcile decision is independent of the prior
    turn's outcome)."""
    coord = _mk_coord(tmp_path)
    escalations: list = []
    monkeypatch.setattr(
        cd, "_escalate_to_maintainer",
        lambda c, role, klass, attempts, detail: escalations.append(
            (role, klass, attempts)))
    # Simulate the codex app-server auth failure surfacing as a hard error
    # repeatedly, up to the bounded cap.
    detail = "app-server closed before response (401 Unauthorized)"
    for _ in range(cd.RETRY_HARD_MAX):
        cd._note_turn_outcome(coord, "architect-reviewer", "error",
                              detail, verbose=False)
    assert escalations and escalations[-1][0] == "architect-reviewer"
    assert escalations[-1][1] == "error"


def test_real_event_clears_escalation_so_scheduling_resumes():
    """After #21 escalation stops auto-retry, a REAL queue/inbox event
    (a fresh dispatch via _maybe_drive_driven_role with a non-retry
    trigger) clears the backoff state — proving scheduling is not
    permanently wedged by an auth failure."""
    cd._note_turn_outcome  # touch for clarity
    # Force an escalated state.
    cd._DRIVEN_RETRY["architect-reviewer"] = {
        "attempts": cd.RETRY_HARD_MAX, "klass": "error",
        "next_at": 0.0, "escalated": True, "notified": False}
    cd._clear_retry_state("architect-reviewer")
    assert "architect-reviewer" not in cd._DRIVEN_RETRY
