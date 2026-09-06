"""Probe a real ACP executable in a temporary workspace.

Model prompts are sent only with explicit --prompt TEXT; the default is initialization only.
Only explicitly named environment references supplement ACP's basic environment.
Private raw stderr stays in the temporary artifact directory, never in stdout.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile

from greatminds.core.storage import atomic_bytes, atomic_json
from greatminds.runtime.acp_transport import AcpTransport, Callbacks
from greatminds.runtime.processes import process_identity


def public_fields(value):
    if isinstance(value, dict):
        return {key: public_fields(item) for key, item in value.items() if key not in {"field_meta", "_meta"}}
    if isinstance(value, list):
        return [public_fields(item) for item in value]
    return value


async def probe_round(argv, workspace, *, artifacts=None, session=False, session_id=None,
                      auth_method=None, prompt=None, expect=None, environment=None, timeout=15,
                      model=None, mode=None, cancel_after=None, cancel_on_output=False):
    artifacts = artifacts or workspace
    report = {"version": 1, "argv": argv, "artifact_dir": str(artifacts), "inference": prompt is not None,
              "tested_at": datetime.now(timezone.utc).isoformat(), "environment_refs": sorted(environment or {})}
    assistant = bytearray()
    capturing = False
    output_started = asyncio.Event()
    report.update(assistant_bytes=0, tool_calls=0, permission_requests=0)

    async def events(kind, data):
        if kind == "permission_requested":
            report["permission_requests"] += 1
        if kind == "session_update" and capturing:
            update = data["update"]
            if update.get("sessionUpdate") == "tool_call":
                report["tool_calls"] += 1
            content = update.get("content", {})
            if update.get("sessionUpdate") == "agent_message_chunk" and content.get("type") == "text":
                raw = content["text"].encode()
                if raw:
                    output_started.set()
                report["assistant_bytes"] += len(raw)
                assistant.extend(raw[:max(0, 65536 - len(assistant))])

    callbacks = Callbacks(events=events)

    async def spawned(process):
        identity = process_identity(process.pid)
        report["process"] = identity
        atomic_json(artifacts / "process.json", identity)

    transport = AcpTransport(argv, workspace=workspace, environment=environment,
                             callbacks=callbacks, request_timeout=timeout, shutdown_timeout=2, on_spawn=spawned)
    try:
        async with transport:
            initialized = transport.initialized
            report.update(status="protocol_ok", protocol_version=initialized.protocol_version,
                agent_info=public_fields(initialized.agent_info.model_dump(mode="json")) if initialized.agent_info else None,
                capabilities=public_fields(initialized.agent_capabilities.model_dump(mode="json")) if initialized.agent_capabilities else {},
                auth_methods=[method.id for method in initialized.auth_methods or []])
            if auth_method:
                report["auth_method"] = auth_method
                await transport.authenticate(auth_method)
                report["authenticated"] = True
            if session or prompt is not None or model or mode or session_id:
                result = await transport.open_session(session_id=session_id)
                session_id = transport.callbacks.session_id
                atomic_json(artifacts / "session.json", {"session_id": session_id})
                report.update(status="session_ok", session_created=True,
                    session_modes=[mode.id for mode in result.modes.available_modes] if result.modes else [],
                    config_categories=[option.category for option in result.config_options or []])
                report["model_choices"] = [choice.value for option in result.config_options or []
                    if option.category == "model" and option.type == "select"
                    for item in option.options
                    for choice in (item.options if hasattr(item, "options") else [item])]
                await transport.configure_session(result, model=model, mode=mode)
                report.update(selected_model=model, selected_mode=mode)
            if prompt is not None:
                capturing = True
                turn = asyncio.create_task(transport.prompt(prompt, timeout=timeout))
                try:
                    if cancel_after is not None:
                        if cancel_on_output:
                            started = asyncio.create_task(output_started.wait())
                            try:
                                await asyncio.wait({turn, started}, return_when=asyncio.FIRST_COMPLETED)
                            finally:
                                started.cancel()
                                try:
                                    await started
                                except asyncio.CancelledError:
                                    pass
                            report["output_before_cancel"] = output_started.is_set()
                        done, _ = await asyncio.wait({turn}, timeout=cancel_after)
                        report["cancel_sent"] = not done
                        if not done:
                            await transport.cancel()
                    result = await turn
                finally:
                    capturing = False
                    if not turn.done():
                        turn.cancel()
                    try:
                        await turn
                    except BaseException:
                        pass
                report["stop_reason"] = result.stop_reason
                report["status"] = "prompt_ok" if result.stop_reason == "end_turn" else "agent_stopped"
                if cancel_after is not None:
                    report["status"] = ("cancel_ok" if report["cancel_sent"] and result.stop_reason == "cancelled"
                                        else "cancel_not_confirmed")
                if callbacks.needs_input:
                    report["status"] = "waiting_input"
                if expect is not None:
                    report["expected_text_matches"] = (report["assistant_bytes"] == len(assistant)
                                                       and assistant.decode(errors="replace").strip() == expect)
                    if report["status"] == "prompt_ok" and not report["expected_text_matches"]:
                        report["status"] = "prompt_mismatch"
    except Exception as exc:
        report.update(status="waiting_auth" if getattr(exc, "code", None) == -32000 else "failed",
                      error_type=type(exc).__name__, error_code=getattr(exc, "code", None))
        atomic_json(artifacts / "error.json", {"error_type": type(exc).__name__, "message": str(exc),
                                              "error_code": getattr(exc, "code", None)})
    report["stderr_bytes"] = len(transport.stderr_tail)
    atomic_bytes(artifacts / "stderr.log", transport.stderr_tail)
    if prompt is not None:
        atomic_bytes(artifacts / "assistant.txt", bytes(assistant))
        report["assistant_sha256"] = hashlib.sha256(assistant).hexdigest()
    report["process_exited"] = not report.get("process") or process_identity(report["process"]["pid"]) is None
    atomic_json(artifacts / "report.json", report)
    return report, session_id


async def probe(argv, *, resume=False, resume_prompt=None, resume_expect=None, **options):
    workspace = Path(tempfile.mkdtemp(prefix="greatminds-acp-probe-"))
    session = options.pop("session", False)
    report, session_id = await probe_round(argv, workspace, session=resume or session, **options)
    if resume:
        if report["status"] not in {"session_ok", "prompt_ok"} or not report["process_exited"]:
            report["resume_skipped"] = "initial_round_failed"
        else:
            artifacts = workspace / "resume-evidence"
            artifacts.mkdir(mode=0o700)
            resumed_options = dict(options)
            if resume_prompt is not None:
                resumed_options.update(prompt=resume_prompt, expect=resume_expect)
            resumed, _ = await probe_round(argv, workspace, artifacts=artifacts, session_id=session_id, **resumed_options)
            report["resume"] = resumed
            report["status"] = ("resume_ok" if resumed["status"] in {"session_ok", "prompt_ok"}
                                and resumed["process_exited"] else "resume_failed")
        atomic_json(workspace / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="store_true")
    parser.add_argument("--auth-method", metavar="ID")
    parser.add_argument("--model", metavar="ID")
    parser.add_argument("--mode", metavar="ID")
    parser.add_argument("--resume", action="store_true", help="close the process, load its session in a fresh process, repeat any prompt")
    parser.add_argument("--resume-prompt", metavar="TEXT", help="different prompt after loading the saved session")
    parser.add_argument("--resume-expect", metavar="TEXT")
    parser.add_argument("--cancel-after", type=float, metavar="SECONDS", help="cancel a pending explicit prompt")
    parser.add_argument("--cancel-on-output", action="store_true", help="start the cancellation delay after the first assistant text")
    parser.add_argument("--prompt", metavar="TEXT", help="explicit live model prompt (may consume provider usage)")
    parser.add_argument("--expect", metavar="TEXT", help="require an exact assistant text response")
    parser.add_argument("--env", action="append", default=[], metavar="NAME")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv or args.timeout <= 0:
        parser.error("provide an executable and a positive timeout")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name not in os.environ for name in args.env):
        parser.error("--env must name an existing environment variable")
    if args.expect is not None and args.prompt is None:
        parser.error("--expect requires --prompt")
    if (args.resume_prompt is not None and not args.resume) or (args.resume_expect is not None and args.resume_prompt is None):
        parser.error("--resume-prompt requires --resume; --resume-expect requires --resume-prompt")
    if args.cancel_on_output and args.cancel_after is None:
        parser.error("--cancel-on-output requires --cancel-after")
    if args.cancel_after is not None and (args.cancel_after <= 0 or args.cancel_after >= args.timeout
                                         or args.prompt is None or args.resume or args.expect is not None):
        parser.error("--cancel-after requires a prompt, 0 < delay < timeout, and no --resume or --expect")
    report = asyncio.run(probe(argv, session=args.session, auth_method=args.auth_method, prompt=args.prompt,
                               resume=args.resume, model=args.model, mode=args.mode, cancel_after=args.cancel_after,
                               resume_prompt=args.resume_prompt, resume_expect=args.resume_expect,
                               cancel_on_output=args.cancel_on_output,
                               expect=args.expect, timeout=args.timeout,
                               environment={name: os.environ[name] for name in args.env}))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"protocol_ok", "session_ok", "prompt_ok", "resume_ok", "cancel_ok"} and report["process_exited"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
