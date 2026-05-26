"""Tests for task 0236: ``UserPromptSubmit`` hook closes the
end-of-turn inbox gap for chat-mode roles.

Pre-0236 ``.claude/settings.local.json`` had only a ``Stop`` hook.
PLANNER (and MAINTAINER) — chat-mode roles paced by the human — got
``stop-decide`` only AFTER reply. When USER rapidly switched topics
(new prompt before stop-hook resolved the inbox), pending messages
went unread. 0236 adds a ``UserPromptSubmit`` hook that fires
BEFORE each user turn and forces an inbox drain.

Loop-mode roles (DEVELOPER, etc.) bypass UserPromptSubmit — they
have coordd SIGINT (0150) for inbox events; the additional gate
would jam every operator interaction.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod
from greatminds.cli import stop_decide as sd_mod


# ---------- stop_decide --phase ----------


def test_user_prompt_submit_phase_blocks_planner_with_pending(
    tmp_path: Path, monkeypatch,
) -> None:
    """0236 contract: --phase user-prompt-submit + chat-mode role +
    pending inbox → emits Claude block JSON forcing inbox drain
    before the USER prompt is processed."""
    coord = tmp_path / "coordination"
    inbox = coord / "inbox" / "architect-planner"
    inbox.mkdir(parents=True)
    (inbox / "ask-1234-test.yaml").write_text("body: hi", encoding="utf-8")

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(
        sd_mod.stop_decide,
        ["ARCHITECT-PLANNER", "--host", "claude",
         "--project-dir", str(tmp_path),
         "--phase", "user-prompt-submit"],
        input="{}",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.get("decision") == "block"
    assert "continue your tick" in payload.get("reason", "")


def test_user_prompt_submit_phase_blocks_maintainer_with_pending(
    tmp_path: Path,
) -> None:
    """MAINTAINER is the other chat-mode role per the allowlist."""
    coord = tmp_path / "coordination"
    inbox = coord / "inbox" / "maintainer"
    inbox.mkdir(parents=True)
    (inbox / "info-9999.yaml").write_text("body: ok", encoding="utf-8")

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(
        sd_mod.stop_decide,
        ["MAINTAINER", "--host", "claude",
         "--project-dir", str(tmp_path),
         "--phase", "user-prompt-submit"],
        input="{}",
    )
    assert result.exit_code == 0
    assert json.loads(result.output).get("decision") == "block"


def test_user_prompt_submit_phase_noop_for_loop_mode_role(
    tmp_path: Path,
) -> None:
    """0236: DEVELOPER (and other loop-mode roles) bypass the
    UserPromptSubmit phase even with pending inbox. The operator-
    chat experience would otherwise stall on every prompt."""
    coord = tmp_path / "coordination"
    inbox = coord / "inbox" / "developer"
    inbox.mkdir(parents=True)
    (inbox / "wake-9999.yaml").write_text("body: x", encoding="utf-8")

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(
        sd_mod.stop_decide,
        ["DEVELOPER", "--host", "claude",
         "--project-dir", str(tmp_path),
         "--phase", "user-prompt-submit"],
        input="{}",
    )
    assert result.exit_code == 0
    assert result.output.strip() == "{}", (
        "0236: loop-mode role under user-prompt-submit phase must "
        "emit no-op {} regardless of pending inbox"
    )


def test_user_prompt_submit_noop_when_no_pending(tmp_path: Path) -> None:
    """Happy path: PLANNER with empty inbox → no-op JSON (don't
    block the USER turn)."""
    (tmp_path / "coordination" / "inbox" / "architect-planner").mkdir(
        parents=True,
    )
    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(
        sd_mod.stop_decide,
        ["ARCHITECT-PLANNER", "--host", "claude",
         "--project-dir", str(tmp_path),
         "--phase", "user-prompt-submit"],
        input="{}",
    )
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


def test_stop_phase_unchanged_for_loop_mode_role(tmp_path: Path) -> None:
    """Regression pin: default Stop phase still blocks DEVELOPER on
    pending inbox (existing behavior; 0236 didn't change it)."""
    inbox = tmp_path / "coordination" / "inbox" / "developer"
    inbox.mkdir(parents=True)
    (inbox / "wake-1.yaml").write_text("x", encoding="utf-8")
    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(
        sd_mod.stop_decide,
        ["DEVELOPER", "--host", "claude",
         "--project-dir", str(tmp_path)],
        input="{}",
    )
    assert result.exit_code == 0
    assert json.loads(result.output).get("decision") == "block"


# ---------- settings.local.json carries both hooks ----------


def test_settings_local_carries_both_stop_and_user_prompt_submit(
    tmp_path: Path,
) -> None:
    """0236: greenfield ``.claude/settings.local.json`` has BOTH
    Stop and UserPromptSubmit hook entries pointing at
    ``greatminds stop-decide`` with the right --phase value."""
    from greatminds.core.paths import find_canon_dir
    project = tmp_path / "project"
    project.mkdir()
    status = setup_mod._ensure_claude_settings_local(
        project, find_canon_dir(),
    )
    assert status == "written"
    doc = json.loads(
        (project / ".claude" / "settings.local.json").read_text(
            encoding="utf-8",
        )
    )
    hooks = doc.get("hooks") or {}
    assert "Stop" in hooks
    assert "UserPromptSubmit" in hooks, (
        "0236: settings.local.json missing UserPromptSubmit hook"
    )
    stop_cmd = hooks["Stop"][0]["hooks"][0]["command"]
    ups_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "--phase stop" in stop_cmd
    assert "--phase user-prompt-submit" in ups_cmd
    assert "stop-decide" in stop_cmd
    assert "stop-decide" in ups_cmd
