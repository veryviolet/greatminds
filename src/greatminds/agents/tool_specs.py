"""Machine-readable agent tool capability registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AgentToolSpec:
    """Current greatminds support level for one coding-agent tool."""

    name: str
    label: str
    start_agent: bool
    driven: bool
    start_modes: tuple[str, ...] = ("loop", "chat")
    driven_transport: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start_modes"] = list(self.start_modes)
        return data


_TOOL_SPECS: tuple[AgentToolSpec, ...] = (
    AgentToolSpec(
        name="claude",
        label="Claude Code",
        start_agent=True,
        driven=True,
        driven_transport="claude -p subprocess",
    ),
    AgentToolSpec(
        name="codex",
        label="Codex CLI",
        start_agent=True,
        driven=True,
        driven_transport="codex app-server stdio",
    ),
    AgentToolSpec(
        name="cursor",
        label="Cursor agent",
        start_agent=True,
        driven=False,
        driven_transport=None,
    ),
)


def all_tool_specs() -> tuple[AgentToolSpec, ...]:
    """All agent tools known to this greatminds build."""
    return _TOOL_SPECS


def get_tool_spec(name: str) -> AgentToolSpec | None:
    """Return the capability spec for ``name``."""
    key = name.lower()
    return next((spec for spec in _TOOL_SPECS if spec.name == key), None)


def start_tool_names() -> tuple[str, ...]:
    """Tool names accepted by ``greatminds start-agent``."""
    return tuple(spec.name for spec in _TOOL_SPECS if spec.start_agent)


def driven_tool_names() -> tuple[str, ...]:
    """Tool names with coordd-managed driven execution."""
    return tuple(spec.name for spec in _TOOL_SPECS if spec.driven)
