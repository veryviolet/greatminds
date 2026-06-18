"""Machine-readable agent tool capability registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


HeadlessStyle = Literal["cline", "gemini", "openhands", "cursor"]


@dataclass(frozen=True)
class AgentToolSpec:
    """Current greatminds support level for one coding-agent tool."""

    name: str
    label: str
    start_agent: bool
    driven: bool
    start_modes: tuple[str, ...] = ("loop", "chat")
    driven_transport: str | None = None
    binary: str | None = None
    headless_style: HeadlessStyle | None = None
    notes: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start_modes"] = list(self.start_modes)
        return data


_TOOL_SPECS: tuple[AgentToolSpec, ...] = (
    AgentToolSpec(
        name="claude",
        label="Claude Code",
        binary="claude",
        start_agent=True,
        driven=True,
        driven_transport="claude -p subprocess",
        notes="Stateful paned and driven support with Claude session ids.",
    ),
    AgentToolSpec(
        name="codex",
        label="Codex CLI",
        binary="codex",
        start_agent=True,
        driven=True,
        driven_transport="codex app-server stdio",
        notes="Stateful paned and driven support with Codex sessions.",
    ),
    AgentToolSpec(
        name="cursor",
        label="Cursor agent",
        binary="cursor-agent",
        start_agent=True,
        driven=True,
        driven_transport="cursor-agent headless subprocess",
        headless_style="cursor",
        notes="Paned support is wrapped with systemd-run; driven support is one-shot headless.",
    ),
    AgentToolSpec(
        name="cline",
        label="Cline CLI",
        binary="cline",
        start_agent=True,
        driven=True,
        driven_transport="cline --json subprocess",
        headless_style="cline",
        notes="Driven support is one-shot headless JSON mode.",
    ),
    AgentToolSpec(
        name="gemini",
        label="Gemini CLI",
        binary="gemini",
        start_agent=True,
        driven=True,
        driven_transport="gemini -p subprocess",
        headless_style="gemini",
        notes="Driven support is one-shot headless prompt mode.",
    ),
    AgentToolSpec(
        name="openhands",
        label="OpenHands CLI",
        binary="openhands",
        start_agent=True,
        driven=True,
        start_modes=("chat",),
        driven_transport="openhands --headless subprocess",
        headless_style="openhands",
        notes="Driven support is one-shot headless and requires OpenHands/LiteLLM configuration on the machine.",
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


def build_headless_argv(tool: str, prompt: str) -> list[str]:
    """Build a one-shot headless argv for generic driven subprocess tools."""
    spec = get_tool_spec(tool)
    if spec is None or spec.binary is None or spec.headless_style is None:
        raise ValueError(f"{tool} has no generic headless argv")
    if spec.headless_style == "cline":
        return [spec.binary, "--json", "--auto-approve", "true", prompt]
    if spec.headless_style == "gemini":
        return [spec.binary, "--yolo", "-p", prompt]
    if spec.headless_style == "openhands":
        return [spec.binary, "--headless", "--json", "-t", prompt]
    if spec.headless_style == "cursor":
        return [spec.binary, "--yolo", "--approve-mcps", "-p", prompt]
    raise ValueError(f"unsupported headless style: {spec.headless_style}")


def build_interactive_argv(tool: str, prompt: str,
                           extra: list[str] | None = None) -> list[str]:
    """Build the start-agent argv for tools handled by generic adapters."""
    extra = extra or []
    spec = get_tool_spec(tool)
    if spec is None or spec.binary is None:
        raise ValueError(f"{tool} has no interactive argv")
    if tool == "cline":
        return [spec.binary, *extra, prompt]
    if tool == "gemini":
        return [spec.binary, *extra, "-p", prompt]
    if tool == "openhands":
        return [spec.binary, *extra, "-t", prompt]
    raise ValueError(f"{tool} has no generic interactive argv")
