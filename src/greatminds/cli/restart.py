"""greatminds restart — idempotent fleet restart.

Replaces the temporary ``restart_fleet.sh`` bash crutch with a tested
Python implementation. Same external behavior, same order:

  1. ``systemctl --user is-active --quiet coordd``; if not, start it.
  2. ``tmux has-session -t <session>``; if missing, shell out to
     ``greatminds launch --target tmux``.
  3. For each window in ``coord.yaml`` with a non-empty ``role``,
     resolve ``coordination/.agent_registry/<role-lowercase>.json``:
       - missing / pid dead / no pid → ``tmux send-keys Enter`` to
         (re)launch the agent (its window already runs a wrapper that
         waits on Enter).
       - alive → skip.
  4. Wait 10s, then re-read each registry. A role passes if the file
     exists, ``pid`` is alive (``os.kill(pid, 0)``), and ``input_sock``
     is present. Exit 0 if all roles pass; exit 1 otherwise.

Usage::

    greatminds restart [--config <coord.yaml>] [--project-dir <dir>]

Linux + systemd-user only, same as the bash version.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import click
import yaml

from greatminds.cli._colors import err, info


SESSION_DEFAULT = "greatminds-dev"
VERIFY_WAIT_SEC = 10


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    info(f"[{ts}] {msg}")


def _load_coord_yaml(path: Path) -> dict:
    if not path.is_file():
        err(f"coord.yaml not found at {path}")
        raise click.exceptions.Exit(1)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        err(f"coord.yaml parse error: {exc}")
        raise click.exceptions.Exit(1)
    if not isinstance(data, dict):
        err("coord.yaml root must be a mapping")
        raise click.exceptions.Exit(1)
    return data


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True,
    )


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True,
    )


def _ensure_coordd() -> None:
    _log("==> coordd: ensure running")
    if _systemctl("is-active", "--quiet", "coordd").returncode == 0:
        main_pid = _systemctl(
            "show", "-p", "MainPID", "--value", "coordd"
        ).stdout.strip()
        _log(f"    coordd: active ({main_pid})")
        return
    _log("    coordd not active, starting...")
    started = _systemctl("start", "coordd")
    if started.returncode != 0:
        err(
            "    ERROR: systemctl --user start coordd failed: "
            f"{started.stderr.strip()}"
        )
        raise click.exceptions.Exit(1)
    time.sleep(1)
    if _systemctl("is-active", "--quiet", "coordd").returncode != 0:
        err(
            "    ERROR: coordd failed to start — "
            "inspect `journalctl --user -u coordd`"
        )
        raise click.exceptions.Exit(1)
    main_pid = _systemctl(
        "show", "-p", "MainPID", "--value", "coordd"
    ).stdout.strip()
    _log(f"    coordd: active ({main_pid})")


def _ensure_tmux_session(session: str, project_dir: Path) -> None:
    _log(f"==> tmux session {session}: ensure exists")
    if _tmux("has-session", "-t", session).returncode == 0:
        _log(f"    session {session} already exists")
        return
    _log("    session missing, calling greatminds launch --target tmux")
    launched = subprocess.run(
        ["greatminds", "launch", "--target", "tmux"],
        cwd=str(project_dir),
        capture_output=True, text=True,
    )
    if launched.returncode != 0:
        err(
            "    ERROR: greatminds launch --target tmux failed: "
            f"{launched.stderr.strip()}"
        )
        raise click.exceptions.Exit(1)
    time.sleep(1)


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _load_registry(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _iter_role_windows(windows: list[dict]) -> list[tuple[str, str]]:
    """Yield (window_name, role_lower) pairs, skipping windows with empty role."""
    out: list[tuple[str, str]] = []
    for w in windows:
        if not isinstance(w, dict):
            continue
        name = (w.get("name") or "").strip()
        role = (w.get("role") or "").strip()
        if not name or not role:
            continue
        out.append((name, role.lower()))
    return out


def _restart_dead_agents(
    registry_dir: Path,
    windows: list[dict],
    session: str,
) -> None:
    _log("==> agents: check + restart dead ones")
    for name, role_lc in _iter_role_windows(windows):
        reg_path = registry_dir / f"{role_lc}.json"
        data = _load_registry(reg_path)
        pid = 0
        needs_start = False
        if data is None:
            needs_start = True
        else:
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if not _pid_alive(pid):
                try:
                    reg_path.unlink()
                except OSError:
                    pass
                needs_start = True
        if needs_start:
            _log(f"    {name} ({role_lc}): sending Enter to (re)start")
            _tmux("send-keys", "-t", f"{session}:{name}", "Enter")
            time.sleep(0.5)
        else:
            _log(f"    {name} ({role_lc}): pid={pid} alive, skip")


def _verify(
    registry_dir: Path,
    windows: list[dict],
    wait_sec: int,
    session: str,
) -> int:
    _log(f"==> waiting {wait_sec}s for pty_launch sockets to bind...")
    time.sleep(wait_sec)
    _log("==> final registry state:")
    fail = 0
    total = 0
    for _name, role_lc in _iter_role_windows(windows):
        total += 1
        reg_path = registry_dir / f"{role_lc}.json"
        data = _load_registry(reg_path)
        if data is None:
            click.echo(f"    {role_lc:<22} MISSING")
            fail += 1
            continue
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        has_sock = "YES" if "input_sock" in data else "NO"
        alive = "alive" if _pid_alive(pid) else "DEAD"
        click.echo(
            f"    {role_lc:<22} pid={pid:<7} {alive:<6} input_sock={has_sock}"
        )
        if has_sock != "YES":
            fail += 1
        if alive != "alive":
            fail += 1
    click.echo()
    if fail == 0:
        _log(f"==> ALL {total} agents up with input_sock bound. Fleet ready.")
        _log(f"    attach with: tmux a -t {session}")
        return 0
    _log(f"==> {fail} role(s) failed to come up clean. Inspect:")
    _log(f"    tmux a -t {session}")
    _log("    journalctl --user -u coordd | tail -30")
    return 1


@click.command(
    "restart",
    short_help="idempotent fleet restart (coordd + tmux session + agents)",
    help=__doc__,
)
@click.option(
    "--config", "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="path to coord.yaml (default: <project>/coord.yaml)",
)
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="override config.project_dir / cwd",
)
def restart(config_path: Path | None, project_dir: Path | None) -> None:
    if config_path is None:
        for p in (Path.cwd() / "coord.yaml",
                  Path.cwd() / "coordination" / "coord.yaml"):
            if p.is_file():
                config_path = p
                break
    if config_path is None or not config_path.is_file():
        err("coord.yaml not found (pass --config or run from project root)")
        raise click.exceptions.Exit(1)

    cfg = _load_coord_yaml(config_path)
    project_dir = (
        project_dir or Path(cfg.get("project_dir") or ".")
    ).resolve()
    if not project_dir.is_dir():
        err(f"project_dir {project_dir} not found")
        raise click.exceptions.Exit(1)

    session = cfg.get("session") or SESSION_DEFAULT
    windows = cfg.get("windows") or []
    if not isinstance(windows, list) or not windows:
        err("coord.yaml: windows must be a non-empty list")
        raise click.exceptions.Exit(1)

    registry_dir = project_dir / "coordination" / ".agent_registry"

    _ensure_coordd()
    _ensure_tmux_session(session, project_dir)
    _restart_dead_agents(registry_dir, windows, session)
    rc = _verify(registry_dir, windows, VERIFY_WAIT_SEC, session)
    if rc != 0:
        raise click.exceptions.Exit(rc)


if __name__ == "__main__":
    restart()
