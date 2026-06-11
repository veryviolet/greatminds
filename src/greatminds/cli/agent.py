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
  - pane_state    — tmux-pane usability classification (0387): ``ok`` /
                    ``auth_prompt`` / ``login_timeout`` / ``trust_prompt``
                    / ``empty`` / null (pane not inspectable)
  - usable        — False when the pane sits at a pre-agent / auth-wedge
                    prompt (alive pid + input_sock but NOT at the agent
                    prompt); True when healthy; null when not inspected

``greatminds agent status`` reports every registered role; ``greatminds
agent status DEVELOPER`` reports one. ``--json`` emits a machine-readable
array (the cat-replacement surface for scripts).

0387: a Codex-backed role can sit at the Codex sign-in / "Login timed
out" prompt forever. Its tmux pid is alive and its input_sock exists, so
the bare ``alive input_sock=yes`` line read as healthy while the agent
silently ignored every queued wake (e.g. a user_feedback task that never
got triaged). ``agent status`` now captures the role's pane and flags
this wedge as ``usable=NO`` so the operator sees the auth block, not a
false "alive".

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


# ---------------------------------------------------------------------------
# 0387: "alive pid but not actually at the agent prompt" detection.
#
# A Codex-backed role can sit at the Codex sign-in / API-key / "Login
# timed out" prompt forever: the pane process (pid) is alive and the
# input_sock exists, so the old ``alive input_sock=yes`` line read as
# healthy while the agent silently ignored every queued wake. We capture
# the role's tmux pane and classify its visible content for known
# pre-agent / auth-wedge prompts. Detection is best-effort and
# CONSERVATIVE: only the explicit wedge signatures below flip ``usable``
# to False; ordinary agent output, a normal shell, or an unreadable pane
# leave usability healthy/unknown (no false positives for live agents).
# ---------------------------------------------------------------------------

# "Login timed out" is the strongest wedge signal — the codex auth flow
# gave up and the pane will not reach the agent prompt without operator
# action. Checked first so it wins over the generic sign-in banner.
_PANE_LOGIN_TIMEOUT_PATTERNS: tuple[str, ...] = ("Login timed out",)

# Codex pre-agent auth screen (sign-in / own-API-key entry). Any of these
# visible in the pane means the codex TUI never reached its agent prompt.
_PANE_AUTH_PROMPT_PATTERNS: tuple[str, ...] = (
    "Sign in with ChatGPT",
    "Provide your own API key",
    "Welcome to Codex",
)

# claude/codex first-run folder-trust dialog — also a pre-agent prompt
# the agent is wedged behind until someone answers it.
_PANE_TRUST_PROMPT_PATTERNS: tuple[str, ...] = (
    "Do you trust the files in this folder",
)

# The classifier states that mean "alive but NOT usable as an agent".
_PANE_WEDGE_STATES: frozenset[str] = frozenset(
    {"login_timeout", "auth_prompt", "trust_prompt"})

_UNSET = object()


def classify_pane_state(text: str | None) -> str:
    """Classify a captured tmux pane's content into one usability state.

    Returns one of:
      - ``"login_timeout"`` — codex auth gave up (``Login timed out``).
      - ``"auth_prompt"``   — codex sign-in / API-key screen (pre-agent).
      - ``"trust_prompt"``  — first-run folder-trust dialog (pre-agent).
      - ``"ok"``            — pane has content, matches no wedge signature.
      - ``"empty"``         — pane is blank / whitespace only.

    Only the first three are in ``_PANE_WEDGE_STATES`` (unusable);
    ``ok``/``empty`` are healthy or indeterminate and never mark a role
    unusable, so a normal agent or shell is never a false positive."""
    if not text or not text.strip():
        return "empty"
    if any(p in text for p in _PANE_LOGIN_TIMEOUT_PATTERNS):
        return "login_timeout"
    if any(p in text for p in _PANE_AUTH_PROMPT_PATTERNS):
        return "auth_prompt"
    if any(p in text for p in _PANE_TRUST_PROMPT_PATTERNS):
        return "trust_prompt"
    return "ok"


def _pane_text_for_role(coord: Path, role_lower: str) -> str | None:
    """Best-effort capture of a role's tmux pane, or None when the pane
    can't be located/captured (no coord.yaml window, tmux absent, empty
    capture). None → pane_state unknown → usability is left untouched."""
    try:
        from greatminds.cli.coordd import (
            _read_coord_yaml, _window_and_tool_for_role,
        )
        from greatminds.cli._send_enter import _capture_pane
        coord_yaml = _read_coord_yaml(coord.parent)
        if not coord_yaml:
            return None
        located = _window_and_tool_for_role(coord_yaml, role_lower)
        if not located or not located[0]:
            return None
        session = (coord_yaml.get("session") or "").strip()
        if not session:
            return None
        text = _capture_pane(session, located[0])
        # _capture_pane returns "" for both an unreachable pane and a
        # genuinely blank one — treat either as "unknown" (None) so a
        # missing tmux server never reads as a wedge or a healthy agent.
        return text or None
    except Exception:
        return None


def _usability(pane_state: str | None) -> bool | None:
    """Map a pane_state to ``usable``: None when not inspected (unknown),
    False for the wedge states, True otherwise."""
    if pane_state is None:
        return None
    return pane_state not in _PANE_WEDGE_STATES


def collect_agent_status(coord: Path, role: str,
                         *, pane_text: Any = _UNSET) -> dict[str, Any]:
    """Build the status record for one role (lowercase or upper accepted).

    ``registered`` is False when no registry file exists; the other
    fields are then null/false so callers get a stable shape.

    Pane inspection (0387): by default the role's tmux pane is captured
    live to classify ``pane_state`` / ``usable``. Pass ``pane_text``
    explicitly (a string, or None to skip inspection) to override — used
    by tests and by the ``--no-pane`` CLI flag."""
    role_lower = role.lower()
    reg = read_registry(coord / REGISTRY_DIR, role_lower)
    if reg is None:
        return {
            "role": role.upper(), "registered": False, "pid": None,
            "alive": False, "tool": None, "session_id": None,
            "venv": None, "heartbeat_age": None, "input_sock": None,
            "pane_state": None, "usable": None,
        }
    pid = reg.get("pid")
    sock = reg.get("input_sock")
    sock_present = bool(sock) and Path(sock).exists() if sock else False
    age = heartbeat_age_seconds(coord, role_lower)
    if pane_text is _UNSET:
        pane = _pane_text_for_role(coord, role_lower)
    else:
        pane = pane_text
    pane_state = classify_pane_state(pane) if pane is not None else None
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
        "pane_state": pane_state,
        "usable": _usability(pane_state),
    }


# ---------------------------------------------------------------------------
# 0388: consume the 0387 wedge detection at the resume/readiness boundary.
#
# 0387 made ``agent status`` / ``watchdog`` SURFACE a wedged role (alive
# pid but stuck at a pre-agent / auth prompt). Nothing CONSUMED that
# signal, so a blocked review_session whose objective needs a live role
# (e.g. a PLANNER to drive user_feedback → planning) could still be
# unblocked and resumed against the wedged stand — and rediscover the
# exact same wedge. These helpers let wake-check (advisory) and the
# feature_blocked → resume validator (enforcing) hold such a task until
# the required role is usable, with an actionable reason.
# ---------------------------------------------------------------------------

def required_live_roles(header: dict[str, Any],
                        blocked_block: "dict[str, Any] | None") -> list[str]:
    """Roles a task's objective needs USABLE before it may resume.

    Opt-in: read from the latest blocked block's ``requires_live_roles``
    (override) else the task header's. Absent / non-list → empty list, so
    tasks that don't declare the field behave exactly as before (no new
    holds, no regression). Normalized upper-case, de-duped,
    order-preserving."""
    raw: Any = None
    if blocked_block is not None and blocked_block.get("requires_live_roles") is not None:
        raw = blocked_block.get("requires_live_roles")
    elif header.get("requires_live_roles") is not None:
        raw = header.get("requires_live_roles")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        if not isinstance(r, str):
            continue
        u = r.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def wedged_required_roles(coord: Path, roles: "list[str]",
                          *, pane_texts: "dict[str, Any] | None" = None
                          ) -> list[tuple[str, str]]:
    """Return ``(role, pane_state)`` for each required role that is
    DEFINITIVELY wedged (``usable is False`` — alive but at a pre-agent /
    auth prompt per 0387).

    CONSERVATIVE / fail-open: a role whose usability is True (healthy) or
    None (not registered / pane not inspectable) is NOT held, and any
    error inspecting a role is swallowed (an infra hiccup — missing tmux,
    unreadable registry — must never wedge the FSM). Only an explicit
    wedge holds the resume.

    ``pane_texts`` injects ``{ROLE_UPPER: text|None}`` for deterministic
    tests (bypasses live tmux capture)."""
    wedged: list[tuple[str, str]] = []
    for role in roles:
        try:
            if pane_texts is not None and role in pane_texts:
                rec = collect_agent_status(coord, role,
                                           pane_text=pane_texts[role])
            else:
                rec = collect_agent_status(coord, role)
        except Exception:
            continue
        if rec.get("usable") is False:
            wedged.append((rec.get("role") or role,
                           str(rec.get("pane_state"))))
    return wedged


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
    line = (
        f"{rec['role']}: pid={rec['pid']} {alive}"
        f"  tool={rec['tool'] or '—'}"
        f"  session={rec['session_id'] or '—'}"
        f"  venv={rec['venv'] or '—'}"
        f"  heartbeat={_fmt_age(rec['heartbeat_age'])}"
        f"  input_sock={sock}"
    )
    # 0387: a wedged pane (alive pid but stuck at a pre-agent / auth
    # prompt) must NOT read as healthy. Make it loud so the operator
    # sees the block instead of trusting the bare ``alive``.
    if rec.get("usable") is False:
        line += f"  ⚠ USABLE=NO(pane:{rec.get('pane_state')})"
    return line


@click.group(name="agent",
             help="per-agent process diagnostics (0339).")
def agent() -> None:
    pass


@agent.command(name="status",
               help="report pid/alive/session/venv/heartbeat/input_sock "
                    "for one or more roles (or every registered role).")
@click.argument("roles", nargs=-1)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="emit a machine-readable JSON array (cat replacement).")
@click.option("--no-pane", "no_pane", is_flag=True, default=False,
              help="skip tmux pane inspection (0387 auth-wedge detection).")
def agent_status(roles: tuple[str, ...], as_json: bool,
                 no_pane: bool) -> None:
    coord = find_coord_dir()
    if roles:
        # Explicit roles (one or many) — emit a stable record per role
        # even if not registered, since the operator named it.
        role_list = [r.lower() for r in roles]
    else:
        role_list = _registered_roles(coord)
        if not role_list:
            if as_json:
                click.echo("[]")
            else:
                click.echo("(no registered agents)")
            return

    # pane_text=None skips inspection (pane_state/usable left unknown);
    # the _UNSET default triggers live capture.
    pane_arg = None if no_pane else _UNSET
    records = [collect_agent_status(coord, r, pane_text=pane_arg)
               for r in role_list]
    if as_json:
        click.echo(json.dumps(records, indent=2))
    else:
        for rec in records:
            click.echo(_render_human(rec))
