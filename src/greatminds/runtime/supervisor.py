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
from .permissions import PermissionService


class Supervisor:
    def __init__(self, *, project: Path, store: RunStore, config: ExecutionConfig,
                 schema: SchemaSnapshot, environment: dict[str, str] | None = None):
        self.project = project.resolve()
        self.store = store
        self.config = config
        self.schema = schema
        self.environment = dict(os.environ if environment is None else environment)
        self.commands = CommandService(store, environment=self.environment)
        self.permissions = PermissionService(store)
        self.id = uuid.uuid4().hex
        self._lease = None
        self._execution_barrier = None
        self._transports: dict[str, AcpTransport] = {}

    async def __aenter__(self):
        from .migration_safety import execution_barrier
        self._execution_barrier = execution_barrier(self.project)
        self._execution_barrier.__enter__()
        self._lease = file_lock(self.store.directory / "supervisor.lock",
                                label="ACP supervisor", timeout=0)
        try:
            self._lease.__enter__()
            await self.recover()
            return self
        except BaseException:
            try:
                self._lease.__exit__(None, None, None)
                self._lease = None
            finally:
                self._execution_barrier.__exit__(None, None, None)
                self._execution_barrier = None
            raise

    async def __aexit__(self, *exc):
        try:
            for run_id in {item["run_id"] for item in self.store.snapshot().get("commands", {}).values()}:
                await self.commands.finish_run(run_id)
            for transport in list(self._transports.values()):
                await transport.close()
        finally:
            try:
                self._lease.__exit__(*exc)
                self._lease = None
            finally:
                self._execution_barrier.__exit__(*exc)
                self._execution_barrier = None

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
            self.permissions.close_run(run["id"], owner_id=run["owner_id"], reason="supervisor_restart")
            # With no recorded PID the launch gate never authorized exec.
            self.store.recover_run(run["id"], previous_owner=run["owner_id"], owner_id=self.id)

    def claim(self, task: TaskRevision, binding: RoleBinding, *, conversation_id=None, automatic=False) -> Claim:
        if self._lease is None:
            raise RuntimeError("claim requires the exclusive supervisor lease")
        return self.store.claim(task=task, binding=binding, config=self.config,
                                schema=self.schema, project=self.project, owner_id=self.id,
                                conversation_id=conversation_id, automatic=automatic)

    def _transition(self, run_id, target, **kwargs):
        return self.store.transition(run_id, owner_id=self.id, event_id=uuid.uuid4().hex,
                                     target=target, **kwargs)

    async def execute(self, claim: Claim, *, binding: RoleBinding, prompt: str | None = None,
                      conversation=None, keep_open=False) -> dict:
        if self._lease is None or claim.run["owner_id"] != self.id:
            raise RuntimeError("run belongs to a different or inactive supervisor")
        if binding.sha256 != claim.run["binding_sha256"]:
            raise GreatMindsError("binding changed after claim", exit_code=2)
        run_id = claim.run["id"]
        started = time.monotonic()
        self._transition(run_id, "starting")
        agent = self.config.agent(binding.agent)
        metrics = {"context_bytes": 0, "updates": 0, "stop_reason": None, "prompt_started": False, "pre_prompt_activity": False}
        current_turn = None
        accept_output = False

        async def record_spawn(process):
            identity = process_identity(process.pid)
            if identity is None:
                raise RuntimeError("launch gate exited before identity was recorded")
            self.store.record_process(run_id, owner_id=self.id, identity=identity)

        # No raw tool input/output enters the durable metadata snapshot.
        async def event(kind, data):
            if not metrics["prompt_started"]:
                metrics["pre_prompt_activity"] = True
            if kind == "session_update":
                metrics["updates"] += 1
                metrics["last_activity_at"] = self.store.clock()
                if conversation is not None and current_turn is not None and accept_output:
                    update = data.get('update', {})
                    content = update.get('content', {})
                    if update.get('sessionUpdate') == 'agent_message_chunk' and content.get('type') == 'text':
                        from .permissions import redact
                        conversation.append_text(self.id, current_turn['id'], redact(content.get('text', ''),
                            [claim.token, *agent.environment_values(self.environment).values()]))

        callbacks = Callbacks(events=event)
        prompt_deadline = None
        async def ask(session_id, tool, options):
            secrets = [claim.token, *agent.environment_values(self.environment).values()]
            request = self.permissions.create(run_id, owner_id=self.id, session_id=session_id,
                                               tool=tool, options=options, secrets=secrets,
                                               timeout=min(callbacks.permission_timeout,
                                                   max(0, prompt_deadline - time.monotonic()) if prompt_deadline else 0))
            callbacks.needs_input = True
            try:
                while True:
                    selected = self.permissions.consume(request["id"], owner_id=self.id)
                    if selected is not None:
                        callbacks.needs_input = any(item["run_id"] == run_id and item["status"] == "pending"
                            for item in self.store.snapshot().get("permissions", {}).values())
                        return selected
                    await asyncio.sleep(0.1)
            finally:
                self.permissions.cancel(request["id"], owner_id=self.id, reason="callback_closed")
        callbacks.permissions = ask
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
                return await ask(_session, tool, options)
            callbacks.permissions = allow_workspace
        transport = None
        target = "failed"
        reason = "transport_failure"
        try:
            if conversation is not None:
                current_turn = conversation.claim_next(self.id)
                if current_turn is None:
                    raise GreatMindsError('conversation has no queued turn')
            if any(not self.environment.get(name) for name in agent.required_env):
                raise RequestError.auth_required()
            run = await prepare_workspace_async(self.store, claim.run, binding, self.schema, self.id)
            claim = Claim(run, claim.token)
            if conversation is not None and not claim.run.get('conversation_task'):
                import json
                from .context import context_document
                prompt = ('You are working interactively with the user in Greatminds. '
                          'This conversation has no assigned workflow task. Do not submit a task result '
                          'or manufacture another role approval. Follow the user request within your role. '
                          + json.dumps(context_document(self.store, claim, self.schema), ensure_ascii=False))
            elif prompt is None:
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
                saved_session = conversation.snapshot().get('session_id') if conversation is not None else None
                if saved_session:
                    if not getattr(caps, 'load_session', False):
                        reason = 'session_load_unavailable'
                        raise GreatMindsError('conversation resume requires ACP session loading')
                    session = await transport.open_session(session_id=saved_session)
                    session_id = saved_session
                    metrics['session_strategy'] = 'loaded'
                elif binding.session == "resume-if-compatible" and compatible and getattr(caps, "load_session", False):
                    session = await transport.open_session(session_id=compatible["session_id"])
                    session_id = compatible["session_id"]
                    metrics["session_strategy"] = "loaded"
                else:
                    session = await transport.open_session()
                    session_id = session.session_id
                    metrics["session_strategy"] = ("new_load_unavailable" if compatible and binding.session != "new"
                                                   else "new_context")
                await transport.configure_session(session, model=binding.model, mode=binding.mode)
                self.store._check_revision(TaskRevision(claim.run["task_id"], claim.run["task_path"],
                                                       claim.run["task_revision"]))
                self._transition(run_id, "running", session_id=session_id)
                if conversation is not None:
                    conversation.set_session(self.id, session_id)
                prompt_deadline = time.monotonic() + binding.timeout_seconds
                if conversation is None:
                    metrics["prompt_started"] = True
                    result = await transport.prompt(prompt, timeout=binding.timeout_seconds)
                else:
                    while True:
                        if current_turn is None and claim.run.get('conversation_task') and any(
                                r['envelope']['run_id'] == run_id for r in self.store.snapshot()['results'].values()):
                            break  # Release the process before domain application can move/clean the task.
                        if current_turn is None:
                            current_turn = conversation.claim_next(self.id)
                            if current_turn is None:
                                if not keep_open:
                                    break
                                await asyncio.sleep(.1)
                                continue
                        prompt_deadline = time.monotonic() + binding.timeout_seconds
                        self.store._check_revision(TaskRevision(claim.run['task_id'], claim.run['task_path'],
                                                               claim.run['task_revision']))
                        accept_output = True
                        metrics["prompt_started"] = True
                        pending = asyncio.create_task(transport.prompt(
                            prompt + '\nUser message:\n' + current_turn['prompt'], timeout=binding.timeout_seconds))
                        try:
                            while not pending.done():
                                await asyncio.wait({pending}, timeout=.1)
                                turn = conversation.snapshot()['turns'][current_turn['id']]
                                if turn['cancel_requested'] and not pending.done():
                                    pending.cancel()
                            result = await pending
                        finally:
                            if not pending.done():
                                pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                            accept_output = False
                        status = 'completed' if result.stop_reason == 'end_turn' else 'failed'
                        conversation.finish(self.id, current_turn['id'], status=status, reason=result.stop_reason)
                        current_turn = None
                        if status != 'completed':
                            break
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
            if callbacks.needs_input:
                target, reason = "waiting_input", "permission_required"
            else:
                reason = "timeout"
        except (OSError, ValueError, RuntimeError, GreatMindsError) as exc:
            # Persist the class, not arbitrary stderr or exception text which
            # can contain auth material. Detailed redacted diagnostics follow.
            metrics["error_type"] = type(exc).__name__
            if reason == 'transport_failure':
                reason = "configuration_error" if isinstance(exc, (ValueError, GreatMindsError)) else "transport_failure"
        finally:
            if conversation is not None and current_turn is not None:
                conversation.finish(self.id, current_turn['id'],
                    status='cancelled' if target == 'cancelled' else 'failed', reason=reason)
            self._transports.pop(run_id, None)
            self.permissions.close_run(run_id, owner_id=self.id, reason="run_closed", resume_run=True)
            await self.commands.finish_run(run_id)
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 6)
        if conversation is not None:
            conversation.dispatch_status(target, reason)
        return self._transition(run_id, target, reason=reason, details=metrics)
