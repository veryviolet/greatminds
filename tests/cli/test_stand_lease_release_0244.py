"""Tests for task 0244 (0242b / Phase 2 of 0242): stand lease +
release CLI + state transitions + FIFO queue + concurrent-lease
race resolution + inbox-info on ready.

API per PLANNER's corrected amendment (info-1779808442):
    greatminds stand lease --task <id> --worktree <path> --profile <enum>
        → returns lease_id (UUID hex)
        → state file: free → preparing(lease_id) OR queued
    greatminds stand ready --lease-id <id>  (SK-only)
        → preparing → ready; inbox-info to holder_role
    greatminds stand release --lease-id <id> --result pass|fail|partial
        → ready → free; SK pops next from queue
    greatminds stand down --reason <text>  (SK-only)
    greatminds stand up --reason <text>    (SK-only)

No prose channel between requester and SK. Information asymmetry
prevents SK rubber-stamping by input definition.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss
from greatminds.core.paths import find_canon_dir


# ---------- helpers ----------


def _invoke(args: list[str], cwd: Path, role: str = "TESTER",
            monkeypatch=None):
    """Run a stand subcommand from ``cwd`` with the given role."""
    if monkeypatch is not None:
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("GREATMINDS_ROLE", role)
    return CliRunner().invoke(stand_mod.stand, args)


# ---------- schema pin ----------


def test_schema_has_profiles_allowed() -> None:
    """0244: stand.resource.profiles_allowed enum present."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    profiles = (doc["stand"]["resource"].get("profiles_allowed") or [])
    assert "full-deploy" in profiles
    assert "vite-dev" in profiles


# ---------- stand lease (free → preparing) ----------


def test_lease_on_free_transitions_to_preparing(tmp_path, monkeypatch) -> None:
    """Happy path: state=free + lease → state=preparing, lease_id
    returned to stdout, active_lease populated with task/worktree/
    profile/holder."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    # 0271: worktree must be project_dir/.worktrees/<seq>[-slug].
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    result = _invoke(
        ["lease", "--task", "0099-test",
         "--worktree", str(wt), "--profile", "full-deploy"],
        tmp_path, monkeypatch=monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert "lease_id:" in result.output

    state = ss.read_stand_state(coord)
    assert state["state"] == "preparing"
    assert state["active_lease"]["task"] == "0099-test"
    assert state["active_lease"]["worktree"] == str(wt)
    assert state["active_lease"]["profile"] == "full-deploy"
    assert state["active_lease"]["holder_role"] == "TESTER"


def test_lease_rejects_unknown_profile(tmp_path, monkeypatch) -> None:
    """0244 schema-enforced: --profile must be in
    stand.resource.profiles_allowed. Mechanical refusal — no
    free-text profile values."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    result = _invoke(
        ["lease", "--task", "0099", "--worktree", "/tmp/wt",
         "--profile", "rogue-profile"],
        tmp_path, monkeypatch=monkeypatch,
    )
    assert result.exit_code != 0
    out = result.output + (str(result.exception) if result.exception else "")
    assert "rogue-profile" in out
    assert "profiles_allowed" in out


def test_lease_returns_unique_lease_ids(tmp_path, monkeypatch) -> None:
    """Two lease invocations → two distinct UUID lease_ids. Pin
    against accidental same-token regression."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    # 0271: per-task worktree convention; both tasks share seq=0099.
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()

    ids = []
    for task in ("0099-a", "0099-b"):
        result = runner.invoke(stand_mod.stand, [
            "lease", "--task", task, "--worktree", str(wt),
            "--profile", "full-deploy",
        ])
        assert result.exit_code == 0, result.output
        lid = result.output.strip().split("lease_id:")[1].strip()
        ids.append(lid)
    assert ids[0] != ids[1]


# ---------- queue: lease on busy → enqueued ----------


def test_lease_on_busy_enqueues(tmp_path, monkeypatch) -> None:
    """Second lease while first is preparing → enqueued; state
    stays preparing; queue length grows."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    # 0271: both tasks share seq=0099 → same worktree path is valid.
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()

    # First lease.
    runner.invoke(stand_mod.stand, [
        "lease", "--task", "0099-a", "--worktree", str(wt),
        "--profile", "full-deploy",
    ])
    # Second lease while busy.
    runner.invoke(stand_mod.stand, [
        "lease", "--task", "0099-b", "--worktree", str(wt),
        "--profile", "vite-dev",
    ])

    state = ss.read_stand_state(coord)
    assert state["state"] == "preparing"
    assert state["active_lease"]["task"] == "0099-a"
    assert len(state["queue"]) == 1
    assert state["queue"][0]["task"] == "0099-b"


# ---------- stand ready (SK-only) + inbox-info ----------


def test_ready_gated_by_marker_not_role(tmp_path, monkeypatch) -> None:
    """1.6.0: the SK-only role guard is GONE (coordd deploys; manual
    ready/down is operator-override, any role). `ready` is still gated by
    the deploy marker — so a non-SK role without a marker fails on the
    marker, not on a role check."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": {"lease_id": "abc", "task": "0099",
                         "holder_role": "TESTER"},
    }))
    result = _invoke(
        ["ready", "--lease-id", "abc"], tmp_path,
        role="DEVELOPER", monkeypatch=monkeypatch,
    )
    assert result.exit_code != 0
    out = result.output + (str(result.exception) if result.exception else "")
    assert "marker" in out and "STAND-KEEPER" not in out


def test_ready_transitions_state(tmp_path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": {"lease_id": "abc", "task": "0099",
                         "holder_role": "TESTER"},
    }))
    # 0286: stand_ready now requires the deploy marker; seed one.
    from greatminds.cli.stand_executor import deploy_marker_path
    marker = deploy_marker_path(coord, "abc")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rc=0\n", encoding="utf-8")
    # Stub the subprocess call to inbox send (would shell out in real).
    monkeypatch.setattr(stand_mod, "_file_inbox_info",
                        lambda *a, **kw: None)
    result = _invoke(["ready", "--lease-id", "abc"], tmp_path,
                     role="STAND-KEEPER", monkeypatch=monkeypatch)
    assert result.exit_code == 0
    state = ss.read_stand_state(coord)
    assert state["state"] == "ready"
    assert state["active_lease"]["ready_at"] is not None


def test_ready_fires_inbox_info_to_holder(tmp_path, monkeypatch) -> None:
    """0244 contract: SK transitions preparing→ready → inbox-info
    is filed to the lease's holder_role. The structured message
    names the lease_id + task — no prose."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": {"lease_id": "xyz-id", "task": "0099-foo",
                         "holder_role": "EXPLORER"},
    }))
    # 0286: deploy marker required.
    from greatminds.cli.stand_executor import deploy_marker_path
    marker = deploy_marker_path(coord, "xyz-id")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rc=0\n", encoding="utf-8")
    sent: list = []
    monkeypatch.setattr(
        stand_mod, "_file_inbox_info",
        lambda coord_, to_role, body, task_ref="": sent.append(
            (to_role, body, task_ref)
        ),
    )
    _invoke(["ready", "--lease-id", "xyz-id"], tmp_path,
            role="STAND-KEEPER", monkeypatch=monkeypatch)
    assert sent, "0244: inbox-info to holder_role must fire on ready"
    to_role, body, task_ref = sent[0]
    assert to_role == "EXPLORER"
    assert "xyz-id" in body
    assert task_ref == "0099-foo"


def test_ready_rejects_wrong_lease_id(tmp_path, monkeypatch) -> None:
    """SK can't accidentally `ready` a different lease than the one
    currently active."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": {"lease_id": "real-lease",
                         "task": "0099", "holder_role": "TESTER"},
    }))
    result = _invoke(["ready", "--lease-id", "wrong-lease"],
                     tmp_path, role="STAND-KEEPER", monkeypatch=monkeypatch)
    assert result.exit_code != 0


# ---------- stand release ----------


def test_release_active_lease_transitions_to_free(tmp_path,
                                                     monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": {"lease_id": "active-id", "task": "0099",
                         "holder_role": "TESTER"},
    }))
    result = _invoke(
        ["release", "--lease-id", "active-id", "--result", "pass"],
        tmp_path, role="TESTER", monkeypatch=monkeypatch,
    )
    assert result.exit_code == 0
    state = ss.read_stand_state(coord)
    assert state["state"] == "free"
    assert state["active_lease"] is None


def test_release_only_holder_may_release(tmp_path, monkeypatch) -> None:
    """0244: lease access control — only the holder role may
    release. Pin against accidental cross-role release."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": {"lease_id": "X", "task": "0099",
                         "holder_role": "TESTER"},
    }))
    result = _invoke(
        ["release", "--lease-id", "X", "--result", "pass"],
        tmp_path, role="DEVELOPER", monkeypatch=monkeypatch,
    )
    assert result.exit_code != 0


def test_release_unknown_lease_id_rejected(tmp_path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": {"lease_id": "X", "task": "0099",
                         "holder_role": "TESTER"},
    }))
    result = _invoke(
        ["release", "--lease-id", "non-existent", "--result", "pass"],
        tmp_path, role="TESTER", monkeypatch=monkeypatch,
    )
    assert result.exit_code != 0


def test_release_queued_lease_is_cancellation(tmp_path,
                                                 monkeypatch) -> None:
    """Releasing a lease_id that's in the queue (not active) =
    cancellation. State unchanged; queue entry removed."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": {"lease_id": "active-id",
                         "holder_role": "TESTER"},
        "queue": [
            {"lease_id": "queued-id", "task": "0099-b",
             "holder_role": "EXPLORER"},
        ],
    }))
    result = _invoke(
        ["release", "--lease-id", "queued-id", "--result", "pass"],
        tmp_path, role="EXPLORER", monkeypatch=monkeypatch,
    )
    assert result.exit_code == 0
    state = ss.read_stand_state(coord)
    # State unchanged; only the queue entry removed.
    assert state["state"] == "preparing"
    assert state["queue"] == []
    assert state["active_lease"]["lease_id"] == "active-id"


# ---------- stand down / up ----------


def test_down_any_role_allowed(tmp_path, monkeypatch) -> None:
    """1.6.0: no SK-only guard — any role may mark the stand down
    (operator-override; coordd marks down automatically on a failed
    deploy). The actor is recorded as whoever ran it."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    result = _invoke(["down", "--reason", "deploy failed"],
                     tmp_path, role="DEVELOPER", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert ss.read_stand_state(coord)["state"] == "down"


def test_down_sets_reason_and_state(tmp_path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    result = _invoke(["down", "--reason", "docker build failed"],
                     tmp_path, role="STAND-KEEPER",
                     monkeypatch=monkeypatch)
    assert result.exit_code == 0
    state = ss.read_stand_state(coord)
    assert state["state"] == "down"
    assert state["down_reason"] == "docker build failed"


def test_up_requires_down_state(tmp_path, monkeypatch) -> None:
    """Can't `stand up` when state isn't currently down."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    result = _invoke(["up", "--reason", "all good"],
                     tmp_path, role="STAND-KEEPER",
                     monkeypatch=monkeypatch)
    assert result.exit_code != 0


def test_up_transitions_down_to_free(tmp_path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "down", "down_reason": "fix me",
    }))
    result = _invoke(["up", "--reason", "fixed"],
                     tmp_path, role="STAND-KEEPER",
                     monkeypatch=monkeypatch)
    assert result.exit_code == 0
    state = ss.read_stand_state(coord)
    assert state["state"] == "free"
    assert state["down_reason"] is None


# ---------- concurrent lease race ----------


def test_concurrent_lease_calls_serialize_via_fcntl(tmp_path) -> None:
    """0244 fcntl pin: two threads call `stand lease` simultaneously
    on state=free. The fcntl LOCK_EX in update_stand_state
    serializes; ONE wins free→preparing, the OTHER enqueues. State
    is deterministic + valid."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    # 0271: per-task worktree convention; both tasks share seq=0099.
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    monkeypatch_env = {"GREATMINDS_ROLE": "TESTER"}

    def fire_lease(task: str, profile: str) -> None:
        import os
        old = os.environ.copy()
        os.environ.update(monkeypatch_env)
        os.chdir(tmp_path)
        try:
            runner = CliRunner()
            runner.invoke(stand_mod.stand, [
                "lease", "--task", task, "--worktree", str(wt),
                "--profile", profile,
            ])
        finally:
            os.environ.clear()
            os.environ.update(old)

    t1 = threading.Thread(target=fire_lease, args=("0099-a", "full-deploy"))
    t2 = threading.Thread(target=fire_lease, args=("0099-b", "vite-dev"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    state = ss.read_stand_state(coord)
    assert state["state"] == "preparing"
    # Exactly one of the two tasks is active; the other queued.
    active_task = state["active_lease"]["task"]
    queued = [q["task"] for q in state["queue"]]
    assert sorted([active_task] + queued) == ["0099-a", "0099-b"]
    assert len(queued) == 1


# ---------- history records transitions ----------


def test_lease_release_recorded_in_history(tmp_path, monkeypatch) -> None:
    """0244 + 0243 history pin: full lifecycle leaves recognizable
    transition entries for `stand status`'s history tail."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    # 0271: per-task worktree convention.
    wt = tmp_path / ".worktrees" / "0099"
    wt.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner = CliRunner()

    out = runner.invoke(stand_mod.stand, [
        "lease", "--task", "0099", "--worktree", str(wt),
        "--profile", "full-deploy",
    ])
    lid = out.output.strip().split("lease_id:")[1].strip()

    # 0286: deploy marker required for stand ready.
    from greatminds.cli.stand_executor import deploy_marker_path
    marker = deploy_marker_path(coord, lid)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rc=0\n", encoding="utf-8")

    monkeypatch.setenv("GREATMINDS_ROLE", "STAND-KEEPER")
    monkeypatch.setattr(stand_mod, "_file_inbox_info",
                        lambda *a, **kw: None)
    runner.invoke(stand_mod.stand, ["ready", "--lease-id", lid])

    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    runner.invoke(stand_mod.stand, [
        "release", "--lease-id", lid, "--result", "pass",
    ])

    state = ss.read_stand_state(coord)
    transitions = [(h["from"], h["to"]) for h in state["history"]]
    assert ("free", "preparing") in transitions
    assert ("preparing", "ready") in transitions
    assert ("ready", "free") in transitions
