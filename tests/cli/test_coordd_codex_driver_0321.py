"""Tests for task 0321 (0311 Phase 3b): coordd codex driver.

iter-3 transport (PLANNER decision after two live-GATE failures of the
``--listen unix://`` WebSocket control socket): drive each turn through
a FRESH ``codex app-server`` over STDIO (no ``--listen``) —
line-delimited JSON-RPC: ``initialize`` → ``thread/start`` (first turn,
baseInstructions = role contract) or ``thread/resume`` (subsequent) →
``turn/start`` ("continue your tick") → wait ``turn/completed``. This is
symmetric to claude's per-turn ``claude -p`` spawn. A per-role run-lock
(shared with the claude path) blocks a second turn while one is in
flight; the blocking turn runs in a daemon thread so coordd is not held.

The stdio framing is grounded on the host: ``codex app-server`` (no
--listen) emits ``{json}\\n`` per message (initialize response, then
notifications). The end-to-end test below drives ``_drive_codex_turn_stdio``
against a REAL fake-server subprocess so the framing is exercised (the
blind spot that let iter-1/iter-2 pass unit tests yet fail live).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as cd


# ---------- JSON-RPC request builders ----------


def test_build_thread_start_request_shape() -> None:
    req = cd._build_thread_start_request(2, "ROLE CONTRACT", "/proj")
    assert req["method"] == "thread/start"
    assert req["params"]["baseInstructions"] == "ROLE CONTRACT"
    assert req["params"]["cwd"] == "/proj"


def test_build_thread_start_request_omits_empty_fields() -> None:
    req = cd._build_thread_start_request(2, None, None)
    assert req["method"] == "thread/start"
    assert "baseInstructions" not in req["params"]
    assert "cwd" not in req["params"]


def test_build_turn_start_request_shape() -> None:
    req = cd._build_turn_start_request(3, "th_abc")
    assert req["method"] == "turn/start"
    assert req["params"]["threadId"] == "th_abc"
    assert req["params"]["input"] == [
        {"type": "text", "text": "continue your tick"}]


def test_build_initialize_request_has_client_info() -> None:
    req = cd._build_initialize_request(1)
    assert req["method"] == "initialize"
    assert "clientInfo" in req["params"]
    assert req["params"]["clientInfo"].get("name")


def test_build_thread_resume_request_shape() -> None:
    req = cd._build_thread_resume_request(2, "th_keep")
    assert req["method"] == "thread/resume"
    assert req["params"]["threadId"] == "th_keep"


# ---------- _codex_appserver_argv: stdio (no --listen) ----------


def test_codex_appserver_argv_is_stdio(monkeypatch) -> None:
    """iter-3: the driver spawns ``codex app-server`` over STDIO (no
    ``--listen``, no ``proxy``); node is named explicitly when it
    resolves (env-node shebang lesson)."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: {
        "codex": "/nvm/bin/codex", "node": "/nvm/bin/node"}.get(b))
    argv = cd._codex_appserver_argv()
    assert argv[-1] == "app-server"
    assert "--listen" not in argv and "proxy" not in argv
    assert argv[0].endswith("/node")
    assert any(a.endswith("/codex") for a in argv[:2])


def test_codex_appserver_argv_fallback_bare_codex(monkeypatch) -> None:
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: None)
    assert cd._codex_appserver_argv() == ["codex", "app-server"]


# ---------- _drive_codex_turn_stdio: end-to-end over real stdio ----------


_FAKE_APPSERVER = r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m, i = msg.get("method"), msg.get("id")
    if m == "initialize":
        print(json.dumps({"id": i, "result": {"ok": True}}), flush=True)
    elif m == "thread/start":
        print(json.dumps({"id": i, "result": {"thread": {"id": "th_fake"}}}),
              flush=True)
    elif m == "thread/resume":
        print(json.dumps({"id": i, "result": {}}), flush=True)
    elif m == "turn/start":
        tid = msg["params"]["threadId"]
        print(json.dumps({"id": i, "result": {"turn": {}}}), flush=True)
        print(json.dumps({"method": "turn/completed",
                          "params": {"threadId": tid}}), flush=True)
'''


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    return coord


def test_drive_codex_turn_stdio_first_turn_end_to_end(
    tmp_path: Path, monkeypatch,
) -> None:
    """iter-3: drive a real fake ``codex app-server`` over stdio. First
    turn (no threadId) → initialize → thread/start → turn/start →
    turn/completed; returns + persists the minted threadId. Exercises
    the REAL line-delimited framing (iter-1/iter-2 blind spot)."""
    coord = _coord(tmp_path)
    monkeypatch.setattr(
        cd, "_codex_appserver_argv",
        lambda: [sys.executable, "-c", _FAKE_APPSERVER])
    tid = cd._drive_codex_turn_stdio(
        coord, "explorer", "", "CONTRACT", str(tmp_path), False,
        handshake_timeout=10.0, turn_timeout=10.0)
    assert tid == "th_fake"
    reg = json.loads(
        (coord / cd.REGISTRY_DIR / "explorer.json").read_text())
    assert reg["thread_id"] == "th_fake"


def test_drive_codex_turn_stdio_resume_turn_end_to_end(
    tmp_path: Path, monkeypatch,
) -> None:
    """iter-3: subsequent turn (threadId known) → thread/resume, not
    thread/start; turn still completes."""
    coord = _coord(tmp_path)
    monkeypatch.setattr(
        cd, "_codex_appserver_argv",
        lambda: [sys.executable, "-c", _FAKE_APPSERVER])
    tid = cd._drive_codex_turn_stdio(
        coord, "explorer", "th_keep", None, str(tmp_path), False,
        handshake_timeout=10.0, turn_timeout=10.0)
    assert tid == "th_keep"


def test_drive_codex_turn_stdio_raises_on_dead_server(
    tmp_path: Path, monkeypatch,
) -> None:
    """A server that exits immediately → OSError (no response)."""
    coord = _coord(tmp_path)
    monkeypatch.setattr(
        cd, "_codex_appserver_argv",
        lambda: [sys.executable, "-c", "pass"])
    with pytest.raises(OSError):
        cd._drive_codex_turn_stdio(
            coord, "explorer", "", "C", str(tmp_path), False,
            handshake_timeout=5.0, turn_timeout=5.0)


# ---------- _spawn_driven_codex_turn: request sequence + run-lock ----------


def _transport(responses: dict):
    """Transport seam recording requests; returns canned responses
    keyed by method."""
    sent: list = []

    def _t(req: dict) -> dict:
        sent.append(req)
        return responses.get(req["method"], {})
    return _t, sent


def test_first_turn_sequence_initialize_threadstart_turnstart(
    tmp_path: Path,
) -> None:
    """No threadId → initialize → thread/start (baseInstructions) →
    turn/start; minted threadId persisted to the registry."""
    coord = _coord(tmp_path)
    transport, sent = _transport({
        "thread/start": {"result": {"thread": {"id": "th_new"}}},
    })
    ok, diag = cd._spawn_driven_codex_turn(
        coord, "explorer", "CONTRACT", "/proj", False,
        transport=transport, reg=None,
    )
    assert ok is True, diag
    assert [r["method"] for r in sent] == [
        "initialize", "thread/start", "turn/start"]
    assert sent[1]["params"]["baseInstructions"] == "CONTRACT"
    assert sent[2]["params"]["threadId"] == "th_new"
    reg = json.loads(
        (coord / cd.REGISTRY_DIR / "explorer.json").read_text())
    assert reg["thread_id"] == "th_new"


def test_subsequent_turn_resumes_thread(tmp_path: Path) -> None:
    """With a threadId in the registry → initialize → thread/resume →
    turn/start (no thread/start)."""
    coord = _coord(tmp_path)
    transport, sent = _transport({})
    ok, _ = cd._spawn_driven_codex_turn(
        coord, "explorer", "CONTRACT", "/proj", False,
        transport=transport, reg={"thread_id": "th_existing"},
    )
    assert ok is True
    assert [r["method"] for r in sent] == [
        "initialize", "thread/resume", "turn/start"]
    assert sent[1]["params"]["threadId"] == "th_existing"
    assert sent[2]["params"]["threadId"] == "th_existing"


def test_run_lock_blocks_second_turn(tmp_path: Path) -> None:
    """Run-lock (shared with the claude path): with the lock held, a
    second event must NOT drive a turn — it sets pending."""
    coord = _coord(tmp_path)
    cd._driven_run_lock_path(coord, "explorer").touch()
    transport, sent = _transport({})
    ok, diag = cd._spawn_driven_codex_turn(
        coord, "explorer", "CONTRACT", "/proj", False,
        transport=transport, reg={"thread_id": "th_x"},
    )
    assert ok is False
    assert sent == [], "0321: no turn while one is in flight"
    assert cd._driven_pending_path(coord, "explorer").exists()
    assert "pending" in diag.lower()


def test_transport_seam_leaves_lock_held(tmp_path: Path) -> None:
    """The transport seam leaves the run-lock held (run-lock
    observability); the async path releases it on turn/completed."""
    coord = _coord(tmp_path)
    transport, _ = _transport({})
    cd._spawn_driven_codex_turn(
        coord, "explorer", None, "/proj", False,
        transport=transport, reg={"thread_id": "th_x"},
    )
    assert cd._driven_run_lock_path(coord, "explorer").exists()


def test_async_path_releases_lock_after_turn(
    tmp_path: Path, monkeypatch,
) -> None:
    """The real (run_async=False here for determinism) path runs the
    stdio turn then RELEASES the run-lock so the next event can drive."""
    coord = _coord(tmp_path)
    monkeypatch.setattr(
        cd, "_codex_appserver_argv",
        lambda: [sys.executable, "-c", _FAKE_APPSERVER])
    ok, _ = cd._spawn_driven_codex_turn(
        coord, "explorer", "CONTRACT", str(tmp_path), False,
        reg=None, run_async=False,
    )
    assert ok is True
    assert not cd._driven_run_lock_path(coord, "explorer").exists(), (
        "0321: run-lock must be released after the turn completes")
    reg = json.loads(
        (coord / cd.REGISTRY_DIR / "explorer.json").read_text())
    assert reg["thread_id"] == "th_fake"


# ---------- _route_queue_event wiring ----------


def _project_codex_driven(tmp_path: Path, *, tool: str, lifecycle: str,
                          role: str, queue: str) -> tuple[Path, Path]:
    project = tmp_path / "project"
    coord = project / "coordination"
    (coord / ".locks").mkdir(parents=True)
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "fleet",
        "project_dir": str(project),
        "windows": [
            {"name": "w", "role": role, "tool": tool, "mode": "driven"},
        ],
    }), encoding="utf-8")
    canon = project / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {queue: {"owner": role}},
        "roles": {role: {"lifecycle": lifecycle}},
        "event_wake": {"by_tool": {
            "claude": "tmux_send_keys",
            "codex": "sigint_deepest_descendant"}},
    }), encoding="utf-8")
    (coord / cd.REGISTRY_DIR / f"{role.lower()}.json").write_text(
        json.dumps({"role": role, "tool": tool, "pid": 1}),
        encoding="utf-8")
    return coord, canon


def test_route_event_drives_codex_role(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a queue landing owned by a driven codex role reaches
    the codex stdio driver (not SIGINT / press_enter)."""
    coord, canon = _project_codex_driven(
        tmp_path, tool="codex", lifecycle="driven",
        role="EXPLORER", queue="review_inbox")

    calls: list = []
    monkeypatch.setattr(
        cd, "_spawn_driven_codex_turn",
        lambda *a, **kw: calls.append((a, kw)) or (True, "ok"),
    )
    monkeypatch.setattr(
        cd, "sigint_sleeping_descendant",
        lambda *a, **kw: pytest.fail(
            "0321: driven codex role must NOT use SIGINT wake"),
    )

    woke = cd._route_queue_event(
        coord, canon, "review_inbox", "0001-x.yaml", verbose=False)
    assert woke is True
    assert len(calls) == 1
    # role threaded through as the first positional after coord.
    assert calls[0][0][1] == "explorer"


def test_route_event_claude_driven_not_codex_path(
    tmp_path: Path, monkeypatch,
) -> None:
    """A driven CLAUDE role must use the 2a ``-p`` path, NOT the codex
    driver."""
    coord, canon = _project_codex_driven(
        tmp_path, tool="claude", lifecycle="driven",
        role="DEVELOPER", queue="feature_dev")
    (coord / cd.REGISTRY_DIR / "developer.json").write_text(
        json.dumps({"role": "DEVELOPER", "tool": "claude", "pid": 1,
                    "session_id": "sess-d"}), encoding="utf-8")

    monkeypatch.setattr(
        cd, "_spawn_driven_codex_turn",
        lambda *a, **kw: pytest.fail(
            "0321: claude role must NOT hit the codex driver"),
    )
    spawned: list = []
    monkeypatch.setattr(
        cd, "_tmux_send_keys_driven",
        lambda session, pane, argv: spawned.append(argv),
    )

    woke = cd._route_queue_event(
        coord, canon, "feature_dev", "0001-x.yaml", verbose=False)
    assert woke is True
    assert spawned and spawned[0][:2] == ["claude", "--resume"]


def test_route_event_codex_not_driven_uses_sigint(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression net: a codex role whose lifecycle != driven keeps the
    legacy SIGINT wake — the codex driver must NOT fire."""
    coord, canon = _project_codex_driven(
        tmp_path, tool="codex", lifecycle="self-loop",
        role="TECHNICAL-WRITER", queue="feature_docs")

    monkeypatch.setattr(
        cd, "_spawn_driven_codex_turn",
        lambda *a, **kw: pytest.fail(
            "0321: non-driven codex role must NOT hit the codex driver"),
    )
    woke_calls: list = []
    monkeypatch.setattr(
        cd, "sigint_sleeping_descendant",
        lambda *a, **kw: woke_calls.append(a) or None,
    )
    woke = cd._route_queue_event(
        coord, canon, "feature_docs", "0001-x.yaml", verbose=False)
    assert woke is True
    assert woke_calls, "0321: non-driven codex keeps SIGINT wake"
