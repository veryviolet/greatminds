"""Task 0375: driven Codex roles use the SINGLE machine Codex login.

Codex 0.137 stores+refreshes the ChatGPT auth in ``$CODEX_HOME/auth.json``
with single-use refresh tokens, so per-role ``coordination/.codex-home/
<role>`` auth copies diverge on the first refresh (``refresh_token_reused``
/ ``token_expired``) and every other role's driven turn then completes
doing zero useful work — the shared root cause blocking #14/#21/#22 and
the feature_review queue.

This module covers the 0375 driven-Codex behavior that is NOT about env
resolution / model injection (those live in
test_driven_codex_resume_fallback.py):

* ``_CodexStdioSession.consume_turn`` raises on auth-failure signatures /
  ``turn/failed`` / a ``turn/start`` error response, and counts work items;
* ``_drive_codex_turn_stdio`` records codexHome + non-zero-work evidence in
  the turn-log on success, and an ``auth_failure`` outcome (not a silent
  ``ok``) when auth breaks;
* the codex worker classifies a ``_CodexAuthError`` as a failure (``error``)
  with an ``AUTH:`` detail so the escalation tells MAINTAINER to
  ``codex login`` on the machine home.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from greatminds.cli import coordd as cd


# ---------- consume_turn: feed messages over a real pipe fd ----------


class _FakeStdout:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class _FakeProc:
    def __init__(self, fd: int) -> None:
        self.stdout = _FakeStdout(fd)


def _session_with(messages: list[dict]) -> cd._CodexStdioSession:
    r, w = os.pipe()
    for m in messages:
        os.write(w, (json.dumps(m) + "\n").encode("utf-8"))
    os.close(w)
    return cd._CodexStdioSession(_FakeProc(r))


def test_consume_turn_raises_on_auth_signature() -> None:
    sess = _session_with([
        {"method": "item/started", "params": {}},
        {"method": "codex/event",
         "params": {"msg": "error: refresh_token_reused"}},
        {"method": "turn/completed", "params": {"threadId": "th"}},
    ])
    with pytest.raises(cd._CodexAuthError):
        sess.consume_turn("th", time.monotonic() + 5)


def test_consume_turn_raises_on_token_expired() -> None:
    sess = _session_with([
        {"method": "codex/event",
         "params": {"error": "token_expired: please run codex login"}},
    ])
    with pytest.raises(cd._CodexAuthError):
        sess.consume_turn("th", time.monotonic() + 5)


def test_consume_turn_counts_work_items_and_completes() -> None:
    sess = _session_with([
        {"method": "item/started", "params": {}},
        {"method": "item/completed", "params": {}},
        {"method": "turn/completed", "params": {"threadId": "th"}},
    ])
    work, transcript = sess.consume_turn("th", time.monotonic() + 5)
    assert work == 2
    assert "item/started" in transcript


def test_consume_turn_raises_on_turn_failed() -> None:
    sess = _session_with([{"method": "turn/failed",
                           "params": {"reason": "x"}}])
    with pytest.raises(OSError):
        sess.consume_turn("th", time.monotonic() + 5)


def test_consume_turn_raises_on_turn_start_error_response() -> None:
    sess = _session_with([{"id": 3, "error": {"message": "boom"}}])
    with pytest.raises(OSError):
        sess.consume_turn("th", time.monotonic() + 5, turn_req_id=3)


# ---------- end-to-end over a real fake app-server subprocess ----------


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    return coord


def _latest_turn_log(coord: Path, role_lower: str) -> str:
    logs = sorted((coord / ".turns").glob(f"{role_lower}-*.log"))
    assert logs, "no turn-log written"
    return logs[-1].read_text(encoding="utf-8")


_FAKE_AUTH = r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line); m, i = msg.get("method"), msg.get("id")
    if m == "initialize":
        print(json.dumps({"id": i,
              "result": {"codexHome": "/home/violet/.codex"}}), flush=True)
    elif m == "thread/start":
        print(json.dumps({"id": i,
              "result": {"thread": {"id": "th_auth"}}}), flush=True)
    elif m == "turn/start":
        tid = msg["params"]["threadId"]
        print(json.dumps({"id": i, "result": {}}), flush=True)
        print(json.dumps({"method": "codex/event",
              "params": {"error": "token_expired: please run codex login"}}),
              flush=True)
        print(json.dumps({"method": "turn/completed",
              "params": {"threadId": tid}}), flush=True)
'''


_FAKE_WORK = r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line); m, i = msg.get("method"), msg.get("id")
    if m == "initialize":
        print(json.dumps({"id": i,
              "result": {"codexHome": "/home/violet/.codex"}}), flush=True)
    elif m == "thread/start":
        print(json.dumps({"id": i,
              "result": {"thread": {"id": "th_work"}}}), flush=True)
    elif m == "turn/start":
        tid = msg["params"]["threadId"]
        print(json.dumps({"id": i, "result": {}}), flush=True)
        print(json.dumps({"method": "item/started", "params": {}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {}}),
              flush=True)
        print(json.dumps({"method": "turn/completed",
              "params": {"threadId": tid}}), flush=True)
'''


def test_drive_turn_auth_failure_raises_and_logs(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    monkeypatch.setattr(cd, "_codex_appserver_argv",
                        lambda *a, **k: [sys.executable, "-c", _FAKE_AUTH])
    monkeypatch.setattr(cd, "_machine_codex_home",
                        lambda: "/home/violet/.codex")
    with pytest.raises(cd._CodexAuthError):
        cd._drive_codex_turn_stdio(
            coord, "explorer", "", "CONTRACT", str(tmp_path), False,
            handshake_timeout=10.0, turn_timeout=10.0)
    log = _latest_turn_log(coord, "explorer")
    assert "status: auth_failure" in log
    assert "/home/violet/.codex" in log


def test_drive_turn_records_machine_home_and_nonzero_work(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    monkeypatch.setattr(cd, "_codex_appserver_argv",
                        lambda *a, **k: [sys.executable, "-c", _FAKE_WORK])
    monkeypatch.setattr(cd, "_machine_codex_home",
                        lambda: "/home/violet/.codex")
    tid = cd._drive_codex_turn_stdio(
        coord, "explorer", "", "CONTRACT", str(tmp_path), False,
        handshake_timeout=10.0, turn_timeout=10.0)
    assert tid == "th_work"
    log = _latest_turn_log(coord, "explorer")
    assert "codex_home: /home/violet/.codex" in log
    assert "non_zero_work: True" in log
    assert "work_items: 2" in log


# ---------- worker classification of an auth failure ----------


def test_worker_classifies_auth_failure_as_error_with_auth_detail(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)

    def _boom(*a, **k):
        raise cd._CodexAuthError("token_expired during driven turn")

    monkeypatch.setattr(cd, "_drive_codex_turn_stdio", _boom)
    captured: dict = {}
    monkeypatch.setattr(
        cd, "_note_turn_outcome",
        lambda c, r, klass, detail, v: captured.update(
            klass=klass, detail=detail))

    ok, _diag = cd._spawn_driven_codex_turn(
        coord, "explorer", "CONTRACT", str(tmp_path), False,
        reg=None, run_async=False)
    assert ok is True
    assert captured["klass"] == "error", \
        "auth failure must NOT be a silent ok"
    assert "AUTH" in captured["detail"]
    assert not cd._driven_run_lock_path(coord, "explorer").exists(), \
        "the run-lock must be released after a failed turn"
