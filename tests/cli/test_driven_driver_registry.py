"""Tests for coordd driven-agent driver registry."""

from __future__ import annotations

from pathlib import Path

from greatminds.agents import tool_specs
from greatminds.agents.driven_drivers import (
    DrivenDispatchContext,
    available_driven_tools,
    get_driven_driver,
)


def _ctx(
    tmp_path: Path,
    *,
    role: str = "DEVELOPER",
    reg: dict | None = None,
    bootstrap_text: str | None = None,
) -> tuple[DrivenDispatchContext, dict]:
    coord = tmp_path / ".greatminds"
    coord.mkdir()
    bootstrap = coord / "bootstrap.md"
    if bootstrap_text is not None:
        bootstrap.write_text(bootstrap_text, encoding="utf-8")
    calls: dict = {}

    def read_registry(_registry_dir: Path, role_lower: str) -> dict | None:
        calls.setdefault("read_registry", []).append(role_lower)
        return reg

    def driven_bootstrap_path(_coord: Path, role_lower: str) -> str | None:
        calls.setdefault("bootstrap_path", []).append(role_lower)
        return str(bootstrap)

    def spawn_claude_turn(*args, **kwargs):
        calls["claude"] = (args, kwargs)
        return True, "claude ok"

    def spawn_codex_turn(*args, **kwargs):
        calls["codex"] = (args, kwargs)
        return True, "codex ok"

    def spawn_headless_turn(*args, **kwargs):
        calls["headless"] = (args, kwargs)
        return True, "headless ok"

    role_lower = role.lower()
    return DrivenDispatchContext(
        coord=coord,
        coord_yaml_doc={"session": "test-session"},
        located=("pane-1", "claude"),
        role=role,
        role_lower=role_lower,
        verbose=False,
        trigger="",
        registry_dir_name=".agent_registry",
        read_registry=read_registry,
        driven_bootstrap_path=driven_bootstrap_path,
        spawn_claude_turn=spawn_claude_turn,
        spawn_codex_turn=spawn_codex_turn,
        spawn_headless_turn=spawn_headless_turn,
    ), calls


def test_available_driven_tools_are_registry_backed() -> None:
    assert available_driven_tools() == tool_specs.driven_tool_names()
    assert available_driven_tools() == (
        "claude", "codex", "cursor", "cline", "gemini", "openhands",
    )


def test_unknown_driven_tool_returns_none() -> None:
    assert get_driven_driver("unknown-tool") is None


def test_claude_driver_dispatches_existing_session(tmp_path: Path) -> None:
    ctx, calls = _ctx(tmp_path, reg={"session_id": "sess-1"},
                      bootstrap_text="role contract")

    ok, diag = get_driven_driver("claude").drive(ctx)  # type: ignore[union-attr]

    assert ok is True
    assert diag == "claude ok"
    args, kwargs = calls["claude"]
    assert args[:7] == (
        ctx.coord,
        "developer",
        "sess-1",
        "pane-1",
        "test-session",
        str(ctx.coord / "bootstrap.md"),
        False,
    )
    assert kwargs["reg"] == {"session_id": "sess-1"}
    assert kwargs["force_fresh"] is False


def test_claude_driver_forces_fresh_without_session(tmp_path: Path) -> None:
    ctx, calls = _ctx(tmp_path, reg={})

    ok, _diag = get_driven_driver("claude").drive(ctx)  # type: ignore[union-attr]

    assert ok is True
    _args, kwargs = calls["claude"]
    assert kwargs["force_fresh"] is True


def test_codex_driver_reads_bootstrap_as_base_instructions(
    tmp_path: Path,
) -> None:
    ctx, calls = _ctx(
        tmp_path,
        role="TECHNICAL-WRITER",
        reg={"thread_id": "thread-1"},
        bootstrap_text="role contract",
    )

    ok, diag = get_driven_driver("codex").drive(ctx)  # type: ignore[union-attr]

    assert ok is True
    assert diag == "codex ok"
    args, kwargs = calls["codex"]
    assert args[:5] == (
        ctx.coord,
        "technical-writer",
        "role contract",
        str(ctx.coord.parent),
        False,
    )
    assert kwargs["reg"] == {"thread_id": "thread-1"}


def test_generic_headless_driver_builds_tool_argv(tmp_path: Path) -> None:
    ctx, calls = _ctx(
        tmp_path,
        role="TESTER",
        bootstrap_text="role contract",
    )

    ok, diag = get_driven_driver("gemini").drive(ctx)  # type: ignore[union-attr]

    assert ok is True
    assert diag == "headless ok"
    args, _kwargs = calls["headless"]
    assert args[0] == ctx.coord
    assert args[1] == "tester"
    assert args[2] == "gemini"
    assert args[3] == ["gemini", "--yolo", "-p", "role contract"]
