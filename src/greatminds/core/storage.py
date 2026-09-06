"""Small POSIX storage primitives shared by domain and runtime services."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import GreatMindsError


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path) -> None:
    """Persist newly created directory entries as well as file replacements."""
    if path.is_dir():
        return
    _ensure_directory(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
    _sync_directory(path.parent)


def safe_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", value):
        raise GreatMindsError("invalid identity (expected a simple name)", exit_code=2)
    return value


@contextmanager
def file_lock(path: Path, *, label: str, timeout: float = 30.0,
              poll_interval: float = 0.1, shared: bool = False) -> Iterator[None]:
    """Hold a stable inode; never unlink a lock with possible queued waiters.

    The PID is diagnostic only. Ownership is determined by flock, which the
    kernel releases on process exit. Lock existence does not mean ownership.
    """
    _ensure_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    holder = os.pread(fd, 64, 0).decode("ascii", errors="replace").strip()
                    raise GreatMindsError(
                        f"{label} is being transitioned by "
                        f"{'pid ' + holder if holder else 'unknown pid'}; "
                        f"waited {timeout:g}s (lock at {path}). Retry after the holder releases.",
                        exit_code=4,
                    )
                time.sleep(poll_interval)
        if not shared:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
        yield
    finally:
        try:
            if acquired and not shared:
                os.ftruncate(fd, 0)
        finally:
            # Closing also releases flock if truncation fails.
            os.close(fd)


def task_lock(runtime: Path, task_id: str, *, timeout: float = 30.0,
              poll_interval: float = 0.1):
    return file_lock(runtime / ".locks" / f"{safe_name(task_id)}.lock",
                     label=f"task {task_id}", timeout=timeout, poll_interval=poll_interval)


def atomic_json(path: Path, value: object) -> None:
    """Replace a document and sync its directory before reporting success."""
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                         allow_nan=False) + "\n"
    atomic_bytes(path, content.encode("utf-8"))


def atomic_bytes(path: Path, content: bytes) -> None:
    """Sync a complete replacement before making it visible."""
    _ensure_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def durable_move(source: Path, destination: Path) -> None:
    _ensure_directory(destination.parent)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    os.rename(source, destination)
    _sync_directory(destination.parent)
    if source.parent != destination.parent:
        _sync_directory(source.parent)
