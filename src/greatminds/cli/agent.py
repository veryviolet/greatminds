"""0339 (DOD2): ``greatminds agent status [ROLE]`` — per-role process
diagnostics.

Replaces raw ``cat coordination/.agent_registry/<role>.json`` (and the
ad-hoc ``os.kill`` / ``stat heartbeat.<role>`` an operator would
otherwise run by hand) with one CLI that reports, per role:

  - pid           — the live tool pid recorded in the registry
  - alive         — whether that pid is currently running
  - session_id    — claude --resume id / codex thread id (empty = none yet)
  - venv          — VIRTUAL_ENV of the live process (best-effort, /proc)
  - heartbeat_age — seconds since heartbeat.<role> was last touched
  - input_sock    — coordd's keystroke-injection socket (present?/path)

``greatminds agent status`` reports every registered role; ``greatminds
agent status DEVELOPER`` reports one. ``--json`` emits a machine-readable
array (the cat-replacement surface for scripts).

Read-only: never mutates the registry. ``alive``/``venv`` derive from the
OS at call time, so a crashed agent shows ``alive: false`` even though its
stale registry file lingers — which is the whole point of the command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir
from greatminds.cli.coordd import (
    REGISTRY_DIR,
    heartbeat_age_seconds,
    read_registry,
)


def _pid_alive(pid: Any) -> bool:
    """True iff ``pid`` names a running process. PermissionError means
    the pid exists but is owned by another user → still alive."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _venv_of_pid(pid: Any) -> str | None:
    """Best-effort VIRTUAL_ENV of a live process via /proc/<pid>/environ.

    Returns None when the pid is dead, /proc is unavailable (non-Linux),
    the environ is unreadable (permissions), or VIRTUAL_ENV is unset."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        raw = Path(f"/proc/{pid_int}/environ").read_bytes()
    except (OSError, ValueError):
        return None
    for chunk in raw.split(b"\x00"):
        if chunk.startswith(b"VIRTUAL_ENV="):
            val = chunk[len(b"VIRTUAL_ENV="):].decode("utf-8", "replace")
            return val or None
    return None


def collect_agent_status(coord: Path, role: str) -> dict[str, Any]:
    """Build the status record for one role (lowercase or upper accepted).

    ``registered`` is False when no registry file exists; the other
    fields are then null/false so callers get a stable shape."""
    role_lower = role.lower()
    reg = read_registry(coord / REGISTRY_DIR, role_lower)
    if reg is None:
        return {
            "role": role.upper(), "registered": False, "pid": None,
            "alive": False, "tool": None, "session_id": None,
            "venv": None, "heartbeat_age": None, "input_sock": None,
        }
    pid = reg.get("pid")
    sock = reg.get("input_sock")
    sock_present = bool(sock) and Path(sock).exists() if sock else False
    age = heartbeat_age_seconds(coord, role_lower)
    return {
        "role": reg.get("role") or role.upper(),
        "registered": True,
        "pid": pid,
        "alive": _pid_alive(pid),
        "tool": reg.get("tool"),
        "session_id": reg.get("session_id") or None,
        "venv": _venv_of_pid(pid),
        "heartbeat_age": round(age, 1) if age is not None else None,
        "input_sock": sock or None,
        "input_sock_present": sock_present,
    }


def _registered_roles(coord: Path) -> list[str]:
    reg_dir = coord / REGISTRY_DIR
    if not reg_dir.is_dir():
        return []
    return sorted(f.stem for f in reg_dir.glob("*.json"))


def _fmt_age(age: float | None) -> str:
    return f"{age:.1f}s" if age is not None else "—"


def _render_human(rec: dict[str, Any]) -> str:
    if not rec["registered"]:
        return f"{rec['role']}: (not registered)"
    alive = "alive" if rec["alive"] else "DEAD"
    sock = "yes" if rec.get("input_sock_present") else (
        "stale-path" if rec.get("input_sock") else "none")
    return (
        f"{rec['role']}: pid={rec['pid']} {alive}"
        f"  tool={rec['tool'] or '—'}"
        f"  session={rec['session_id'] or '—'}"
        f"  venv={rec['venv'] or '—'}"
        f"  heartbeat={_fmt_age(rec['heartbeat_age'])}"
        f"  input_sock={sock}"
    )


@click.group(name="agent",
             help="per-agent process diagnostics (0339).")
def agent() -> None:
    pass


@agent.command(name="status",
               help="report pid/alive/session/venv/heartbeat/input_sock "
                    "for a role (or every registered role).")
@click.argument("role", required=False)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="emit a machine-readable JSON array (cat replacement).")
def agent_status(role: str | None, as_json: bool) -> None:
    coord = find_coord_dir()
    if role:
        roles = [role.lower()]
        reg_dir = coord / REGISTRY_DIR
        if not (reg_dir / f"{role.lower()}.json").is_file():
            # Still emit a stable (not-registered) record rather than
            # erroring — the operator asked a specific role's state.
            pass
    else:
        roles = _registered_roles(coord)
        if not roles:
            if as_json:
                click.echo("[]")
            else:
                click.echo("(no registered agents)")
            return

    records = [collect_agent_status(coord, r) for r in roles]
    if as_json:
        click.echo(json.dumps(records, indent=2))
    else:
        for rec in records:
            click.echo(_render_human(rec))
