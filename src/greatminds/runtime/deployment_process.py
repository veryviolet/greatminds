"""Synchronous, gated process execution for the stand profile engine."""
from __future__ import annotations

import asyncio
import hashlib
import math
import selectors
import time
import os
from pathlib import Path
import subprocess
import sys

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_bytes, safe_name
from .processes import process_identity, terminate_group


def _collect(process, identity, *, timeout, limit, text, cancel_event):
    deadline = time.monotonic() + timeout
    data = {'stdout': bytearray(), 'stderr': bytearray()}
    counts = {'stdout': 0, 'stderr': 0}
    hashes = {name: hashlib.sha256() for name in data}
    timed_out = cancelled = cleaned = False
    drain_deadline = None
    with selectors.DefaultSelector() as selector:
        for name in data:
            stream = getattr(process, name)
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
        while True:
            now = time.monotonic()
            cancelled = cancelled or bool(cancel_event and cancel_event.is_set())
            timed_out = timed_out or (not cleaned and process.poll() is None and now >= deadline)
            if not cleaned and (process.poll() is not None or timed_out or cancelled):
                asyncio.run(terminate_group(identity))
                process.wait(timeout=2)
                cleaned = True
                drain_deadline = time.monotonic() + 2
            if cleaned and not selector.get_map():
                break
            if cleaned and time.monotonic() >= drain_deadline:
                raise GreatMindsError('deployment output pipe remained open after group cleanup', exit_code=4)
            for key, _ in selector.select(timeout=.1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                counts[name] += len(chunk)
                hashes[name].update(chunk)
                data[name].extend(chunk[:max(0, limit - len(data[name]))])
    output = {name: {'bytes': counts[name], 'captured_bytes': len(data[name]),
                     'sha256': hashes[name].hexdigest(), 'truncated': counts[name] > len(data[name])}
              for name in data if getattr(process, name) is not None}
    captured = {name: bytes(data[name]) for name in output}
    def value(name):
        if getattr(process, name) is None:
            return None
        raw = captured[name]
        return raw.decode('utf-8', errors='replace') if text else raw
    return value('stdout'), value('stderr'), output, timed_out, cancelled, captured


def run_deployment_command(argv, *, ledger, attempt_id, capture_output=True,
                           text=True, timeout=None, cwd=None, env=None,
                           max_output_bytes=1048576, cancel_event=None):
    """Persist child identity before exec; drain/clean its group before returning.

    Called by the synchronous profile engine (a daemon worker, not its event loop).
    The enclosing deployment lock serializes execution and recovery operations.
    """
    if type(max_output_bytes) is not int or not 0 <= max_output_bytes <= 67108864:
        raise GreatMindsError('deployment output limit must be 0–67108864 bytes per stream', exit_code=2)
    timeout = 1800 if timeout is None else timeout
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise GreatMindsError('deployment timeout must be finite and positive', exit_code=2)
    if cancel_event and cancel_event.is_set():
        raise InterruptedError('deployment cancelled before launch')
    gate_read, gate_write = os.pipe()
    process = identity = None
    try:
        try:
            process = subprocess.Popen(
                [sys.executable, '-I', str(Path(__file__).with_name('agent_exec.py')),
                 str(gate_read), *argv],
                cwd=cwd, env=env, pass_fds=(gate_read,), start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_output else None,
                stderr=subprocess.PIPE if capture_output else None, text=False,
            )
        finally:
            os.close(gate_read)
        identity = process_identity(process.pid)
        if identity is None:
            raise GreatMindsError('deployment launch gate exited before identity capture', exit_code=4)
        ledger.attach_process(attempt_id, identity, argv=argv, cwd=cwd)
        if cancel_event and cancel_event.is_set():
            raise InterruptedError('deployment cancelled before exec authorization')
        os.write(gate_write, b'1')
        os.close(gate_write)
        gate_write = None
        stdout, stderr, output, timed_out, cancelled, captured = _collect(
            process, identity, timeout=timeout, limit=max_output_bytes, text=text,
            cancel_event=cancel_event)
        for name, raw in captured.items():
            path = ledger.path.parent / 'deployment-output' / safe_name(attempt_id) / name
            atomic_bytes(path, raw)
            output[name].update(path=str(path), captured_sha256=hashlib.sha256(raw).hexdigest())
        ledger.process_exited(attempt_id, process.returncode, output=output)
        if cancelled:
            raise InterruptedError('deployment cancelled; external outcome requires assessment')
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        result.output_metadata = output
        return result
    finally:
        # EOF before authorization prevents exec, including persistence failures.
        if gate_write is not None:
            os.close(gate_write)
        if process is not None:
            if identity is not None:
                asyncio.run(terminate_group(identity))
            elif process.poll() is None:
                process.kill()  # Still our unreaped gated child; no PID reuse.
            try:
                process.wait(timeout=2)
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
