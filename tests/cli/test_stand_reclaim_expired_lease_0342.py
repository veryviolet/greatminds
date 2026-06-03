"""Tests for task 0342: an EXPIRED lease whose holder is dead/absent
must be reclaimable so the singleton stand can't be permanently locked.

`stand release` is holder-only and there was no reaper, so a crashed
holder past its ttl_seconds permanently locked the stand. `stand
reclaim` (STAND-KEEPER / ARCHITECT-PLANNER only) frees a lease that is
BOTH past its TTL AND held by a dead/absent agent — and refuses a live,
in-TTL lease or a still-alive holder.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss


def _iso(delta_seconds: int) -> str:
    return (datetime.now(tz=timezone.utc)
            + timedelta(seconds=delta_seconds)).isoformat(timespec="seconds")


def _project(tmp_path, monkeypatch, *, holder="EXPLORER",
             ttl=14400, granted_delta=-20000, holder_pid=None):
    """Build a leased-stand project. granted_delta<-ttl ⇒ expired.
    holder_pid None ⇒ no registry entry (absent holder)."""
    proj = tmp_path / "proj"
    coord = proj / "coordination"
    (coord / "stand_requests").mkdir(parents=True)
    (coord / ".stand").mkdir()
    state = ss._empty_state()
    state["state"] = "preparing"
    state["active_lease"] = {
        "lease_id": "deadbeef", "task": "0329", "worktree": str(proj),
        "profile": "full-deploy", "holder_role": holder,
        "ttl_seconds": ttl, "granted_at": _iso(granted_delta),
    }
    ss.update_stand_state(coord, lambda s: s.update(state))
    if holder_pid is not None:
        reg = coord / ".agent_registry"
        reg.mkdir(exist_ok=True)
        (reg / f"{holder.lower()}.json").write_text(
            json.dumps({"role": holder, "pid": holder_pid}),
            encoding="utf-8")
    monkeypatch.chdir(proj)
    return proj, coord


def _run(env_role, args):
    runner = CliRunner()
    return runner.invoke(stand_mod.stand, args,
                         env={"GREATMINDS_ROLE": env_role},
                         catch_exceptions=True)


def _state(coord):
    return ss.read_stand_state(coord)


# ---------- happy path: expired + dead/absent holder → free ----------


def test_reclaim_expired_absent_holder_frees_stand(tmp_path, monkeypatch):
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-20000,
                           holder_pid=None)  # no registry → absent
    res = _run("STAND-KEEPER", ["reclaim"])
    assert res.exit_code == 0, res.output
    st = _state(coord)
    assert st["state"] == "free"
    assert st["active_lease"] is None


def test_reclaim_expired_dead_pid_frees_stand(tmp_path, monkeypatch):
    # a pid that is almost certainly not alive
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-20000,
                           holder_pid=2147480000)
    res = _run("ARCHITECT-PLANNER", ["reclaim", "--lease-id", "deadbeef"])
    assert res.exit_code == 0, res.output
    assert _state(coord)["state"] == "free"


# ---------- must NOT clobber a live / in-TTL lease ----------


def test_reclaim_refuses_in_ttl_lease(tmp_path, monkeypatch):
    # granted recently, ttl large ⇒ NOT expired
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-60,
                           ttl=14400, holder_pid=None)
    res = _run("STAND-KEEPER", ["reclaim"])
    assert res.exit_code != 0
    out = (res.output or "") + str(res.exception or "")
    assert "within its TTL" in out
    assert _state(coord)["state"] == "preparing"  # untouched
    assert _state(coord)["active_lease"] is not None


def test_reclaim_refuses_when_holder_alive(tmp_path, monkeypatch):
    # expired BUT holder pid is THIS process → alive
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-20000,
                           holder_pid=os.getpid())
    res = _run("STAND-KEEPER", ["reclaim"])
    assert res.exit_code != 0
    out = (res.output or "") + str(res.exception or "")
    assert "still alive" in out
    assert _state(coord)["active_lease"] is not None  # untouched


# ---------- role gate + no-lease ----------


def test_reclaim_role_gated(tmp_path, monkeypatch):
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-20000)
    res = _run("DEVELOPER", ["reclaim"])
    assert res.exit_code != 0
    out = (res.output or "") + str(res.exception or "")
    assert "MAINTAINER" in out and "reclaim" in out
    assert _state(coord)["active_lease"] is not None


def test_reclaim_allowed_for_maintainer(tmp_path, monkeypatch):
    """MAINTAINER's contract carries the stale-lease reclaim recovery
    duty, so the CLI gate must allow it (reconciles the canon↔code
    mismatch found in the 1.5.0 live fleet run)."""
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-20000,
                           holder_pid=None)  # expired + absent holder
    res = _run("MAINTAINER", ["reclaim"])
    assert res.exit_code == 0, res.output
    st = _state(coord)
    assert st["state"] == "free"
    assert st["active_lease"] is None


def test_reclaim_no_active_lease_errors(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "coordination" / "stand_requests").mkdir(parents=True)
    (proj / "coordination" / ".stand").mkdir()
    monkeypatch.chdir(proj)
    res = _run("STAND-KEEPER", ["reclaim"])
    assert res.exit_code != 0
    out = (res.output or "") + str(res.exception or "")
    assert "no active lease" in out


def test_reclaim_wrong_lease_id_errors(tmp_path, monkeypatch):
    proj, coord = _project(tmp_path, monkeypatch, granted_delta=-20000)
    res = _run("STAND-KEEPER", ["reclaim", "--lease-id", "nope"])
    assert res.exit_code != 0
    out = (res.output or "") + str(res.exception or "")
    assert "not the active lease" in out


# ---------- helper unit ----------


def test_lease_expired_helper(tmp_path):
    assert stand_mod._lease_expired(
        {"granted_at": _iso(-20000), "ttl_seconds": 14400}) is True
    assert stand_mod._lease_expired(
        {"granted_at": _iso(-60), "ttl_seconds": 14400}) is False
    # missing fields → conservative False (never steal)
    assert stand_mod._lease_expired({"ttl_seconds": 1}) is False


def test_holder_alive_helper(tmp_path):
    coord = tmp_path / "coordination"
    reg = coord / ".agent_registry"
    reg.mkdir(parents=True)
    (reg / "explorer.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8")
    assert stand_mod._holder_alive(coord, "EXPLORER") is True
    (reg / "tester.json").write_text(
        json.dumps({"pid": 2147480000}), encoding="utf-8")
    assert stand_mod._holder_alive(coord, "TESTER") is False
    # absent registry → not alive
    assert stand_mod._holder_alive(coord, "READER") is False
