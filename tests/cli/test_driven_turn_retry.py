"""1.6.2: driven-turn outcome classification + retry / escalation.

A driven turn that FAILS (rate-limit / error / timeout) is retried —
rate-limit effectively forever with backoff, other errors a bounded
number of times then escalated to MAINTAINER and auto-retry stops. A
CLEAN completion is never retried (the agent may legitimately have had
nothing to do).
"""
from __future__ import annotations

import json

import pytest

from greatminds.cli import coordd as cd


@pytest.fixture(autouse=True)
def _clean_retry_state():
    cd._DRIVEN_RETRY.clear()
    yield
    cd._DRIVEN_RETRY.clear()


# ---------------- classification ----------------


def test_classify_clean_success_is_ok():
    out = json.dumps({"is_error": False, "session_id": "s"})
    assert cd._classify_turn_outcome(0, out) == "ok"


def test_classify_429_is_rate_limit():
    out = json.dumps({"is_error": True, "api_error_status": 429,
                      "result": "Server is temporarily limiting requests"})
    assert cd._classify_turn_outcome(0, out) == "rate_limit"


def test_classify_529_overloaded_is_rate_limit():
    out = json.dumps({"is_error": True, "api_error_status": 529,
                      "result": "Overloaded"})
    assert cd._classify_turn_outcome(0, out) == "rate_limit"


def test_classify_execution_error_is_error():
    out = json.dumps({"is_error": True, "subtype": "error_during_execution",
                      "result": "boom"})
    assert cd._classify_turn_outcome(0, out) == "error"


def test_classify_nonzero_rc_no_json_is_error():
    assert cd._classify_turn_outcome(1, "not json at all") == "error"


def test_classify_timeout_flag():
    assert cd._classify_turn_outcome(0, "", timed_out=True) == "timeout"


# ---------------- backoff ----------------


def test_retry_delay_rate_limit_first_and_cap():
    assert cd._retry_delay("rate_limit", 1) == cd.RETRY_RL_BASE_SEC
    assert cd._retry_delay("rate_limit", 50) == cd.RETRY_RL_CAP_SEC


def test_retry_delay_hard_first_and_cap():
    assert cd._retry_delay("error", 1) == cd.RETRY_HARD_BASE_SEC
    assert cd._retry_delay("error", 50) == cd.RETRY_HARD_CAP_SEC


# ---------------- outcome bookkeeping ----------------


def test_ok_clears_state(tmp_path):
    (tmp_path / ".locks").mkdir()
    cd._driven_retry_path(tmp_path, "developer").write_text("{}")
    cd._DRIVEN_RETRY["developer"] = {"attempts": 2, "klass": "error",
                                     "next_at": 0.0, "escalated": False,
                                     "notified": False}
    cd._note_turn_outcome(tmp_path, "developer", "ok", "", False)
    assert "developer" not in cd._DRIVEN_RETRY
    assert not cd._driven_retry_path(tmp_path, "developer").exists()


def test_rate_limit_schedules_and_never_escalates(tmp_path, monkeypatch):
    esc = []
    monkeypatch.setattr(cd, "_escalate_to_maintainer",
                        lambda *a, **k: esc.append(a))
    for _ in range(8):
        cd._note_turn_outcome(tmp_path, "developer", "rate_limit", "429",
                              False)
    st = cd._DRIVEN_RETRY["developer"]
    assert st["escalated"] is False
    assert st["attempts"] == 8
    assert st["next_at"] > 0
    status = json.loads(
        cd._driven_retry_path(tmp_path, "developer").read_text())
    assert status["klass"] == "rate_limit"
    assert status["attempts"] == 8
    assert status["next_at_epoch"] > 0


def test_hard_error_escalates_and_stops(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cd, "_escalate_to_maintainer",
                        lambda c, r, k, a, d: calls.append((r, k, a)))
    last = None
    for _ in range(cd.RETRY_HARD_MAX):
        last = cd._note_turn_outcome(tmp_path, "tester", "error", "boom",
                                     False)
    assert last["escalated"] is True
    assert calls and calls[-1][0] == "tester"
    assert cd._DRIVEN_RETRY["tester"]["escalated"] is True
    status = json.loads(cd._driven_retry_path(tmp_path, "tester").read_text())
    assert status["escalated"] is True
    assert status["detail"] == "boom"


def test_class_change_resets_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "_escalate_to_maintainer", lambda *a, **k: None)
    cd._note_turn_outcome(tmp_path, "developer", "error", "boom", False)
    cd._note_turn_outcome(tmp_path, "developer", "rate_limit", "429", False)
    # switched class → counter restarted at 1, not escalated
    st = cd._DRIVEN_RETRY["developer"]
    assert st["klass"] == "rate_limit" and st["attempts"] == 1


# ---------------- scheduler ----------------


def _coord(tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    return coord


def test_process_due_retries_dispatches_when_due(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    cd._DRIVEN_RETRY["developer"] = {"attempts": 1, "klass": "rate_limit",
                                     "next_at": 1.0, "escalated": False,
                                     "notified": False}
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda p: {"windows": []})
    monkeypatch.setattr(cd, "_window_and_tool_for_role",
                        lambda d, r: ("", "claude"))
    driven = []
    monkeypatch.setattr(cd, "_maybe_drive_driven_role",
                        lambda *a, **k: driven.append(a[4]))
    cd._process_due_retries(coord, tmp_path, False)
    assert "DEVELOPER" in driven


def test_process_due_retries_skips_escalated(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    cd._DRIVEN_RETRY["tester"] = {"attempts": 3, "klass": "error",
                                  "next_at": 1.0, "escalated": True,
                                  "notified": False}
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda p: {"windows": []})
    driven = []
    monkeypatch.setattr(cd, "_maybe_drive_driven_role",
                        lambda *a, **k: driven.append(a))
    cd._process_due_retries(coord, tmp_path, False)
    assert driven == []


def test_process_due_retries_skips_when_lock_held(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    cd._driven_run_lock_path(coord, "developer").touch()
    cd._DRIVEN_RETRY["developer"] = {"attempts": 1, "klass": "rate_limit",
                                     "next_at": 1.0, "escalated": False,
                                     "notified": False}
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda p: {"windows": []})
    monkeypatch.setattr(cd, "_window_and_tool_for_role",
                        lambda d, r: ("", "claude"))
    driven = []
    monkeypatch.setattr(cd, "_maybe_drive_driven_role",
                        lambda *a, **k: driven.append(a))
    cd._process_due_retries(coord, tmp_path, False)
    assert driven == []


def test_event_dispatch_clears_retry_state(tmp_path, monkeypatch):
    """A real (non-retry) dispatch clears prior backoff/escalation."""
    coord = _coord(tmp_path)
    cd._driven_retry_path(coord, "developer").write_text("{}")
    cd._DRIVEN_RETRY["developer"] = {"attempts": 3, "klass": "error",
                                     "next_at": 1.0, "escalated": True,
                                     "notified": False}
    # lifecycle/window say driven so the clear branch runs; tool unknown so
    # it returns before spawning anything real.
    monkeypatch.setattr(cd, "_lifecycle_for_role", lambda c, r: "driven")
    monkeypatch.setattr(cd, "_window_mode_for_role", lambda d, r: "driven")
    cd._maybe_drive_driven_role(coord, tmp_path, {"windows": []},
                                ("", "unknown-tool"), "DEVELOPER", False,
                                trigger=" (startup-reconcile)")
    assert "developer" not in cd._DRIVEN_RETRY
    assert not cd._driven_retry_path(coord, "developer").exists()


def test_process_due_retries_restores_persisted_retry(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    cd._driven_retry_path(coord, "developer").write_text(json.dumps({
        "role": "DEVELOPER",
        "klass": "rate_limit",
        "attempts": 2,
        "next_at_epoch": 1.0,
        "escalated": False,
        "notified": False,
    }))
    monkeypatch.setattr(cd, "_read_coord_yaml", lambda p: {"windows": []})
    monkeypatch.setattr(cd, "_window_and_tool_for_role",
                        lambda d, r: ("", "claude"))
    driven = []
    monkeypatch.setattr(cd, "_maybe_drive_driven_role",
                        lambda *a, **k: driven.append(a[4]))
    cd._process_due_retries(coord, tmp_path, False)
    assert "DEVELOPER" in driven
    assert cd._DRIVEN_RETRY["developer"]["klass"] == "rate_limit"
