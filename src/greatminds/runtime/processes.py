"""Linux process identities and bounded cleanup for daemon restart recovery."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
from pathlib import Path


def _pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Portable CPython builds may omit the wrappers even when host libc and
    # kernel support pidfds. Keep the same kernel primitive in that case.
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_open
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    fd = function(pid, 0)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return fd


def _pidfd_signal(fd: int, signum: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, signum)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_send_signal
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(fd, signum, None, 0) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def process_identity(pid: int) -> dict | None:
    """PID plus boot/start identity; None means absent or already terminated.

    Permission and parse failures propagate. They are uncertainty, not proof
    that a process is gone. /proc is the supported Linux host interface.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        return {"pid": pid, "start_ticks": int(fields[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "group": int(fields[2]), "session": int(fields[3])}
    except FileNotFoundError:
        return None


def group_members(identity: dict) -> list[dict]:
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != identity["boot_id"]:
        return []
    leader = process_identity(identity["pid"])
    if leader is not None and leader != identity:
        return []  # PID has been reused; this is not our process group.
    members = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        member = process_identity(int(path.name))
        if (member and member["group"] == identity["pid"]
                and member["session"] == identity["pid"]
                and member["start_ticks"] >= identity["start_ticks"]):
            members.append(member)
    return members


def signal_member(identity: dict, signum: int) -> None:
    """Use a pidfd so reuse between identity verification and signal is safe."""
    try:
        fd = _pidfd_open(identity["pid"])
    except ProcessLookupError:
        return
    try:
        if process_identity(identity["pid"]) == identity:
            _pidfd_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


async def terminate_group(identity: dict, *, timeout: float = 2) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = asyncio.get_running_loop().time() + timeout
        while members := group_members(identity):
            for member in members:
                signal_member(member, sig)
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        if not group_members(identity):
            return
    raise TimeoutError("agent process group is still alive after bounded cleanup")
