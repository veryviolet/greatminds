"""Driven-agent dispatch registry.

The heavy driven-turn mechanics still live in ``coordd``: run locks,
retry/backoff, journal events, subprocess workers, and Codex JSON-RPC.
This module owns the tool selection boundary so driven Claude/Codex roles
share one dispatch contract and future tools can be added without growing
``coordd._maybe_drive_driven_role``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


ReadRegistry = Callable[[Path, str], dict | None]
DrivenBootstrapPath = Callable[[Path, str], str | None]
SpawnClaudeTurn = Callable[..., tuple[bool, str]]
SpawnCodexTurn = Callable[..., tuple[bool, str]]


@dataclass(frozen=True)
class DrivenDispatchContext:
    """Resolved driven-role state shared by driven tool drivers."""

    coord: Path
    coord_yaml_doc: dict | None
    located: tuple[Any, ...] | None
    role: str
    role_lower: str
    verbose: bool
    trigger: str
    registry_dir_name: str
    read_registry: ReadRegistry
    driven_bootstrap_path: DrivenBootstrapPath
    spawn_claude_turn: SpawnClaudeTurn
    spawn_codex_turn: SpawnCodexTurn


class DrivenDriver(Protocol):
    """Tool-specific driven dispatch adapter."""

    name: str

    def drive(self, ctx: DrivenDispatchContext) -> tuple[bool, str]:
        """Run or dispatch one driven turn for ``ctx.role``."""


def available_driven_tools() -> tuple[str, ...]:
    """Tools with a coordd-managed driven execution driver."""
    return ("claude", "codex")


class ClaudeDrivenDriver:
    name = "claude"

    def drive(self, ctx: DrivenDispatchContext) -> tuple[bool, str]:
        reg = ctx.read_registry(ctx.coord / ctx.registry_dir_name,
                                ctx.role_lower)
        session_id = (reg or {}).get("session_id") or ""
        session_name = (ctx.coord_yaml_doc.get("session") or "").strip() \
            if ctx.coord_yaml_doc else ""
        pane = (ctx.located[0] if ctx.located else "").strip()
        bootstrap_file = ctx.driven_bootstrap_path(ctx.coord, ctx.role_lower)
        bf = (bootstrap_file if bootstrap_file and
              Path(bootstrap_file).is_file() else None)
        ok, diag = ctx.spawn_claude_turn(
            ctx.coord,
            ctx.role_lower,
            session_id,
            pane,
            session_name,
            bf,
            ctx.verbose,
            reg=reg,
            force_fresh=(not session_id),
        )
        return ok, diag


class CodexDrivenDriver:
    name = "codex"

    def drive(self, ctx: DrivenDispatchContext) -> tuple[bool, str]:
        reg = ctx.read_registry(ctx.coord / ctx.registry_dir_name,
                                ctx.role_lower)
        bootstrap_file = ctx.driven_bootstrap_path(ctx.coord, ctx.role_lower)
        base_instructions = None
        try:
            bp = Path(bootstrap_file)
            if bp.is_file():
                base_instructions = bp.read_text(encoding="utf-8")
        except (OSError, TypeError):
            base_instructions = None
        ok, diag = ctx.spawn_codex_turn(
            ctx.coord,
            ctx.role_lower,
            base_instructions,
            str(ctx.coord.parent),
            ctx.verbose,
            reg=reg,
        )
        return ok, diag


def get_driven_driver(tool: str) -> DrivenDriver | None:
    """Return the driven driver for ``tool``, or ``None`` when unsupported."""
    if tool == "claude":
        return ClaudeDrivenDriver()
    if tool == "codex":
        return CodexDrivenDriver()
    return None
