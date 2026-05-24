"""Regression tests for task 0112: per-task flock with timeout and PID
identification.

The existing task_file_lock blocked indefinitely with no diagnostic.
0112 adds:
  - non-blocking acquire with poll-loop up to TASK_FILE_LOCK_TIMEOUT_SEC
  - holder PID written to the lock file while held
  - on timeout, raises GreatMindsError naming the holder PID

Tests run an external Python subprocess to actually hold an OS-level
flock (in-process flock on the same fd doesn't conflict on Linux).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from greatminds.cli.task import (
    TASK_FILE_LOCK_TIMEOUT_SEC,
    task_file_lock,
)
from greatminds.core.errors import GreatMindsError


HOLDER_SCRIPT = textwrap.dedent("""
    import fcntl, os, sys, time
    lock_path = sys.argv[1]
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.fsync(fd)
    print("LOCKED", flush=True)
    time.sleep(60)
""")


def _start_holder(lock_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER_SCRIPT, str(lock_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Wait for the holder to announce it has the lock.
    line = proc.stdout.readline().strip()
    assert line == "LOCKED", (
        f"holder script failed to acquire lock: line={line!r} "
        f"stderr={proc.stderr.read()!r}"
    )
    return proc


def _stop_holder(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_task_file_lock_writes_holder_pid_to_file(tmp_path: Path) -> None:
    """While the lock is held, the lock file content is the holder PID."""
    coord = tmp_path
    task_id = "0001-test"
    lock_path = coord / ".locks" / f"{task_id}.lock"
    with task_file_lock(coord, task_id):
        assert lock_path.is_file()
        content = lock_path.read_text(encoding="utf-8").strip()
        assert content == str(os.getpid()), (
            f"lock file content must be holder PID; got {content!r}"
        )


def test_task_file_lock_clears_pid_on_release(tmp_path: Path) -> None:
    coord = tmp_path
    task_id = "0001-test"
    lock_path = coord / ".locks" / f"{task_id}.lock"
    with task_file_lock(coord, task_id):
        pass
    # After release, file exists but content is empty.
    assert lock_path.is_file()
    assert lock_path.read_text(encoding="utf-8").strip() == ""


def test_task_file_lock_times_out_when_other_process_holds(
    tmp_path: Path,
) -> None:
    """Second acquirer with short timeout must raise GreatMindsError
    naming the holder PID."""
    coord = tmp_path
    task_id = "0002-contested"
    lock_path = coord / ".locks" / f"{task_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = _start_holder(lock_path)
    try:
        t0 = time.monotonic()
        with pytest.raises(GreatMindsError) as excinfo:
            with task_file_lock(coord, task_id, timeout=0.5,
                                poll_interval=0.05):
                pass
        elapsed = time.monotonic() - t0
        # Must actually wait close to the timeout (not fail immediately).
        assert 0.4 < elapsed < 2.0, f"unexpected elapsed: {elapsed}"
        msg = str(excinfo.value)
        assert f"task {task_id} is being transitioned by" in msg
        assert f"pid {holder.pid}" in msg, (
            f"error message must name holder pid {holder.pid}: {msg!r}"
        )
    finally:
        _stop_holder(holder)


def test_task_file_lock_acquires_after_holder_dies(tmp_path: Path) -> None:
    """Linux flock is auto-released on process exit; the next acquirer
    must succeed once the holder is gone — no manual stale-pid cleanup
    required."""
    coord = tmp_path
    task_id = "0003-stale-holder"
    lock_path = coord / ".locks" / f"{task_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = _start_holder(lock_path)
    # Kill the holder; the kernel releases its flock.
    holder.kill()
    holder.wait()
    # Acquire should succeed quickly without waiting the full timeout.
    t0 = time.monotonic()
    with task_file_lock(coord, task_id, timeout=2.0, poll_interval=0.05):
        pass
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, (
        f"acquire after holder death should be fast; got {elapsed}s"
    )


def test_task_file_lock_default_timeout_constant_is_30s() -> None:
    """0112 contract: default timeout is 30s."""
    assert TASK_FILE_LOCK_TIMEOUT_SEC == 30.0
