"""Tests for the start-agent driver registry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from greatminds.agents.start_drivers import (
    StartAgentContext,
    available_start_tools,
    get_start_driver,
)
from greatminds.core.errors import GreatMindsError


@pytest.fixture(autouse=True)
def _disable_yolo(monkeypatch):
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "1")


def _ctx(tmp_path: Path, *, role: str = "DEVELOPER") -> StartAgentContext:
    project = tmp_path / "project"
    registry = project / ".greatminds" / ".agent_registry"
    registry.mkdir(parents=True)
    return StartAgentContext(
        role=role,
        canon_dir=tmp_path / "canon",
        project_dir=project,
        registry_dir=registry,
        session_id="session-123",
        session_new=True,
        extra=[],
        prompt="bootstrap prompt",
    )


def test_available_start_tools_are_registry_backed() -> None:
    assert available_start_tools() == ("claude", "codex", "cursor")


def test_unknown_start_tool_is_rejected() -> None:
    with pytest.raises(GreatMindsError):
        get_start_driver("cline")


def test_codex_driver_uses_injected_session_discoverer(tmp_path: Path) -> None:
    calls: list[tuple[str, Path | None]] = []

    def discover(role: str, project_dir: Path | None) -> str:
        calls.append((role, project_dir))
        return "rollout-session-123"

    ctx = _ctx(tmp_path)
    driver = get_start_driver("codex", discover_codex_session=discover)

    argv = driver.build_argv(ctx)

    assert argv[:3] == ["codex", "resume", "rollout-session-123"]
    assert calls == [("DEVELOPER", ctx.project_dir)]
    assert (
        ctx.registry_dir / "developer.codex-session-id"
    ).read_text(encoding="utf-8").strip() == "rollout-session-123"


def test_cursor_driver_marks_logical_registry_tool(tmp_path: Path,
                                                   monkeypatch) -> None:
    monkeypatch.delenv("GREATMINDS_REGISTRY_TOOL", raising=False)
    ctx = _ctx(tmp_path)
    driver = get_start_driver("cursor")

    argv = driver.build_argv(ctx)

    assert argv[0] == "systemd-run"
    assert "cursor-agent" in argv
    assert os.environ["GREATMINDS_REGISTRY_TOOL"] == "cursor"
