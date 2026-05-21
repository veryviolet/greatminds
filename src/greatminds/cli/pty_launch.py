#!/usr/bin/env python3
"""pty-launch <ROLE> <TOOL> [tool args...] — spawn TOOL in a pty we own.

Why: writing to /dev/pts/N as an unrelated process targets the *slave* side
and shows up as terminal output (display), not input. The reason user
keystrokes wake the agent is that the terminal emulator (xterm/gnome-
terminal/…) writes to the *master* end of the pty, which routes to the
slave's read-queue — i.e. the running process's stdin.

This launcher creates its own pty, runs TOOL inside it (slave fd as
stdin/stdout/stderr), keeps the master fd, and proxies bytes between
the user's terminal and the pty. Crucially it also exposes a unix
socket at `<COORDINATION_DIR>/.agent_registry/<role>.sock` that any
external process (coordd!) can connect to and write bytes — those bytes
go straight into the master end and are seen by the agent exactly like
user keystrokes.

The registry file `<role>.json` is updated to record the socket path so
coordd knows where to push.

Usage in start_agent:
    pty-launch DEVELOPER claude --name DEVELOPER --permission-mode auto "$PROMPT"
"""
from __future__ import annotations

import errno
import json
import os
import pty
import select
import signal
import socket
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import click

from greatminds.core.paths import find_coord_dir as _strict_find_coord_dir


def find_coord_dir() -> Path:
    """Best-effort coord-dir resolution — never dies, falls back to cwd/coordination.

    pty-launch may run before any project tree exists yet (e.g. during a fresh
    coord-init sequence). ``core.paths.find_coord_dir(strict=False)`` returns
    ``cwd/coordination`` when no enclosing project is found, matching the
    historical behaviour of this module.
    """
    return _strict_find_coord_dir(strict=False)


def usage() -> None:
    print("usage: pty-launch <ROLE> <TOOL> [args...]", file=sys.stderr)


def write_registry(role: str, sock_path: Path, tty_path: str, tool: str, pid: int) -> Path:
    """Enrich the existing registry record with pty-launch's runtime info.

    start_agent writes a pre-pty record with ``session_id`` and
    ``session_new`` (it owns the session UUID). pty-launch then runs
    inside the same execvp chain and rewrites the same file with
    ``pid`` (the forked child's pid, not start_agent's) and the
    ``input_sock`` path. We MERGE on top of the pre-existing record
    rather than overwriting, so the session_id is preserved end-to-end
    (otherwise downstream readers of the registry lose resume info).
    """
    coord = find_coord_dir()
    registry = coord / ".agent_registry"
    registry.mkdir(parents=True, exist_ok=True)
    reg_file = registry / f"{role.lower()}.json"

    existing: dict = {}
    if reg_file.is_file():
        try:
            existing = json.loads(reg_file.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}

    enriched = {
        **existing,
        "role": role,
        "tool": tool,
        "pid": pid,
        "tty": tty_path,
        "input_sock": str(sock_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    reg_file.write_text(json.dumps(enriched))
    return reg_file


def serve_input_sock(sock_path: Path, master_fd: int, stop_event: threading.Event) -> None:
    """Listen on a unix socket; every connection's bytes get forwarded to master_fd."""
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(8)
        srv.settimeout(0.5)
    except OSError as exc:
        print(f"pty-launch: cannot bind input socket {sock_path}: {exc}", file=sys.stderr)
        return
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            # Keep reading until the client closes — coordd may send the
            # text and the Enter as separate writes with a small pause
            # between them, and a single recv would break the second one.
            conn.settimeout(2.0)
            while True:
                try:
                    data = conn.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not data:
                    break
                os.write(master_fd, data)
        finally:
            try:
                conn.close()
            except OSError:
                pass
    try:
        srv.close()
        sock_path.unlink(missing_ok=True)
    except OSError:
        pass


def proxy_loop(master_fd: int, stop_event: threading.Event) -> None:
    """Forward bytes between user's stdin/stdout and the pty master."""
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    # Put user's terminal in raw mode for transparent forwarding.
    old_attrs = None
    if os.isatty(stdin_fd):
        try:
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except (termios.error, OSError):
            old_attrs = None
    try:
        while not stop_event.is_set():
            try:
                r, _, _ = select.select([stdin_fd, master_fd], [], [], 0.5)
            except (OSError, select.error) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.EINTR:
                    continue
                break
            if stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                try:
                    os.write(master_fd, data)
                except OSError:
                    break
            if master_fd in r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                try:
                    os.write(stdout_fd, data)
                except OSError:
                    break
    finally:
        if old_attrs is not None and os.isatty(stdin_fd):
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
            except (termios.error, OSError):
                pass


@click.command(
    name="pty-launch",
    short_help="exec TOOL inside a pty + expose unix socket for coordd",
    help=__doc__,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("role")
@click.argument("exec_binary")
@click.argument("tool_args", nargs=-1, type=click.UNPROCESSED)
def pty_launch(role: str, exec_binary: str, tool_args: tuple[str, ...]) -> None:
    # argv[2] is the binary we exec. When start_agent wraps the agent in
    # ``systemd-run --user --scope ... cursor-agent``, exec_binary is
    # ``systemd-run``, which is useless in the registry (and hides that
    # this is really a cursor agent — coordd needs the logical tool to
    # pick the right wake/submit sequence). start_agent exports
    # GREATMINDS_REGISTRY_TOOL with the logical name; prefer it.
    tool = os.environ.get("GREATMINDS_REGISTRY_TOOL") or exec_binary
    tool_args = list(tool_args)

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: exec the actual binary (systemd-run wrapper or the tool).
        try:
            os.execvp(exec_binary, [exec_binary, *tool_args])
        except OSError as exc:
            print(f"pty-launch: exec {exec_binary} failed: {exc}", file=sys.stderr)
            os._exit(127)

    # Parent: register, then proxy.
    coord = find_coord_dir()
    sock_path = coord / ".agent_registry" / f"{role.lower()}.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    tty_path = os.ttyname(sys.stdin.fileno()) if os.isatty(sys.stdin.fileno()) else "none"
    reg_file = write_registry(role, sock_path, tty_path, tool, pid)

    stop_event = threading.Event()

    sock_thread = threading.Thread(
        target=serve_input_sock,
        args=(sock_path, master_fd, stop_event),
        daemon=True,
    )
    sock_thread.start()

    # Propagate terminal resize.
    def on_winch(signum, frame):
        try:
            import fcntl
            import struct
            sz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, sz)
        except (OSError, ImportError):
            pass

    signal.signal(signal.SIGWINCH, on_winch)
    on_winch(None, None)  # initial sync

    try:
        proxy_loop(master_fd, stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        try:
            reg_file.unlink()
        except OSError:
            pass
        try:
            sock_path.unlink()
        except OSError:
            pass


def _pty_launch_impl(role: str, exec_binary: str, tool_args: list[str]) -> None:
    """Pure-Python entry — invoked by ``__main__`` direct path so that
    the ``--`` separator (which click strips during option parsing) is
    preserved into the child process's argv. claude's ``--mcp-config``
    is variadic and needs ``--`` to terminate it before the prompt.
    """
    tool = os.environ.get("GREATMINDS_REGISTRY_TOOL") or exec_binary

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(exec_binary, [exec_binary, *tool_args])
        except OSError as exc:
            print(f"pty-launch: exec {exec_binary} failed: {exc}", file=sys.stderr)
            os._exit(127)

    coord = find_coord_dir()
    sock_path = coord / ".agent_registry" / f"{role.lower()}.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    tty_path = os.ttyname(sys.stdin.fileno()) if os.isatty(sys.stdin.fileno()) else "none"
    reg_file = write_registry(role, sock_path, tty_path, tool, pid)

    stop_event = threading.Event()

    sock_thread = threading.Thread(
        target=serve_input_sock,
        args=(sock_path, master_fd, stop_event),
        daemon=True,
    )
    sock_thread.start()

    def on_winch(signum, frame):
        try:
            import fcntl
            import struct
            sz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, sz)
        except (OSError, ImportError):
            pass

    signal.signal(signal.SIGWINCH, on_winch)
    on_winch(None, None)

    try:
        proxy_loop(master_fd, stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        try:
            reg_file.unlink()
        except OSError:
            pass
        try:
            sock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    # ``python -m greatminds.cli.pty_launch ROLE EXEC_BINARY [args...]`` —
    # the direct invocation path used by start_agent. Click would strip
    # the ``--`` separator from variadic args here, breaking claude's
    # ``--mcp-config ... -- PROMPT`` contract. Parse argv ourselves
    # exactly when we're in the direct-exec form. ``--help`` / ``-h``
    # still go through the click handler for proper help text.
    if len(sys.argv) >= 3 and sys.argv[1] not in ("--help", "-h"):
        _pty_launch_impl(sys.argv[1], sys.argv[2], sys.argv[3:])
    else:
        pty_launch()
