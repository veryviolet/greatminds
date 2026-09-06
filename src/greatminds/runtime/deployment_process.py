"""Synchronous, gated process execution for the stand profile engine."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

from greatminds.core.errors import GreatMindsError
from .processes import process_identity, terminate_group


def run_deployment_command(argv, *, ledger, attempt_id, capture_output=True,
                           text=True, timeout=None, cwd=None, env=None):
    """Persist child identity before exec; drain/clean its group before returning.

    Called by the synchronous profile engine (a daemon worker, not its event loop).
    The enclosing deployment lock serializes execution and recovery operations.
    """
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
                stderr=subprocess.PIPE if capture_output else None, text=text,
            )
        finally:
            os.close(gate_read)
        identity = process_identity(process.pid)
        if identity is None:
            raise GreatMindsError('deployment launch gate exited before identity capture', exit_code=4)
        ledger.attach_process(attempt_id, identity, argv=argv, cwd=cwd)
        os.write(gate_write, b'1')
        os.close(gate_write)
        gate_write = None
        try:
            stdout, stderr = process.communicate(timeout=1800 if timeout is None else timeout)
        except subprocess.TimeoutExpired:
            asyncio.run(terminate_group(identity))
            stdout, stderr = process.communicate(timeout=2)
            ledger.process_exited(attempt_id, process.returncode)
            raise subprocess.TimeoutExpired(argv, 1800 if timeout is None else timeout,
                                            output=stdout, stderr=stderr)
        asyncio.run(terminate_group(identity))
        ledger.process_exited(attempt_id, process.returncode)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
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
