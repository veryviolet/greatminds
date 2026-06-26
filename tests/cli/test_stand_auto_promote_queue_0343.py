"""Tests for task 0343: freeing the singleton stand must auto-promote
the head FIFO-queued lease.

`stand release` (active→free) and `stand up` (down→free) used to leave a
non-empty queue untouched — the docstring promised "pops the next FIFO
queue entry" but the mutator never did, so queued validations stalled at
state=free until someone manually re-leased. The fix promotes the head
entry (grant it, drop from queue, free→preparing) so SK deploys it on its
next tick. An empty queue must still settle at free.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss


def _coord(tmp_path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / "stand_requests").mkdir(parents=True)
    (coord / ".stand").mkdir()
    return coord


def _q_entry(lease_id, task, holder="TESTER"):
    return {
        "lease_id": lease_id, "task": task,
        "worktree": f"/wt/{task}", "profile": "full-deploy",
        "holder_role": holder, "ttl_seconds": 14400,
        "enqueued_at": ss.now_iso(),
    }


def _active(lease_id, task, holder="TESTER"):
    e = _q_entry(lease_id, task, holder)
    e["granted_at"] = ss.now_iso()
    e["ready_at"] = None
    return e


# ---------- the helper (FSM core) ----------


def test_promote_head_on_free_activates_head(tmp_path):
    state = {
        "state": "free", "active_lease": None,
        "queue": [_q_entry("l1", "0401"), _q_entry("l2", "0402")],
        "history": [],
    }
    promoted = ss.promote_head_on_free(state, "STAND-KEEPER")
    assert promoted == "l1"
    assert state["state"] == "preparing"
    assert state["active_lease"]["lease_id"] == "l1"
    assert state["active_lease"].get("granted_at")
    assert state["active_lease"].get("ready_at") is None
    # head consumed, tail preserved in order
    assert [q["lease_id"] for q in state["queue"]] == ["l2"]


def test_promote_head_on_free_skips_poisoned_duplicate(tmp_path):
    state = {
        "state": "free", "active_lease": None,
        "queue": [
            _q_entry("bad-again", "0400"),
            _q_entry("good-next", "0401"),
        ],
        "history": [],
    }
    state["queue"][0]["worktree"] = "/wt/0400"
    promoted = ss.promote_head_on_free(
        state, "COORDD",
        poison={"task": "0400", "profile": "full-deploy",
                "worktree": "/wt/0400"},
    )

    assert promoted == "good-next"
    assert state["active_lease"]["lease_id"] == "good-next"
    assert [q["lease_id"] for q in state["queue"]] == ["bad-again"]


def test_promote_head_on_free_noop_when_queue_empty(tmp_path):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "free", "active_lease": None, "queue": [],
    }))
    res = ss.promote_head_on_free(ss.read_stand_state(coord), "STAND-KEEPER")
    assert res is None


def test_promote_head_on_free_noop_when_not_free(tmp_path):
    state = {"state": "preparing", "active_lease": _active("a", "0400"),
             "queue": [_q_entry("l1", "0401")]}
    res = ss.promote_head_on_free(state, "STAND-KEEPER")
    assert res is None
    # untouched
    assert state["active_lease"]["lease_id"] == "a"
    assert [q["lease_id"] for q in state["queue"]] == ["l1"]


# ---------- release auto-promotes ----------


def _run(env_role, args):
    return CliRunner().invoke(
        stand_mod.stand, args, env={"GREATMINDS_ROLE": env_role},
        catch_exceptions=True)


def test_release_with_queue_auto_promotes_head(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": _active("active1", "0400", holder="TESTER"),
        "queue": [_q_entry("queued2", "0401"), _q_entry("queued3", "0402")],
    }))
    monkeypatch.chdir(coord.parent)
    res = _run("TESTER", ["release", "--lease-id", "active1",
                          "--result", "pass"])
    assert res.exit_code == 0, res.output
    st = ss.read_stand_state(coord)
    # head of queue is now the active lease; stand left free → preparing
    assert st["state"] == "preparing"
    assert st["active_lease"]["lease_id"] == "queued2"
    assert [q["lease_id"] for q in st["queue"]] == ["queued3"]
    assert "auto-promoted" in res.output


def test_release_with_empty_queue_settles_free(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": _active("active1", "0400", holder="TESTER"),
        "queue": [],
    }))
    monkeypatch.chdir(coord.parent)
    res = _run("TESTER", ["release", "--lease-id", "active1",
                          "--result", "pass"])
    assert res.exit_code == 0, res.output
    st = ss.read_stand_state(coord)
    assert st["state"] == "free"
    assert st["active_lease"] is None
    assert st["queue"] == []
    assert "auto-promoted" not in res.output


# ---------- up auto-promotes ----------


def test_up_with_queue_auto_promotes_head(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "down", "down_reason": "infra blip", "active_lease": None,
        "queue": [_q_entry("queued1", "0401"), _q_entry("queued2", "0402")],
    }))
    monkeypatch.chdir(coord.parent)
    res = _run("MAINTAINER", ["up", "--reason", "fixed"])
    assert res.exit_code == 0, res.output
    st = ss.read_stand_state(coord)
    assert st["state"] == "preparing"
    assert st["active_lease"]["lease_id"] == "queued1"
    assert st["active_lease"].get("granted_at")
    assert st["down_reason"] is None
    assert [q["lease_id"] for q in st["queue"]] == ["queued2"]
    assert "auto-promoted" in res.output


def test_up_with_empty_queue_settles_free(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "down", "down_reason": "infra blip",
        "active_lease": None, "queue": [],
    }))
    monkeypatch.chdir(coord.parent)
    res = _run("MAINTAINER", ["up", "--reason", "fixed"])
    assert res.exit_code == 0, res.output
    st = ss.read_stand_state(coord)
    assert st["state"] == "free"
    assert st["active_lease"] is None


# ---------- transition history records the auto-promotion ----------


def test_promotion_recorded_in_history(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": _active("active1", "0400", holder="TESTER"),
        "queue": [_q_entry("queued2", "0401")],
    }))
    monkeypatch.chdir(coord.parent)
    _run("TESTER", ["release", "--lease-id", "active1", "--result", "pass"])
    st = ss.read_stand_state(coord)
    transitions = [(h["from"], h["to"]) for h in st["history"]]
    # ready→free (release) immediately followed by free→preparing (promote)
    assert ("ready", "free") in transitions
    assert ("free", "preparing") in transitions
    promote = [h for h in st["history"]
               if h["from"] == "free" and h["to"] == "preparing"][-1]
    assert promote["lease_id"] == "queued2"
