"""1.6.1: a driven codex turn whose cached threadId can't be resumed
(phantom/stale id — its rollout was lost) must fall back to a FRESH
thread/start WITH baseInstructions, so the turn has the role contract and
does real work — instead of resuming a non-existent thread and
"completing" doing nothing (the driven-codex reviewer bug).
"""
from __future__ import annotations

import json
from pathlib import Path

from greatminds.cli import coordd as cd


def _coord(tmp_path: Path, *, thread_id: str | None) -> Path:
    coord = tmp_path / "coordination"
    (coord / cd.REGISTRY_DIR).mkdir(parents=True)
    (coord / ".locks").mkdir(parents=True)
    reg = {"role": "ARCHITECT-REVIEWER", "tool": "codex"}
    if thread_id is not None:
        reg["thread_id"] = thread_id
    (coord / cd.REGISTRY_DIR / "architect-reviewer.json").write_text(
        json.dumps(reg), encoding="utf-8")
    return coord


def _recorded_thread(coord: Path) -> str:
    d = json.loads(
        (coord / cd.REGISTRY_DIR / "architect-reviewer.json").read_text())
    return d.get("thread_id") or ""


def test_resume_error_falls_back_to_fresh_thread_with_contract(tmp_path):
    coord = _coord(tmp_path, thread_id="PHANTOM-019e9381")
    reg = json.loads(
        (coord / cd.REGISTRY_DIR / "architect-reviewer.json").read_text())
    calls = []

    def transport(req):
        calls.append(req)
        m = req.get("method")
        if m == "thread/resume":
            return {"id": req["id"],
                    "error": {"code": -32000, "message": "thread not found"}}
        if m == "thread/start":
            return {"id": req["id"],
                    "result": {"thread": {"id": "FRESH-THREAD"}}}
        return {"id": req["id"], "result": {}}

    ok, diag = cd._spawn_driven_codex_turn(
        coord, "architect-reviewer", "ROLE CONTRACT TEXT",
        str(tmp_path), False, transport=transport, reg=reg)

    assert ok, diag
    methods = [c.get("method") for c in calls]
    assert "thread/resume" in methods, "should TRY resume of the cached id"
    assert "thread/start" in methods, "resume failed → must start fresh"
    start = next(c for c in calls if c.get("method") == "thread/start")
    assert start["params"].get("baseInstructions") == "ROLE CONTRACT TEXT", \
        "the fresh thread must carry the role contract (else context-blind)"
    turn = next(c for c in calls if c.get("method") == "turn/start")
    assert turn["params"]["threadId"] == "FRESH-THREAD", \
        "the turn must run on the fresh thread, not the phantom"
    assert _recorded_thread(coord) == "FRESH-THREAD", \
        "the new threadId must be persisted for next time"


def test_resume_success_does_not_start_fresh(tmp_path):
    coord = _coord(tmp_path, thread_id="GOOD-THREAD")
    reg = json.loads(
        (coord / cd.REGISTRY_DIR / "architect-reviewer.json").read_text())
    calls = []

    def transport(req):
        calls.append(req)
        if req.get("method") == "thread/resume":
            return {"id": req["id"], "result": {"thread": {"id": "GOOD-THREAD"}}}
        return {"id": req["id"], "result": {}}

    ok, _diag = cd._spawn_driven_codex_turn(
        coord, "architect-reviewer", "CONTRACT", str(tmp_path), False,
        transport=transport, reg=reg)

    assert ok
    methods = [c.get("method") for c in calls]
    assert "thread/start" not in methods, "valid resume must NOT start fresh"
    turn = next(c for c in calls if c.get("method") == "turn/start")
    assert turn["params"]["threadId"] == "GOOD-THREAD"


# ---------- 0375: SINGLE machine CODEX_HOME for the driven app-server ----
#
# Reverses the 1.6.1 per-role-CODEX_HOME behavior: codex 0.137 refreshes
# single-use auth tokens in $CODEX_HOME/auth.json, so per-role copies
# diverge (refresh_token_reused) and driven turns do zero work. Driven
# codex now runs against the SINGLE machine login ($HOME/.codex); role
# config (model) is injected via -c overrides + baseInstructions.


def test_codex_appserver_env_uses_machine_home_not_per_role(
        tmp_path, monkeypatch):
    """Even when a per-role home exists, the driven app-server env points
    CODEX_HOME at the single MACHINE home, never the per-role copy."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GREATMINDS_CODEX_HOME", raising=False)
    monkeypatch.setattr(cd.os.path, "expanduser",
                        lambda p: p.replace("~", "/home/violet"))
    coord = tmp_path / "coordination"
    (coord / ".codex-home" / "architect-reviewer").mkdir(parents=True)
    env = cd._codex_appserver_env("architect-reviewer", coord=coord)
    assert env["CODEX_HOME"] == "/home/violet/.codex", \
        "driven codex must use the single machine login, not a per-role home"
    assert ".codex-home" not in env["CODEX_HOME"]
    assert env["GREATMINDS_ROLE"] == "ARCHITECT-REVIEWER"


def test_machine_codex_home_resolution(monkeypatch):
    """Override > inherited-non-per-role CODEX_HOME > ~/.codex; a per-role
    inherited CODEX_HOME is rejected (it's the diverging-auth path)."""
    monkeypatch.setattr(cd.os.path, "expanduser",
                        lambda p: p.replace("~", "/home/violet"))
    # 1. explicit override wins
    monkeypatch.setenv("GREATMINDS_CODEX_HOME", "/custom/codex")
    assert cd._machine_codex_home() == "/custom/codex"
    # 2. inherited real machine home is honored
    monkeypatch.delenv("GREATMINDS_CODEX_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/opt/machine/.codex")
    assert cd._machine_codex_home() == "/opt/machine/.codex"
    # 3. an inherited PER-ROLE home is rejected → fall back to ~/.codex
    monkeypatch.setenv(
        "CODEX_HOME", "/proj/coordination/.codex-home/architect-reviewer")
    assert cd._machine_codex_home() == "/home/violet/.codex"
    # 4. nothing set → ~/.codex
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cd._machine_codex_home() == "/home/violet/.codex"


# ---------- 0375: role model injected via -c (no per-role profile) -------


def test_codex_role_model_read_from_profile_layer(tmp_path):
    """The role model is read from the per-role profile SOURCE (layer
    file preferred), to inject via -c model= without per-role auth."""
    coord = tmp_path / "coordination"
    home = coord / ".codex-home" / "developer"
    home.mkdir(parents=True)
    (home / "developer.config.toml").write_text(
        'model = "gpt-5.5"\napproval_policy = "never"\n', encoding="utf-8")
    assert cd._codex_role_model(coord, "developer") == "gpt-5.5"


def test_codex_role_model_falls_back_to_base_config(tmp_path):
    coord = tmp_path / "coordination"
    home = coord / ".codex-home" / "tester"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        'model = "gpt-5.5-codex"\n', encoding="utf-8")
    assert cd._codex_role_model(coord, "tester") == "gpt-5.5-codex"


def test_codex_role_model_none_when_absent(tmp_path):
    coord = tmp_path / "coordination"
    (coord / ".codex-home" / "reader").mkdir(parents=True)
    assert cd._codex_role_model(coord, "reader") is None


def test_codex_appserver_argv_injects_model():
    argv = cd._codex_appserver_argv("gpt-5.5")
    assert "-c" in argv and 'model="gpt-5.5"' in argv
    # without a model the argv is unchanged (no stray -c model)
    assert not any("model=" in a for a in cd._codex_appserver_argv())
