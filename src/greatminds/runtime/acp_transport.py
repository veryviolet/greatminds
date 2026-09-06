"""Common ACP stdio transport using the upstream Python SDK.

Greatminds owns the OS process group and deadlines; the SDK owns JSON-RPC
framing, correlation, schema validation, and bidirectional dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable

from acp import Client, PROTOCOL_VERSION, RequestError, connect_to_agent, text_block
from acp.schema import (AllowedOutcome, ClientCapabilities, DeniedOutcome,
                        Implementation, RequestPermissionResponse)
from acp.transports import default_environment

from greatminds import __version__


EventSink = Callable[[str, dict], Awaitable[None]]
PermissionHandler = Callable[[str, dict, list[dict]], Awaitable[str | None]]


async def _discard(_kind: str, _data: dict) -> None:
    pass


class Callbacks(Client):
    """Advertise only implemented callbacks; default permission is cancellation.

    Permission selection is delegated to the run policy/operator broker. A
    callback cannot select an option the agent did not offer. Filesystem and
    terminal capabilities are false; unsupported requests fail explicitly.
    """

    def __init__(self, *, events: EventSink = _discard,
                 permissions: PermissionHandler | None = None,
                 permission_timeout: float = 300):
        self.events = events
        self.permissions = permissions
        self.permission_timeout = permission_timeout
        self.session_id: str | None = None
        self.needs_input = False

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        if self.session_id != session_id:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        tool = tool_call.model_dump(mode="json", by_alias=True, exclude_none=True)
        choices = [option.model_dump(mode="json", by_alias=True, exclude_none=True) for option in options]
        await self.events("permission_requested", {"session_id": session_id,
                                                  "tool_call": tool, "options": choices})
        selected = None
        if self.permissions is not None:
            try:
                selected = await asyncio.wait_for(
                    self.permissions(session_id, tool, choices), self.permission_timeout)
            except TimeoutError:
                self.needs_input = True
        else:
            self.needs_input = True
        if selected is not None and selected not in {option.option_id for option in options}:
            raise RequestError.invalid_params({"reason": "permission option was not offered"})
        await self.events("permission_resolved", {"session_id": session_id,
                                                  "tool_call_id": tool_call.tool_call_id,
                                                  "option_id": selected})
        if selected is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=selected))

    async def session_update(self, session_id, update, **kwargs):
        if self.session_id is not None and session_id != self.session_id:
            raise RequestError.invalid_params({"reason": "session mismatch"})
        await self.events("session_update", {"session_id": session_id,
                                            "update": update.model_dump(mode="json", by_alias=True,
                                                                        exclude_none=True)})

    async def _unsupported(self, *args, **kwargs):
        raise RequestError.method_not_found("capability not advertised by this client")

    read_text_file = write_text_file = create_terminal = terminal_output = _unsupported
    release_terminal = wait_for_terminal_exit = kill_terminal = _unsupported
    create_elicitation = complete_elicitation = ext_method = _unsupported

    async def ext_notification(self, method, params):
        # Unknown optional notifications do not alter authoritative state.
        await self.events("extension_notification", {"method": method})


class AcpTransport:
    def __init__(self, argv: tuple[str, ...] | list[str], *, workspace: Path,
                 environment: dict[str, str] | None = None,
                 callbacks: Callbacks | None = None, request_timeout: float = 30,
                 shutdown_timeout: float = 2,
                 on_spawn: Callable[[asyncio.subprocess.Process], Awaitable[None]] | None = None):
        if not argv:
            raise ValueError("ACP argv must not be empty")
        self.argv = tuple(argv)
        self.workspace = workspace.resolve()
        self.environment = {**default_environment(), **(environment or {})}
        self.callbacks = callbacks or Callbacks()
        self.request_timeout = request_timeout
        self.shutdown_timeout = shutdown_timeout
        self.process = None
        self.connection = None
        self.initialized = None
        self._stderr_task = None
        self.stderr_tail = b""
        self._turn_lock = asyncio.Lock()
        self.on_spawn = on_spawn

    async def __aenter__(self):
        gate_read, gate_write = os.pipe()
        try:
            try:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name("agent_exec.py")),
                    str(gate_read), *self.argv, cwd=self.workspace, env=self.environment,
                    pass_fds=(gate_read,), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True, limit=4 * 1024 * 1024)
            finally:
                os.close(gate_read)
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            if self.on_spawn:
                await self.on_spawn(self.process)
            os.write(gate_write, b"1")
            self.connection = connect_to_agent(self.callbacks, self.process.stdin, self.process.stdout)
            self.initialized = await asyncio.wait_for(self.connection.initialize(
                protocol_version=PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="greatminds", version=__version__)), self.request_timeout)
            if self.initialized.protocol_version != PROTOCOL_VERSION:
                raise ValueError(f"unsupported ACP protocol version {self.initialized.protocol_version}")
            return self
        except BaseException:
            await self.close()
            raise
        finally:
            os.close(gate_write)

    async def _drain_stderr(self):
        stream = self.process.stderr
        while chunk := await stream.read(8192):
            self.stderr_tail = (self.stderr_tail + chunk)[-32768:]

    async def open_session(self, *, session_id: str | None = None, mcp_servers=None):
        if session_id is not None:
            if not getattr(self.initialized.agent_capabilities, "load_session", False):
                raise ValueError("agent does not support session/load; create a session from task context")
            # Loading can stream historical updates before its response.
            self.callbacks.session_id = session_id
            response = await asyncio.wait_for(self.connection.load_session(
                session_id=session_id, cwd=str(self.workspace), mcp_servers=mcp_servers or []),
                self.request_timeout)
        else:
            response = await asyncio.wait_for(self.connection.new_session(
                cwd=str(self.workspace), mcp_servers=mcp_servers or []), self.request_timeout)
            self.callbacks.session_id = response.session_id
        return response

    async def authenticate(self, method_id: str):
        if method_id not in {method.id for method in self.initialized.auth_methods or []}:
            raise ValueError("authentication method was not advertised")
        return await asyncio.wait_for(self.connection.authenticate(method_id=method_id), self.request_timeout)

    async def prompt(self, text: str, *, timeout: float):
        if self.callbacks.session_id is None:
            raise ValueError("open a session before prompting")
        if self._turn_lock.locked():
            raise ValueError("a prompt is already active in this session")
        async with self._turn_lock:
            self.callbacks.needs_input = False
            turn = asyncio.create_task(self.connection.prompt(
                session_id=self.callbacks.session_id, prompt=[text_block(text)]))
            try:
                return await asyncio.wait_for(asyncio.shield(turn), timeout)
            except (TimeoutError, asyncio.CancelledError):
                with contextlib.suppress(Exception):
                    await self.cancel()
                    await asyncio.wait_for(asyncio.shield(turn), self.shutdown_timeout)
                if not turn.done():
                    await self.close()
                raise
            finally:
                if not turn.done():
                    turn.cancel()
                with contextlib.suppress(BaseException):
                    await turn

    async def cancel(self):
        if self.connection and self.callbacks.session_id:
            await asyncio.wait_for(self.connection.cancel(session_id=self.callbacks.session_id),
                                   self.shutdown_timeout)

    async def close(self):
        if self.connection:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.connection.close(), self.shutdown_timeout)
            self.connection = None
        if self.process:
            # Descendants may still be alive after the agent process exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), self.shutdown_timeout)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
            self.process = None
        if self._stderr_task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._stderr_task, self.shutdown_timeout)
            self._stderr_task = None

    async def __aexit__(self, *exc):
        await self.close()
