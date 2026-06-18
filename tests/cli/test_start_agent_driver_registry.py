"""Tests for the start-agent driver registry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from greatminds.agents import tool_specs
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
    assert available_start_tools() == tool_specs.start_tool_names()
    assert available_start_tools() == (
        "claude", "codex", "cursor", "cline", "gemini", "openhands",
    )


def test_unknown_start_tool_is_rejected() -> None:
    with pytest.raises(GreatMindsError):
        get_start_driver("unknown-tool")


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
    assert "--slice=cursor.slice" in argv
    assert "-p" in argv
    assert "MemoryMax=4G" in argv
    assert "cursor-agent" in argv
    assert os.environ["GREATMINDS_REGISTRY_TOOL"] == "cursor"


def test_cursor_driver_honors_resource_limit_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GREATMINDS_CURSOR_SLICE", "limited-cursor.slice")
    monkeypatch.setenv("GREATMINDS_CURSOR_MEM_HIGH", "1536M")
    monkeypatch.setenv("GREATMINDS_CURSOR_MEM_MAX", "2G")
    monkeypatch.setenv("GREATMINDS_CURSOR_CPU", "175%")
    ctx = _ctx(tmp_path)
    driver = get_start_driver("cursor")

    argv = driver.build_argv(ctx)

    assert "--slice=limited-cursor.slice" in argv
    assert "MemoryHigh=1536M" in argv
    assert "MemoryMax=2G" in argv
    assert "CPUQuota=175%" in argv


def test_generic_start_driver_builds_cline_argv(tmp_path: Path,
                                                monkeypatch) -> None:
    monkeypatch.delenv("GREATMINDS_REGISTRY_TOOL", raising=False)
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "0")
    ctx = _ctx(tmp_path)
    driver = get_start_driver("cline")

    argv = driver.build_argv(ctx)
    driver.prepare_environment(ctx, dry_run=True)

    assert argv == [
        "cline", "--auto-approve", "true", "bootstrap prompt",
    ]
    assert os.environ["GREATMINDS_REGISTRY_TOOL"] == "cline"


def test_generic_start_driver_builds_gemini_interactive_argv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "0")
    ctx = _ctx(tmp_path)
    driver = get_start_driver("gemini")

    argv = driver.build_argv(ctx)

    assert argv == [
        "gemini", "--yolo", "--prompt-interactive", "bootstrap prompt",
    ]
