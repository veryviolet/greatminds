"""Tests for task 0339 (DOD2): ``greatminds agent status [ROLE]``
process diagnostics replacing raw cat of .agent_registry.

Reports pid / alive / session_id / venv / heartbeat-age / input_sock per
role. ``alive`` and ``venv`` are derived from the OS at call time so a
stale registry whose pid is dead reports alive:false — the point of the
command.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import agent as agent_mod
from greatminds.cli.coordd import REGISTRY_DIR


def _coord(tmp_path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / REGISTRY_DIR).mkdir(parents=True)
    return coord


def _write_reg(coord, role_lower, **fields):
    payload = {"role": role_lower.upper(), "tool": "claude"}
    payload.update(fields)
    (coord / REGISTRY_DIR / f"{role_lower}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _touch_heartbeat(coord, role_lower, age_seconds=0.0):
    hb = coord / f"heartbeat.{role_lower}"
    hb.write_text("", encoding="utf-8")
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(hb, (past, past))


def _run(args, cwd):
    from greatminds.cli import main as main_mod
    return CliRunner().invoke(main_mod.cli, args,
                              catch_exceptions=True,
                              # find_coord_dir walks up from cwd
                              )


# ---------- collect_agent_status (core) ----------


def test_alive_pid_reported_alive(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid(),
               session_id="sess-1",
               input_sock=str(coord / REGISTRY_DIR / "developer.sock"))
    rec = agent_mod.collect_agent_status(coord, "DEVELOPER")
    assert rec["registered"] is True
    assert rec["pid"] == os.getpid()
    assert rec["alive"] is True
    assert rec["session_id"] == "sess-1"
    # this test process has a VIRTUAL_ENV (the build venv) → reported
    assert rec["venv"] == os.environ.get("VIRTUAL_ENV") or rec["venv"] is None


def test_dead_pid_reported_dead(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "tester", pid=2147480000, session_id="x")
    rec = agent_mod.collect_agent_status(coord, "TESTER")
    assert rec["registered"] is True
    assert rec["alive"] is False
    assert rec["venv"] is None  # dead → no /proc environ


def test_unregistered_role_stable_shape(tmp_path):
    coord = _coord(tmp_path)
    rec = agent_mod.collect_agent_status(coord, "READER")
    assert rec["registered"] is False
    assert rec["pid"] is None
    assert rec["alive"] is False
    assert rec["session_id"] is None
    assert rec["heartbeat_age"] is None
    assert rec["input_sock"] is None


def test_heartbeat_age_reported(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid())
    _touch_heartbeat(coord, "developer", age_seconds=42)
    rec = agent_mod.collect_agent_status(coord, "developer")
    assert rec["heartbeat_age"] is not None
    assert 40 <= rec["heartbeat_age"] <= 60


def test_input_sock_presence(tmp_path):
    coord = _coord(tmp_path)
    sock = coord / REGISTRY_DIR / "developer.sock"
    _write_reg(coord, "developer", pid=os.getpid(), input_sock=str(sock))
    # path recorded but file absent → not present
    rec = agent_mod.collect_agent_status(coord, "developer")
    assert rec["input_sock"] == str(sock)
    assert rec["input_sock_present"] is False
    sock.write_text("", encoding="utf-8")
    rec2 = agent_mod.collect_agent_status(coord, "developer")
    assert rec2["input_sock_present"] is True


def test_venv_of_pid_for_self(tmp_path):
    # /proc-based; on Linux this process's VIRTUAL_ENV (if set) round-trips
    val = agent_mod._venv_of_pid(os.getpid())
    assert val == (os.environ.get("VIRTUAL_ENV") or None) or val is None
    assert agent_mod._venv_of_pid(2147480000) is None
    assert agent_mod._venv_of_pid("not-a-pid") is None


# ---------- CLI surface ----------


def test_cli_status_all_roles(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid(), session_id="d1")
    _write_reg(coord, "tester", pid=2147480000, session_id="t1")
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status"], coord.parent)
    assert res.exit_code == 0, res.output
    assert "DEVELOPER" in res.output
    assert "TESTER" in res.output
    assert "alive" in res.output
    assert "DEAD" in res.output


def test_cli_status_single_role(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid(), session_id="d1")
    _write_reg(coord, "tester", pid=2147480000)
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "DEVELOPER"], coord.parent)
    assert res.exit_code == 0, res.output
    assert "DEVELOPER" in res.output
    assert "TESTER" not in res.output


def test_cli_status_json(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid(), session_id="d1",
               input_sock=str(coord / REGISTRY_DIR / "developer.sock"))
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "--json"], coord.parent)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert isinstance(data, list) and len(data) == 1
    rec = data[0]
    assert rec["role"] == "DEVELOPER"
    assert rec["alive"] is True
    assert set(rec) >= {"pid", "alive", "session_id", "venv",
                        "heartbeat_age", "input_sock"}


def test_cli_status_no_agents(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status"], coord.parent)
    assert res.exit_code == 0, res.output
    assert "no registered agents" in res.output.lower()
    res_json = _run(["agent", "status", "--json"], coord.parent)
    assert res_json.output.strip() == "[]"


def test_cli_status_unregistered_single_role(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid())
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "READER"], coord.parent)
    assert res.exit_code == 0, res.output
    assert "not registered" in res.output.lower()
