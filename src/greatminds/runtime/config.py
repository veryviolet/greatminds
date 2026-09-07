"""Versioned ACP launch manifests and independent role bindings.

Configuration contains environment *names*, never copied credentials. Nothing
in this module launches a process or silently substitutes another harness.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import safe_name


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _fail(message: str):
    raise GreatMindsError(f"execution config: {message}", exit_code=2)


def _mapping(value: Any, label: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        _fail(f"{label} must be a mapping")
    if allowed is not None and value.keys() - allowed:
        _fail(f"unknown {label} fields: {sorted(value.keys() - allowed)}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        _fail(f"{label} must be a nonempty string")
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return tuple(_string(item, label) for item in value)


def _choice(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{label} must be one of {sorted(allowed)}")
    return value


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        _fail(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class AgentManifest:
    id: str
    argv: tuple[str, ...]
    adapter_version: str
    harness_version: str
    environment: tuple[tuple[str, str], ...] = ()
    required_env: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    optional_capabilities: tuple[str, ...] = ()
    auth_method: str | None = None

    @property
    def sha256(self) -> str:
        return fingerprint(asdict(self))

    def environment_values(self, source: Mapping[str, str]) -> dict[str, str]:
        """Resolve references at launch time without persisting their values."""
        absent = [name for name in self.required_env if not source.get(name)]
        if absent:
            raise GreatMindsError(f"agent {self.id}: missing environment names: "
                                  + ", ".join(absent), exit_code=2)
        return {dest: source[ref] for dest, ref in self.environment if ref in source}


@dataclass(frozen=True)
class RoleBinding:
    id: str
    role: str
    agent: str
    workspace: str = "."
    scheduling: str = "on-demand"
    permission: str = "ask"
    session: str = "resume-if-compatible"
    model: str | None = None
    mode: str | None = None
    account: str = "default"
    max_running: int = 1
    timeout_seconds: int = 1800
    max_prompt_bytes: int = 262144
    max_session_input_bytes: int = 1048576
    max_no_progress_turns: int = 1
    max_startup_retries: int = 2
    retry_initial_seconds: int = 5
    retry_max_seconds: int = 60

    @property
    def sha256(self) -> str:
        return fingerprint(asdict(self))

    def workspace_path(self, project: Path) -> Path:
        path = (project / self.workspace).resolve()
        if not path.is_dir():
            raise GreatMindsError(f"binding {self.id}: workspace does not exist: {path}",
                                  exit_code=2)
        return path


@dataclass(frozen=True)
class CommandDefinition:
    id: str
    argv: tuple[str, ...]
    roles: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 600
    max_output_bytes: int = 1048576
    environment: tuple[tuple[str, str], ...] = ()
    required_env: tuple[str, ...] = ()
    environment_revision: str = "1"
    purpose: str = "validation"
    authorized: bool = False

    @property
    def sha256(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class StandDeploymentPolicy:
    profiles: tuple[str, ...] = ()
    authorized: bool = False
    timeout_seconds: int = 1800
    max_output_bytes: int = 1048576
    environment_revision: str = "1"


@dataclass(frozen=True)
class ExecutionConfig:
    agents: tuple[AgentManifest, ...]
    bindings: tuple[RoleBinding, ...]
    max_running: int = 4
    account_limits: tuple[tuple[str, int], ...] = ()
    commands: tuple[CommandDefinition, ...] = ()
    stand: StandDeploymentPolicy | None = None
    account_retry_initial_seconds: int = 5
    account_retry_max_seconds: int = 60
    max_runtime_events: int = 10000

    @property
    def sha256(self) -> str:
        return fingerprint(asdict(self))

    def agent(self, agent_id: str) -> AgentManifest:
        return next(agent for agent in self.agents if agent.id == agent_id)


def parse_execution_config(document: Any, *, roles: set[str]) -> ExecutionConfig:
    root = _mapping(document, "root", {"version", "agents", "bindings", "max_running",
                                       "account_limits", "commands", "stand",
                                       "account_retry_initial_seconds", "account_retry_max_seconds", "max_runtime_events"})
    if type(root.get("version")) is not int or root["version"] != 1:
        _fail("version must be 1")
    agents = []
    for name, raw in _mapping(root.get("agents"), "agents").items():
        safe_name(name)
        item = _mapping(raw, f"agent {name}", {
            "transport", "argv", "adapter_version", "harness_version", "environment",
            "required_env", "required_capabilities", "optional_capabilities", "auth_method"})
        if item.get("transport") != "acp":
            _fail(f"agent {name}: transport must be acp")
        argv = _strings(item.get("argv"), "argv")
        if not argv:
            _fail(f"agent {name}: argv must not be empty")
        env = _mapping(item.get("environment", {}), "environment")
        required = _strings(item.get("required_env", []), "required_env")
        for name_or_ref in [*env, *env.values(), *required]:
            if not isinstance(name_or_ref, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name_or_ref):
                _fail("environment entries must reference environment variable names")
        agents.append(AgentManifest(
            name, argv, _string(item.get("adapter_version"), "adapter_version"),
            _string(item.get("harness_version"), "harness_version"),
            tuple(sorted(env.items())), required,
            _strings(item.get("required_capabilities", []), "required_capabilities"),
            _strings(item.get("optional_capabilities", []), "optional_capabilities"),
            _string(item["auth_method"], "auth_method") if "auth_method" in item else None))
    bindings = []
    agent_ids = {agent.id for agent in agents}
    for name, raw in _mapping(root.get("bindings"), "bindings").items():
        safe_name(name)
        item = _mapping(raw, f"binding {name}", set(RoleBinding.__dataclass_fields__) - {"id"})
        role = _string(item.get("role"), "role")
        if role not in roles:
            _fail(f"binding {name}: unknown role {role}")
        agent = _string(item.get("agent"), "agent")
        if agent not in agent_ids:
            _fail(f"binding {name}: unknown agent {agent}")
        retries = item.get("max_startup_retries", 2)
        if type(retries) is not int or not 0 <= retries <= 20:
            _fail("max_startup_retries must be an integer between 0 and 20")
        no_progress = _positive(item.get("max_no_progress_turns", 1), "max_no_progress_turns")
        if no_progress > 20:
            _fail("max_no_progress_turns must not exceed 20")
        bindings.append(RoleBinding(
            id=name, role=role, agent=agent,
            workspace=_string(item.get("workspace", "."), "workspace"),
            scheduling=_choice(item.get("scheduling", "on-demand"), {"on-demand", "queue"}, "scheduling"),
            permission=_choice(item.get("permission", "ask"), {"ask", "deny", "allow-workspace"}, "permission"),
            session=_choice(item.get("session", "resume-if-compatible"), {"new", "resume-if-compatible"}, "session"),
            model=_string(item["model"], "model") if "model" in item else None,
            mode=_string(item["mode"], "mode") if "mode" in item else None,
            account=safe_name(item.get("account", "default")),
            max_running=_positive(item.get("max_running", 1), "max_running"),
            timeout_seconds=_positive(item.get("timeout_seconds", 1800), "timeout_seconds"),
            max_prompt_bytes=_positive(item.get("max_prompt_bytes", 262144), "max_prompt_bytes"),
            max_session_input_bytes=_positive(item.get("max_session_input_bytes", 1048576), "max_session_input_bytes"),
            max_startup_retries=retries, max_no_progress_turns=no_progress,
            retry_initial_seconds=_positive(item.get("retry_initial_seconds", 5), "retry_initial_seconds"),
            retry_max_seconds=_positive(item.get("retry_max_seconds", 60), "retry_max_seconds")))
    limits = _mapping(root.get("account_limits", {}), "account_limits")
    commands = []
    for name, raw in _mapping(root.get("commands", {}), "commands").items():
        safe_name(name)
        item = _mapping(raw, f"command {name}", set(CommandDefinition.__dataclass_fields__) - {"id"})
        argv = _strings(item.get("argv"), "command argv")
        allowed_roles = _strings(item.get("roles"), "command roles")
        if not argv or not allowed_roles or set(allowed_roles) - roles:
            _fail(f"command {name}: requires argv and known roles")
        cwd = _string(item.get("cwd", "."), "command cwd")
        if Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            _fail("command cwd must be relative to the assigned workspace")
        env = _mapping(item.get("environment", {}), "command environment")
        required = _strings(item.get("required_env", []), "command required_env")
        for ref in [*env, *env.values(), *required]:
            if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref):
                _fail("command environment entries must reference variable names")
        authorized = item.get("authorized", False)
        if type(authorized) is not bool:
            _fail("command authorized must be boolean")
        commands.append(CommandDefinition(
            id=name, argv=argv, roles=allowed_roles, cwd=cwd,
            timeout_seconds=_positive(item.get("timeout_seconds", 600), "command timeout"),
            max_output_bytes=_positive(item.get("max_output_bytes", 1048576), "command output limit"),
            environment=tuple(sorted(env.items())), required_env=required,
            environment_revision=_string(item.get("environment_revision", "1"), "environment revision"),
            purpose=_choice(item.get("purpose", "validation"),
                            {"validation", "deployment", "publication"}, "command purpose"),
            authorized=authorized))
    stand = None
    if "stand" in root:
        item = _mapping(root["stand"], "stand", set(StandDeploymentPolicy.__dataclass_fields__))
        authorized = item.get("authorized", False)
        if type(authorized) is not bool:
            _fail("stand authorized must be boolean")
        profiles = _strings(item.get("profiles", []), "stand profiles")
        if len(set(profiles)) != len(profiles) or (authorized and not profiles):
            _fail("authorized stand deployment requires distinct explicit profiles")
        limit = _positive(item.get("max_output_bytes", 1048576), "stand output limit")
        if limit > 67108864:
            _fail("stand output limit must not exceed 67108864")
        stand = StandDeploymentPolicy(profiles, authorized,
            _positive(item.get("timeout_seconds", 1800), "stand timeout"), limit,
            _string(item.get("environment_revision", "1"), "stand environment revision"))
    max_events = _positive(root.get("max_runtime_events", 10000), "max_runtime_events")
    if not 100 <= max_events <= 1000000:
        _fail("max_runtime_events must be between 100 and 1000000")
    return ExecutionConfig(tuple(agents), tuple(bindings),
                           _positive(root.get("max_running", 4), "max_running"),
                           tuple((safe_name(k), _positive(v, "account limit"))
                                 for k, v in sorted(limits.items())), tuple(commands), stand,
                           _positive(root.get("account_retry_initial_seconds", 5), "account_retry_initial_seconds"),
                           _positive(root.get("account_retry_max_seconds", 60), "account_retry_max_seconds"), max_events)


def load_execution_config(path: Path, *, roles: set[str]) -> ExecutionConfig:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(f"cannot load execution config {path}: {exc}", exit_code=2) from exc
    return parse_execution_config(document, roles=roles)
