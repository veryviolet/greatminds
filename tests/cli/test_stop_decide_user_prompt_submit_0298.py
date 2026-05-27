"""Tests for task 0298: ``stop-decide --phase user-prompt-submit``
MUST NOT return ``decision: block`` — that would reject every USER
prompt to chat-mode roles and deadlock the conversation.

Pre-0298 ``stop_decide.py`` emitted the same ``decision: block``
payload for both ``phase=stop`` (correct) and ``phase=user-prompt-
submit`` (catastrophic — USER's input jammed at the hook layer).
0298 branches the output: user-prompt-submit emits only the
informational ``systemMessage``; the USER's prompt still reaches
the role.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _project(tmp_path: Path, *, role: str = "ARCHITECT-PLANNER",
              with_pending_inbox: bool = True) -> Path:
    project = tmp_path / "proj"
    coord = project / "coordination"
    inbox = coord / "inbox" / role.lower()
    inbox.mkdir(parents=True)
    if with_pending_inbox:
        (inbox / "ask-fake-0001.yaml").write_text(
            "to_role: PLANNER\nbody: test\n", encoding="utf-8",
        )
    return project


def _run(role: str, *, project: Path, phase: str,
          host: str = "claude") -> dict:
    """Invoke ``greatminds stop-decide`` and parse JSON stdout."""
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project)
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "stop-decide", role,
         "--host", host,
         "--project-dir", str(project),
         "--phase", phase],
        capture_output=True, text=True, env=env,
        cwd=str(project), input="",
    )
    assert cp.returncode == 0, cp.stderr
    if not cp.stdout.strip():
        return {}
    return json.loads(cp.stdout.strip())


# ---------- user-prompt-submit NEVER blocks ----------


def test_user_prompt_submit_does_not_emit_decision_block(
    tmp_path: Path,
) -> None:
    """0298 contract: ``phase=user-prompt-submit`` with pending
    inbox messages must emit only ``systemMessage`` — never
    ``decision: block``. Returning ``block`` here jams the USER's
    prompt at the hook layer and deadlocks the conversation."""
    project = _project(tmp_path, role="ARCHITECT-PLANNER",
                        with_pending_inbox=True)
    payload = _run("ARCHITECT-PLANNER", project=project,
                    phase="user-prompt-submit")
    assert "decision" not in payload, (
        f"0298: user-prompt-submit must NOT carry decision; "
        f"got {payload!r}"
    )
    assert "systemMessage" in payload
    assert "pending inbox" in payload["systemMessage"].lower() \
        or "continue your tick" in payload["systemMessage"]


def test_user_prompt_submit_maintainer_also_does_not_block(
    tmp_path: Path,
) -> None:
    """Same contract for MAINTAINER — both chat-mode roles must
    have prompts pass through the hook."""
    project = _project(tmp_path, role="MAINTAINER",
                        with_pending_inbox=True)
    payload = _run("MAINTAINER", project=project,
                    phase="user-prompt-submit")
    assert "decision" not in payload


def test_user_prompt_submit_non_chat_role_returns_empty(
    tmp_path: Path,
) -> None:
    """0236 regression net: loop-mode roles (DEVELOPER, TESTER,
    etc.) get an empty {} from user-prompt-submit regardless of
    inbox state — the hook is chat-mode-only."""
    project = _project(tmp_path, role="DEVELOPER",
                        with_pending_inbox=True)
    payload = _run("DEVELOPER", project=project,
                    phase="user-prompt-submit")
    assert payload == {}


def test_user_prompt_submit_no_inbox_returns_empty(
    tmp_path: Path,
) -> None:
    """An empty inbox + user-prompt-submit phase → ``{}``; no
    systemMessage when there's nothing to surface."""
    project = _project(tmp_path, role="ARCHITECT-PLANNER",
                        with_pending_inbox=False)
    payload = _run("ARCHITECT-PLANNER", project=project,
                    phase="user-prompt-submit")
    assert payload == {}


# ---------- phase=stop still blocks (regression net) ----------


def test_phase_stop_still_blocks_chat_role_with_inbox(
    tmp_path: Path,
) -> None:
    """0236 contract: ``phase=stop`` (or default) with pending
    inbox returns the full ``decision: block`` payload so the
    agent doesn't idle between turns when work is pending."""
    project = _project(tmp_path, role="ARCHITECT-PLANNER",
                        with_pending_inbox=True)
    payload = _run("ARCHITECT-PLANNER", project=project,
                    phase="stop")
    assert payload.get("decision") == "block"
    assert payload.get("reason")
    assert payload.get("systemMessage")


def test_phase_stop_chat_role_no_inbox_returns_empty(
    tmp_path: Path,
) -> None:
    """No inbox → no block, even on stop (the hook is informational
    only)."""
    project = _project(tmp_path, role="ARCHITECT-PLANNER",
                        with_pending_inbox=False)
    payload = _run("ARCHITECT-PLANNER", project=project,
                    phase="stop")
    assert payload == {}


def test_phase_stop_loop_role_with_inbox_still_blocks(
    tmp_path: Path,
) -> None:
    """Loop-mode roles also block on stop — this preserves the
    coordd SIGINT contract for inbox events landing between
    ticks."""
    project = _project(tmp_path, role="DEVELOPER",
                        with_pending_inbox=True)
    payload = _run("DEVELOPER", project=project, phase="stop")
    assert payload.get("decision") == "block"
