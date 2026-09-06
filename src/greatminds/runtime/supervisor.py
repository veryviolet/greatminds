"""One daemon-owned ACP execution path for every role and manifest."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

from acp import RequestError

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import SchemaSnapshot
from greatminds.core.storage import file_lock
from .acp_transport import AcpTransport, Callbacks
from .config import ExecutionConfig, RoleBinding
from .processes import process_identity, terminate_group
from .store import Claim, RunStore, TERMINAL, TaskRevision
from .workspaces import prepare_workspace_async
from .commands import CommandService


class Supervisor:
    def __init__(self, *, project: Path, store: RunStore, config: ExecutionConfig,
                 schema: SchemaSnapshot, environment: dict[str, str] | None = None):
        self.project = project.resolve()
        self.store = store
        self.config = config
        self.schema = schema
        self.environment = dict(os.environ if environment is None else environment)
        self.commands = CommandService(store, environment=self.environment)
        self.id = uuid.uuid4().hex
        self._lease = None
        self._transports: dict[str, AcpTransport] = {}

    async def __aenter__(self):
        self._lease = file_lock(self.store.directory / "supervisor.lock",
                                label="ACP supervisor", timeout=0)
        self._lease.__enter__()
        try:
            await self.recover()
            return self
        except BaseException:
            self._lease.__exit__(None, None, None)
            self._lease = None
            raise

    async def __aexit__(self, *exc):
        try:
            for run_id in {item["run_id"] for item in self.store.snapshot().get("commands", {}).values()}:
                await self.commands.finish_run(run_id)
            for transport in list(self._transports.values()):
                await transport.close()
        finally:
            self._lease.__exit__(*exc)
            self._lease = None

    async def recover(self):
        if self._lease is None:
            raise RuntimeError("recovery requires the exclusive supervisor lease")
        await self.commands.recover(self.id)
        for run in self.store.snapshot()["runs"].values():
            if run["state"] in TERMINAL or run["owner_id"] == self.id:
                continue
            identity = run.get("process")
            if identity:
                await terminate_group(identity)
            # With no recorded PID the launch gate never authorized exec.
            self.store.recover_run(run["id"], previous_owner=run["owner_id"], owner_id=self.id)

    def claim(self, task: TaskRevision, binding: RoleBinding) -> Claim:
        if self._lease is None:
            raise RuntimeError("claim requires the exclusive supervisor lease")
        return self.store.claim(task=task, binding=binding, config=self.config,
                                schema=self.schema, project=self.project, owner_id=self.id)

    def _transition(self, run_id, target, **kwargs):
        return self.store.transition(run_id, owner_id=self.id, event_id=uuid.uuid4().hex,
                                     target=target, **kwargs)

    async def execute(self, claim: Claim, *, binding: RoleBinding, prompt: str | None = None) -> dict:
        if self._lease is None or claim.run["owner_id"] != self.id:
            raise RuntimeError("run belongs to a different or inactive supervisor")
        if binding.sha256 != claim.run["binding_sha256"]:
            raise GreatMindsError("binding changed after claim", exit_code=2)
        run_id = claim.run["id"]
        started = time.monotonic()
        self._transition(run_id, "starting")
        agent = self.config.agent(binding.agent)
        metrics = {"context_bytes": 0, "updates": 0, "stop_reason": None}

        async def record_spawn(process):
            identity = process_identity(process.pid)
            if identity is None:
                raise RuntimeError("launch gate exited before identity was recorded")
            self.store.record_process(run_id, owner_id=self.id, identity=identity)

        # No raw tool input/output enters the durable metadata snapshot.
        async def event(kind, data):
            if kind == "session_update":
                metrics["updates"] += 1
                metrics["last_activity_at"] = self.store.clock()

        callbacks = Callbacks(events=event)
        if binding.permission == "deny":
            async def deny(_session, _tool, options):
                return next((o["optionId"] for o in options if o["kind"] == "reject_once"), None)
            callbacks.permissions = deny
        elif binding.permission == "allow-workspace":
            async def allow_workspace(_session, tool, options):
                locations = tool.get("locations") or []
                workspace = Path(claim.run["workspace"])
                scoped = bool(locations) and all(
                    isinstance(item.get("path"), str) and Path(item["path"]).is_absolute()
                    and Path(item["path"]).resolve().is_relative_to(workspace) for item in locations)
                if tool.get("kind") in {"read", "edit", "search"} and scoped:
                    chosen = next((o["optionId"] for o in options if o["kind"] == "allow_once"), None)
                    if chosen:
                        return chosen
                callbacks.needs_input = True
                return None
            callbacks.permissions = allow_workspace
        transport = None
        target = "failed"
        reason = "transport_failure"
        try:
            if any(not self.environment.get(name) for name in agent.required_env):
                raise RequestError.auth_required()
            run = await prepare_workspace_async(self.store, claim.run, binding, self.schema, self.id)
            claim = Claim(run, claim.token)
            if prompt is None:
                from .context import compile_context
                prompt = compile_context(self.store, claim, self.schema)
            metrics["context_bytes"] = len(prompt.encode())
            env = agent.environment_values(self.environment)
            env.update(GREATMINDS_PROJECT_DIR=str(self.project), GREATMINDS_ROLE=binding.role,
                       GREATMINDS_RUN_ID=run_id, GREATMINDS_RUN_TOKEN=claim.token)
            transport = AcpTransport(agent.argv, workspace=Path(claim.run["workspace"]),
                                     environment=env, callbacks=callbacks, on_spawn=record_spawn)
            self._transports[run_id] = transport
            async with transport:
                caps = transport.initialized.agent_capabilities
                capabilities = caps.model_dump(by_alias=True, warnings=False) if caps else {}
                for name in agent.required_capabilities:
                    value = capabilities
                    for part in name.split("."):
                        value = value.get(part) if isinstance(value, dict) else None
                    if not value:
                        raise ValueError(f"required ACP capability unavailable: {name}")
                if agent.auth_method:
                    await transport.authenticate(agent.auth_method)
                    metrics["authentication_method"] = agent.auth_method
                previous = [run for run in self.store.snapshot()["runs"].values()
                            if run["id"] != run_id and run["state"] in TERMINAL
                            and run.get("session_id") and all(run[key] == claim.run[key] for key in (
                                "task_id", "task_revision", "binding_sha256", "agent_sha256",
                                "schema_sha256", "workspace"))]
                compatible = max(previous, key=lambda run: run.get("sequence", 0)) if previous else None
                if binding.session == "resume-if-compatible" and compatible and getattr(caps, "load_session", False):
                    session = await transport.open_session(session_id=compatible["session_id"])
                    session_id = compatible["session_id"]
                    metrics["session_strategy"] = "loaded"
                else:
                    session = await transport.open_session()
                    session_id = session.session_id
                    metrics["session_strategy"] = ("new_load_unavailable" if compatible and binding.session != "new"
                                                   else "new_context")
                if binding.mode:
                    available = session.modes.available_modes if session.modes else []
                    if binding.mode not in {mode.id for mode in available}:
                        raise ValueError("configured session mode is not advertised")
                    await asyncio.wait_for(transport.connection.set_session_mode(
                        session_id=session_id, mode_id=binding.mode), transport.request_timeout)
                if binding.model:
                    option = next((item for item in session.config_options or []
                                   if item.category == "model" and item.type == "select"), None)
                    if option is None:
                        raise ValueError("agent does not advertise model selection")
                    choices = [choice for item in option.options
                               for choice in (item.options if hasattr(item, "options") else [item])]
                    if binding.model not in {choice.value for choice in choices}:
                        raise ValueError("configured model is not advertised")
                    selected = await asyncio.wait_for(transport.connection.set_config_option(
                        config_id=option.id, session_id=session_id, value=binding.model),
                        transport.request_timeout)
                    if not any(item.id == option.id and item.current_value == binding.model
                               for item in selected.config_options):
                        raise ValueError("agent did not confirm the configured model")
                self.store._check_revision(TaskRevision(claim.run["task_id"], claim.run["task_path"],
                                                       claim.run["task_revision"]))
                self._transition(run_id, "running", session_id=session_id)
                result = await transport.prompt(prompt, timeout=binding.timeout_seconds)
                metrics["stop_reason"] = result.stop_reason
                if callbacks.needs_input:
                    target, reason = "waiting_input", "permission_required"
                elif result.stop_reason == "cancelled":
                    self._transition(run_id, "cancelling", reason="agent_cancelled")
                    target, reason = "cancelled", "agent_cancelled"
                elif result.stop_reason == "end_turn":
                    target, reason = "completed", "turn_ended"
                else:
                    target, reason = "failed", f"agent_stop_{result.stop_reason}"
        except RequestError as exc:
            target = "waiting_auth" if exc.code == -32000 else "failed"
            reason = "authentication_required" if exc.code == -32000 else "protocol_error"
            metrics["error_code"] = exc.code
        except asyncio.CancelledError:
            self._transition(run_id, "cancelling", reason="operator_cancelled")
            target, reason = "cancelled", "operator_cancelled"
        except TimeoutError:
            reason = "timeout"
        except (OSError, ValueError, RuntimeError, GreatMindsError) as exc:
            # Persist the class, not arbitrary stderr or exception text which
            # can contain auth material. Detailed redacted diagnostics follow.
            metrics["error_type"] = type(exc).__name__
            reason = "configuration_error" if isinstance(exc, (ValueError, GreatMindsError)) else "transport_failure"
        finally:
            self._transports.pop(run_id, None)
            await self.commands.finish_run(run_id)
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 6)
        return self._transition(run_id, target, reason=reason, details=metrics)
