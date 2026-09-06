import asyncio
import sys
import time
from pathlib import Path

import pytest

from greatminds.runtime.acp_transport import AcpTransport, Callbacks


SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "acp_server.py"


def transport(tmp_path, scenario="echo", callbacks=None):
    return AcpTransport([sys.executable, str(SERVER), scenario], workspace=tmp_path,
                        callbacks=callbacks, request_timeout=1, shutdown_timeout=0.2)


def test_sdk_negotiates_streams_and_closes_child(tmp_path):
    async def check():
        events = []
        async def sink(kind, data):
            events.append((kind, data))
        client = transport(tmp_path, callbacks=Callbacks(events=sink))
        async with client:
            process = client.process
            assert client.initialized.protocol_version == 1
            session = await client.open_session()
            assert session.session_id == "test-session"
            response = await client.prompt("hello", timeout=1)
            assert response.stop_reason == "end_turn"
            assert events[0][1]["update"]["content"]["text"] == "hello"
        assert process.returncode is not None
    asyncio.run(check())


@pytest.mark.parametrize("decision", [None, "yes", "no"])
def test_permission_callback_runs_while_prompt_is_pending(tmp_path, decision):
    async def check():
        seen = []
        async def choose(session, tool, options):
            assert session == "test-session"
            assert tool["toolCallId"] == "tool-one"
            assert [option["optionId"] for option in options] == ["yes", "no"]
            return decision
        async def sink(kind, data):
            seen.append((kind, data))
        callbacks = Callbacks(events=sink, permissions=choose if decision else None)
        async with transport(tmp_path, "permission", callbacks) as client:
            await client.open_session()
            response = await client.prompt("read", timeout=1)
            assert response.stop_reason == "end_turn"
            assert callbacks.needs_input == (decision is None)
        text = seen[-1][1]["update"]["content"]["text"]
        assert ('"outcome": "cancelled"' if decision is None else f'"optionId": "{decision}"') in text
    asyncio.run(check())


def test_missing_session_load_is_explicit(tmp_path):
    async def check():
        async with transport(tmp_path) as client:
            with pytest.raises(ValueError, match="does not support session/load"):
                await client.open_session(session_id="previous")
    asyncio.run(check())


def test_load_and_prompt_wait_for_preceding_stream_updates(tmp_path):
    async def check():
        messages = []
        async def delayed_sink(kind, data):
            if kind == "session_update":
                await asyncio.sleep(0.03)
                messages.append(data["update"]["content"]["text"])
        async with transport(tmp_path, "resume", Callbacks(events=delayed_sink)) as client:
            await client.open_session(session_id="test-session")
            assert messages == ["historical reply"]
            messages.clear()
            await client.prompt("new turn", timeout=1)
            assert messages == ["hello"]
    asyncio.run(check())


def test_failed_update_sink_cannot_report_success(tmp_path):
    async def check():
        async def failing_sink(kind, data):
            raise RuntimeError("sink unavailable")
        async with transport(tmp_path, callbacks=Callbacks(events=failing_sink)) as client:
            await client.open_session()
            with pytest.raises(ValueError, match="update processing failed"):
                await client.prompt("hello", timeout=1)
    asyncio.run(check())


def test_timeout_kills_uncooperative_process(tmp_path):
    async def check():
        async with transport(tmp_path, "hang") as client:
            process = client.process
            await client.open_session()
            with pytest.raises(TimeoutError):
                await client.prompt("hang", timeout=0.05)
            assert process.returncode is not None
    start = time.monotonic()
    asyncio.run(check())
    assert time.monotonic() - start < 3


def test_incompatible_protocol_closes_process_on_enter_failure(tmp_path):
    async def check():
        client = transport(tmp_path, "bad-version")
        with pytest.raises(ValueError, match="unsupported ACP protocol"):
            async with client:
                pytest.fail("incompatible agent was accepted")
        assert client.process is None
    asyncio.run(check())


def test_disconnect_does_not_turn_into_success(tmp_path):
    async def check():
        async with transport(tmp_path, "disconnect") as client:
            await client.open_session()
            with pytest.raises(Exception):
                await client.prompt("disconnect", timeout=0.5)
    asyncio.run(check())


def test_shutdown_terminates_descendants_in_agent_process_group(tmp_path):
    async def check():
        async with transport(tmp_path, "child") as client:
            await client.open_session()
            await client.prompt("spawn child", timeout=1)
            pid = int((tmp_path / "child.pid").read_text())
            assert Path(f"/proc/{pid}/stat").exists()
        stat = Path(f"/proc/{pid}/stat")
        # A terminated orphan may remain as a zombie until init reaps it.
        assert not stat.exists() or stat.read_text().split(") ", 1)[1].split()[0] == "Z"
    asyncio.run(check())
