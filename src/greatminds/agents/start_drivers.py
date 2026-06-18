"""Start-agent driver registry.

This module owns tool-specific argv and environment preparation for
``greatminds start-agent``. Keeping this logic behind a small registry makes
Claude, Codex, Cursor, and future tools follow the same launch contract while
preserving the existing CLI behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from greatminds.cli import codex_auth
from greatminds.agents import tool_specs
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import project_config_dir, project_runtime_dir


CodexSessionDiscoverer = Callable[[str, Path | None], str]


@dataclass(frozen=True)
class StartAgentContext:
    """Resolved launch state shared by all start-agent drivers."""

    role: str
    canon_dir: Path
    project_dir: Path
    registry_dir: Path
    session_id: str
    session_new: bool
    extra: list[str]
    prompt: str


class StartAgentDriver(Protocol):
    """Tool-specific start-agent adapter."""

    name: str

    def build_argv(self, ctx: StartAgentContext) -> list[str]:
        """Return the tool argv before the optional pty wrapper."""

    def prepare_environment(self, ctx: StartAgentContext, *,
                            dry_run: bool) -> None:
        """Apply tool-specific cwd/env/auth preflight before exec."""


def available_start_tools() -> tuple[str, ...]:
    """Tools supported by ``greatminds start-agent``."""
    return tool_specs.start_tool_names()


def yolo_args(tool: str) -> list[str]:
    if os.environ.get("GREATMINDS_START_AGENT_SAFE", "0") == "1":
        return []
    return {
        "claude": ["--permission-mode", "auto"],
        "codex":  ["-a", "never", "-s", "danger-full-access"],
        "cursor": ["--yolo", "--approve-mcps"],
    }.get(tool, [])


def build_claude_argv(
    role: str,
    canon_dir: Path,
    project_dir: Path,
    session_id: str,
    session_new: bool,
    extra: list[str],
    prompt: str,
) -> list[str]:
    """Compose ``claude --name R --session-id|--resume X [plugins] [mcp] -- PROMPT``.

    0390 Claude audit: Claude has NO per-role auth-home split — there is no
    ``CLAUDE_HOME``-per-role equivalent of the Codex ``CODEX_HOME`` path
    that caused the paned-Codex auth-prompt wedge. Claude authenticates
    against ONE global login (``~/.claude`` / ``claude setup-token`` /
    ``ANTHROPIC_API_KEY``) regardless of role; the role identity rides
    ``--name`` + the bootstrap prompt + per-role plugin dirs (config, not
    auth). So a Claude login/limit failure is inherently a GLOBAL auth
    problem, never a misleading per-role-setup failure, and needs no
    machine-vs-role home disambiguation here. (Global login/limit failures
    surface in claude's own startup output / exit; this launcher sets no
    per-role auth home to confuse them with.)"""
    role_plugin_suffix = role.lower().replace("_", "-")
    plugin_dirs = [canon_dir / "plugins" / "coordination-protocol"]
    role_plugin = canon_dir / "plugins" / f"role-{role_plugin_suffix}"
    if role_plugin.is_dir():
        plugin_dirs.append(role_plugin)
    proj_overrides = (
        project_config_dir(project_dir) / "plugins.local" / "project-overrides"
    )
    if proj_overrides.is_dir():
        plugin_dirs.append(proj_overrides)

    mcp_files = [canon_dir / "mcp" / "canon.json"]
    mcp_local = project_config_dir(project_dir) / "mcp.local.json"
    if mcp_local.is_file():
        mcp_files.append(mcp_local)

    canon_args: list[str] = []
    for d in plugin_dirs:
        canon_args += ["--plugin-dir", str(d)]
    for f in mcp_files:
        canon_args += ["--mcp-config", str(f)]

    if session_new:
        session_args = ["--session-id", session_id]
    else:
        session_args = ["--resume", session_id]

    return [
        "claude", "--name", role, *session_args, *yolo_args("claude"),
        *canon_args, *extra, "--", prompt,
    ]


def build_codex_argv(
    role: str,
    registry_dir: Path,
    session_new: bool,
    extra: list[str],
    prompt: str,
    *,
    discover_session: CodexSessionDiscoverer | None = None,
) -> list[str]:
    """Compose ``codex [resume <SID>|] [config overrides] EXTRA PROMPT``.

    The resume decision is driven by Codex's own rollout UUID cache, not by
    Claude-style ``session_new``. When no rollout can be found, Codex starts a
    fresh session and receives the bootstrap prompt as the final positional.
    """
    del session_new  # Kept on the signature for start-agent compatibility.
    role_lower = role.lower()
    codex_session_file = registry_dir / f"{role_lower}.codex-session-id"

    codex_sid = ""
    if codex_session_file.is_file() and codex_session_file.stat().st_size > 0:
        codex_sid = codex_session_file.read_text(encoding="utf-8").strip()
    else:
        project_dir = registry_dir.parent.parent
        if discover_session is not None:
            codex_sid = discover_session(role, project_dir)
        if codex_sid:
            codex_session_file.write_text(codex_sid + "\n", encoding="utf-8")

    project_dir = registry_dir.parent.parent
    role_home = project_runtime_dir(project_dir) / ".codex-home" / role_lower
    codex_model_args = codex_auth.codex_model_config_args(role_home, role_lower)

    if codex_sid:
        codex_args = ["resume", codex_sid, *yolo_args("codex"), *codex_model_args]
    else:
        codex_args = [*yolo_args("codex"), *codex_model_args]

    return ["codex", *codex_args, *extra, prompt]


def build_cursor_argv(
    session_new: bool,
    extra: list[str],
    prompt: str,
) -> list[str]:
    """Compose ``systemd-run … cursor-agent [--continue] --model M EXTRA PROMPT``.

    The systemd-run wrapper isolates cursor's memory/CPU so its known
    long-session leaks OOM-kill only itself, not the host.
    """
    cursor_model = os.environ.get("GREATMINDS_CURSOR_MODEL", "composer-2.5-fast")
    if session_new:
        cursor_args = ["--model", cursor_model, *yolo_args("cursor")]
    else:
        cursor_args = ["--continue", "--model", cursor_model, *yolo_args("cursor")]

    os.environ["GREATMINDS_REGISTRY_TOOL"] = "cursor"

    sdr = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "-p", f"MemoryHigh={os.environ.get('GREATMINDS_CURSOR_MEM_HIGH', '3G')}",
        "-p", f"MemoryMax={os.environ.get('GREATMINDS_CURSOR_MEM_MAX', '4G')}",
        "-p", f"CPUQuota={os.environ.get('GREATMINDS_CURSOR_CPU', '300%')}",
    ]
    return [*sdr, "cursor-agent", *cursor_args, *extra, prompt]


class ClaudeStartDriver:
    name = "claude"

    def build_argv(self, ctx: StartAgentContext) -> list[str]:
        return build_claude_argv(
            ctx.role, ctx.canon_dir, ctx.project_dir, ctx.session_id,
            ctx.session_new, ctx.extra, ctx.prompt,
        )

    def prepare_environment(self, ctx: StartAgentContext, *,
                            dry_run: bool) -> None:
        del ctx, dry_run


class CodexStartDriver:
    name = "codex"

    def __init__(self, discover_session: CodexSessionDiscoverer | None = None):
        self._discover_session = discover_session

    def build_argv(self, ctx: StartAgentContext) -> list[str]:
        return build_codex_argv(
            ctx.role, ctx.registry_dir, ctx.session_new, ctx.extra, ctx.prompt,
            discover_session=self._discover_session,
        )

    def prepare_environment(self, ctx: StartAgentContext, *,
                            dry_run: bool) -> None:
        machine_home = codex_auth.machine_codex_home()
        os.environ["CODEX_HOME"] = machine_home
        if not dry_run and not codex_auth.machine_codex_auth_present(machine_home):
            raise GreatMindsError(
                codex_auth.machine_codex_auth_error(machine_home, ctx.role),
                exit_code=2,
            )


class CursorStartDriver:
    name = "cursor"

    def build_argv(self, ctx: StartAgentContext) -> list[str]:
        return build_cursor_argv(ctx.session_new, ctx.extra, ctx.prompt)

    def prepare_environment(self, ctx: StartAgentContext, *,
                            dry_run: bool) -> None:
        if not dry_run:
            os.chdir(ctx.project_dir)


def get_start_driver(
    tool: str,
    *,
    discover_codex_session: CodexSessionDiscoverer | None = None,
) -> StartAgentDriver:
    """Return the start-agent driver for ``tool``."""
    if tool == "claude":
        return ClaudeStartDriver()
    if tool == "codex":
        return CodexStartDriver(discover_codex_session)
    if tool == "cursor":
        return CursorStartDriver()
    raise GreatMindsError(f"unknown TOOL: {tool}", exit_code=2)
