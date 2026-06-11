"""Tests for task 0387: ``greatminds agent status`` (and ``watchdog``)
detect a Codex-backed agent that is alive at the pid/socket level but
WEDGED at a pre-agent / auth prompt (Codex sign-in, "Login timed out",
folder-trust dialog).

The bug: such an agent passed the old health surface as
``alive input_sock=yes`` while its tmux pane was stuck at the Codex
login screen, so a queued user_feedback wake was silently ignored and
the operator believed the planner was healthy. The fix classifies the
pane content and marks the role ``usable=NO`` (no false positives for a
normal agent / shell / unreadable pane).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from greatminds.cli import agent as agent_mod
from greatminds.cli.coordd import REGISTRY_DIR


# Verbatim avatar 0379 evidence (the planner pane the report captured).
CODEX_LOGIN_TIMEOUT_PANE = """\
  Welcome to Codex, OpenAI's command-line coding agent

  Sign in with ChatGPT to use your plan, or provide your own API key.

  > 3. Provide your own API key
  Press enter to continue

  Login timed out
"""

HEALTHY_AGENT_PANE = """\
> I have read the canon and checked my inbox. feature_dev is empty this
  tick. Re-arming wake.

esc to interrupt
"""


def _coord(tmp_path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / REGISTRY_DIR).mkdir(parents=True)
    return coord


def _write_reg(coord, role_lower, **fields):
    payload = {"role": role_lower.upper(), "tool": "codex"}
    payload.update(fields)
    (coord / REGISTRY_DIR / f"{role_lower}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _run(args):
    from greatminds.cli import main as main_mod
    return CliRunner().invoke(main_mod.cli, args, catch_exceptions=True)


# ---------- classify_pane_state (pure) ----------


def test_classify_login_timeout_wins():
    # "Login timed out" is the strongest signal — even though the codex
    # sign-in banner is also present, login_timeout classifies.
    assert agent_mod.classify_pane_state(
        CODEX_LOGIN_TIMEOUT_PANE) == "login_timeout"


def test_classify_auth_prompt_each_pattern():
    for snippet in (
        "Sign in with ChatGPT to continue",
        "  > 3. Provide your own API key",
        "Welcome to Codex, OpenAI's command-line coding agent",
    ):
        assert agent_mod.classify_pane_state(snippet) == "auth_prompt", snippet


def test_classify_trust_prompt():
    assert agent_mod.classify_pane_state(
        "Do you trust the files in this folder?") == "trust_prompt"


def test_classify_ok_for_ordinary_agent_output():
    assert agent_mod.classify_pane_state(HEALTHY_AGENT_PANE) == "ok"
    # an ordinary shell prompt is NOT a wedge
    assert agent_mod.classify_pane_state("violet@dev:~/proj$ ") == "ok"


def test_classify_empty():
    assert agent_mod.classify_pane_state("") == "empty"
    assert agent_mod.classify_pane_state("   \n  ") == "empty"
    assert agent_mod.classify_pane_state(None) == "empty"


def test_wedge_states_are_unusable_others_not():
    assert agent_mod._usability("login_timeout") is False
    assert agent_mod._usability("auth_prompt") is False
    assert agent_mod._usability("trust_prompt") is False
    assert agent_mod._usability("ok") is True
    assert agent_mod._usability("empty") is True
    assert agent_mod._usability(None) is None  # not inspected → unknown


# ---------- collect_agent_status (injected pane) ----------


def test_collect_marks_wedged_codex_unusable(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid(),
               input_sock=str(coord / REGISTRY_DIR / "architect-planner.sock"))
    rec = agent_mod.collect_agent_status(
        coord, "ARCHITECT-PLANNER", pane_text=CODEX_LOGIN_TIMEOUT_PANE)
    # the exact misleading combination from the bug report:
    assert rec["alive"] is True
    assert rec["pane_state"] == "login_timeout"
    assert rec["usable"] is False


def test_collect_healthy_agent_stays_usable(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid())
    rec = agent_mod.collect_agent_status(
        coord, "DEVELOPER", pane_text=HEALTHY_AGENT_PANE)
    assert rec["pane_state"] == "ok"
    assert rec["usable"] is True


def test_collect_uninspected_pane_is_unknown(tmp_path):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid())
    # pane_text=None → skip inspection → unknown (never a false wedge)
    rec = agent_mod.collect_agent_status(
        coord, "DEVELOPER", pane_text=None)
    assert rec["pane_state"] is None
    assert rec["usable"] is None


def test_collect_unregistered_has_stable_pane_shape(tmp_path):
    coord = _coord(tmp_path)
    rec = agent_mod.collect_agent_status(coord, "READER")
    assert rec["registered"] is False
    assert rec["pane_state"] is None
    assert rec["usable"] is None


# ---------- CLI surface ----------


def test_cli_status_flags_wedged_planner(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid(),
               input_sock=str(coord / REGISTRY_DIR / "architect-planner.sock"))

    def fake_pane(_coord, role_lower):
        return CODEX_LOGIN_TIMEOUT_PANE if "planner" in role_lower else None

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "ARCHITECT-PLANNER"])
    assert res.exit_code == 0, res.output
    assert "alive" in res.output            # pid is genuinely alive
    assert "USABLE=NO" in res.output        # but not usable
    assert "login_timeout" in res.output


def test_cli_status_no_pane_skips_inspection(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())

    called = {"n": 0}

    def fake_pane(_coord, role_lower):
        called["n"] += 1
        return CODEX_LOGIN_TIMEOUT_PANE

    monkeypatch.setattr(agent_mod, "_pane_text_for_role", fake_pane)
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "ARCHITECT-PLANNER", "--no-pane"])
    assert res.exit_code == 0, res.output
    assert "USABLE=NO" not in res.output
    assert called["n"] == 0  # --no-pane must not capture the pane


def test_cli_status_json_includes_usable(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())
    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, _r: CODEX_LOGIN_TIMEOUT_PANE)
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "ARCHITECT-PLANNER", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data[0]["pane_state"] == "login_timeout"
    assert data[0]["usable"] is False


def test_cli_healthy_codex_not_flagged(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-reviewer", pid=os.getpid())
    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, _r: HEALTHY_AGENT_PANE)
    monkeypatch.chdir(coord.parent)
    res = _run(["agent", "status", "ARCHITECT-REVIEWER"])
    assert res.exit_code == 0, res.output
    assert "USABLE=NO" not in res.output


# ---------- watchdog surface ----------


def test_watchdog_reports_wedged_agent(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "architect-planner", pid=os.getpid())

    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, role_lower: (
            CODEX_LOGIN_TIMEOUT_PANE if "planner" in role_lower else None))
    monkeypatch.chdir(coord.parent)
    res = _run(["watchdog"])
    # watchdog exits non-zero when it has findings; the wedge IS a finding
    assert "WEDGED AGENTS" in res.output
    assert "login_timeout" in res.output
    assert "ARCHITECT-PLANNER" in res.output


def test_watchdog_no_wedge_when_healthy(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    _write_reg(coord, "developer", pid=os.getpid())
    monkeypatch.setattr(
        agent_mod, "_pane_text_for_role",
        lambda _c, _r: HEALTHY_AGENT_PANE)
    monkeypatch.chdir(coord.parent)
    res = _run(["watchdog"])
    assert "WEDGED AGENTS" not in res.output
    assert "0 wedged" in res.output
