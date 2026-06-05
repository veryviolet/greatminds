"""``greatminds dashboard`` — read-only live fleet status table.

A non-scrolling console view of three things the operator keeps asking
for at a glance: what each agent is doing, the state of active tasks,
and the singleton stand. PURE OBSERVER — reads only ``coordination/``
filesystem state, holds no role, registers nothing, sends no wakes.
Safe to kill and restart; if it dies nothing else notices.

Data sources (all read-only):
  - agents: ``collect_agent_status`` (registry pid/alive/heartbeat) +
    the ``coord.yaml`` window roster (role/tool/mode) + schema lifecycle
    + the driven run-lock presence (``.locks/driven-<role>.lock``).
  - tasks: task files across the active queues → id + title; the owning
    role comes from ``schema.queues.<q>.owner``.
  - stand: ``.stand/state.yaml`` via ``stand_state.read_stand_state``.

"What an agent is doing" is necessarily INFERRED — there is no field
that records an agent's current thought. We derive it from: a held
run-lock (driven turn in flight) → heartbeat freshness (working vs
idle) → and the task sitting in the role's owned queue. Honest limits,
spelled out so nobody reads more certainty into the column than exists.

Collection is split from rendering: ``collect_snapshot()`` returns plain
data, ``render_dashboard()`` turns it into a string. The CLI loop clears
the screen and reprints each frame; ``--once`` prints a single frame
(used by the tests and by scripting).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir, find_coord_dir


# Active task queues in FSM-pipeline order (terminal verified/archive are
# excluded — the dashboard shows work in flight, not the graveyard).
ORDERED_TASK_QUEUES = [
    "feature_inbox", "feature_plan", "feature_dev", "feature_ui_dev",
    "feature_docs", "feature_test", "feature_docs_review", "feature_live",
    "feature_review", "feature_blocked", "review_sessions", "user_feedback",
]

# ANSI colors (used only when color is enabled).
_RESET = "\033[0m"
_C = {
    "alive": "\033[32m",    # green
    "idle": "\033[33m",     # yellow
    "dead": "\033[90m",     # bright black (grey)
    "staged": "\033[36m",   # cyan
    "head": "\033[1m",      # bold
    "rule": "\033[90m",     # grey rule
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_age(seconds: float | None) -> str:
    """Compact heartbeat age: 'fresh' (<60s), '7m', '21h', '3d', '—'."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return "fresh"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _paint(text: str, key: str, color: bool) -> str:
    if not color or key not in _C:
        return text
    return f"{_C[key]}{text}{_RESET}"


def _clip(line: str, width: int) -> str:
    """Clip a (non-ANSI) line to width. Render builds plain strings and
    only colorizes whole cells, so clipping happens before paint."""
    return line if len(line) <= width else line[: max(0, width - 1)] + "…"


# ---------------------------------------------------------------------------
# Collection (pure: takes coord + now, returns plain data)
# ---------------------------------------------------------------------------


def _fleet_roster(coord_yaml: dict | None) -> list[dict[str, str]]:
    """Roles to show, in coord.yaml window order. Role-less windows
    (the dashboard/ops bash panes) are skipped."""
    roster: list[dict[str, str]] = []
    for w in (coord_yaml or {}).get("windows") or []:
        if not isinstance(w, dict):
            continue
        role = (w.get("role") or "").strip()
        if not role:
            continue
        roster.append({
            "role": role.upper(),
            "tool": (w.get("tool") or "").strip().lower(),
            "mode": (w.get("mode") or "").strip().lower(),
        })
    return roster


def _agent_state(rec: dict[str, Any], mode: str, lifecycle: str,
                 running: bool) -> str:
    """Coherent STATE token. Driven roles are NEVER 'dead': they have no
    persistent process by design — coordd runs each turn on an event, so
    between turns they are 'idle', and during a (live) turn 'running'.
    Only roles that SHOULD hold a persistent agent (interactive /
    self-loop) are 'dead' when their pid is gone."""
    if lifecycle == "driven":
        return "running" if running else "idle"
    if rec["alive"]:
        return "alive"
    if mode == "staged":
        return "staged"
    return "dead"


def _agent_doing(rec: dict[str, Any], mode: str, lifecycle: str,
                 driven_turn: bool, fresh_sec: float,
                 claimed: list[str]) -> str:
    """Inferred activity word. The owned task is attached ONLY while the
    role is actively on it (running a turn / fresh heartbeat). An idle
    role is NOT working its queue's task — showing it there read as
    'still on X' after the turn ended; the task still appears in the
    TASKS table below."""
    task_suffix = f" · {claimed[0]}" + (f" (+{len(claimed)-1})"
                                        if len(claimed) > 1 else "") \
        if claimed else ""
    if driven_turn:
        return f"running turn{task_suffix}"
    if lifecycle == "driven":
        # idle-by-design between turns — STATE already shows 'idle'; don't
        # attribute the queue's task here (it read as "still on X").
        return "—"
    if not rec["alive"]:
        if mode == "staged":
            return "awaiting USER start"
        return "—"
    age = rec.get("heartbeat_age")
    if age is not None and age < fresh_sec:
        return f"working{task_suffix}"
    return "idle"


def collect_agents(coord: Path, coord_yaml: dict | None, canon_dir: Path,
                   tasks_by_owner: dict[str, list[str]]) -> list[dict[str, Any]]:
    from greatminds.cli.agent import collect_agent_status
    from greatminds.cli.coordd import (
        PUSH_FRESH_GUARD_SEC, _driven_run_lock_path, _lifecycle_for_role,
    )

    rows: list[dict[str, Any]] = []
    for entry in _fleet_roster(coord_yaml):
        role = entry["role"]
        rec = collect_agent_status(coord, role)
        lifecycle = _lifecycle_for_role(canon_dir, role) or ""
        lock = _driven_run_lock_path(coord, role.lower())
        hb_age = rec.get("heartbeat_age")
        # The run-lock IS the authoritative "turn in flight" marker: coordd
        # holds it for the turn's duration and clears stale locks on
        # startup (1.5.15), so while coordd is alive a present lock means a
        # real running turn. A driven claude turn has no persistent
        # registered pid / heartbeat, so do NOT gate on those — that hid a
        # genuinely-running turn as "idle".
        driven_turn = lifecycle == "driven" and lock.is_file()
        claimed = tasks_by_owner.get(role, [])
        # A driven turn does NOT refresh a heartbeat, so the HB column would
        # show a misleading stale age while the turn runs ("running · 8m" reads
        # as a hang). When a turn is in flight, show its DURATION instead (the
        # run-lock's age) prefixed with ⟳ — the real "how long active" signal,
        # which also surfaces a genuinely hung turn (e.g. ⟳31m).
        if driven_turn:
            try:
                turn_age = time.time() - lock.stat().st_mtime
                hb_display = "⟳" + _fmt_age(turn_age)
            except OSError:
                hb_display = _fmt_age(hb_age)
        else:
            hb_display = _fmt_age(hb_age)
        rows.append({
            "role": role,
            "tool": entry["tool"] or (rec.get("tool") or "—"),
            "lifecycle": lifecycle or "—",
            "mode": entry["mode"],
            "alive": rec["alive"],
            "registered": rec["registered"],
            "state": _agent_state(rec, entry["mode"], lifecycle, driven_turn),
            "heartbeat": hb_display,
            "doing": _agent_doing(rec, entry["mode"], lifecycle,
                                  driven_turn, PUSH_FRESH_GUARD_SEC, claimed),
        })
    return rows


def collect_tasks(coord: Path) -> list[dict[str, Any]]:
    """Active tasks across queues, in pipeline order. Each row: id,
    queue (=FSM state), owner role, title."""
    from greatminds.cli.task import load_task, queue_meta

    rows: list[dict[str, Any]] = []
    for q in ORDERED_TASK_QUEUES:
        qdir = coord / q
        if not qdir.is_dir():
            continue
        try:
            owner = (queue_meta(q).get("owner") or "").upper()
        except GreatMindsError:
            owner = ""
        for f in sorted(qdir.iterdir()):
            if f.suffix not in (".yaml", ".md"):
                continue
            if f.name.startswith(("_TEMPLATE", "processed-")):
                continue
            try:
                data = load_task(f)
            except GreatMindsError:
                data = {}
            rows.append({
                "id": (data.get("id") or f.stem),
                "queue": q,
                "owner": owner,
                "title": (data.get("title") or "").strip(),
            })
    return rows


def collect_stand(coord: Path) -> dict[str, Any]:
    from greatminds.cli import stand_state as ss
    state = ss.read_stand_state(coord)
    active = state.get("active_lease") or {}
    queue = state.get("queue") or []
    return {
        "state": state.get("state") or "—",
        "holder": (active.get("holder_role") or "").upper(),
        "lease": (active.get("lease_id") or "")[:8],
        "task": active.get("task") or "",
        "queue_len": len(queue),
        "queue_next": (queue[0].get("task") if queue else ""),
        "last_change_at": state.get("last_state_change_at") or "",
        "last_change_by": state.get("last_state_change_by") or "",
        "down_reason": state.get("down_reason") or "",
    }


def collect_snapshot(coord: Path) -> dict[str, Any]:
    coord_yaml = _read_coord_yaml_safe(coord)
    try:
        canon_dir = find_canon_dir()
    except Exception:
        canon_dir = coord
    tasks = collect_tasks(coord)
    by_owner: dict[str, list[str]] = {}
    for t in tasks:
        by_owner.setdefault(t["owner"], []).append(t["id"])
    return {
        "session": (coord_yaml or {}).get("session") or "greatminds",
        "agents": collect_agents(coord, coord_yaml, canon_dir, by_owner),
        "tasks": tasks,
        "stand": collect_stand(coord),
    }


def _read_coord_yaml_safe(coord: Path) -> dict | None:
    from greatminds.cli.coordd import _read_coord_yaml
    # coord is .../coordination; coord.yaml lives there or at the project root.
    for base in (coord.parent, coord):
        doc = _read_coord_yaml(base)
        if doc is not None:
            return doc
    return None


# ---------------------------------------------------------------------------
# Rendering (pure: snapshot -> string)
# ---------------------------------------------------------------------------


def _rule(width: int, color: bool) -> str:
    return _paint("─" * width, "rule", color)


def _render_agents(rows: list[dict[str, Any]], width: int, color: bool) -> list[str]:
    out = [_paint("AGENTS", "head", color)]
    hdr = f"{'ROLE':<19}{'TOOL':<7}{'LIFECYCLE':<13}{'STATE':<12}{'HB':<7}DOING"
    out.append(_clip(hdr, width))
    glyph = {
        "alive":   ("●", "alive"),
        "running": ("●", "alive"),
        "idle":    ("◦", "idle"),    # driven, between turns — normal
        "staged":  ("◌", "staged"),
        "dead":    ("○", "dead"),
    }
    for r in rows:
        state = r.get("state") or ("alive" if r["alive"] else "dead")
        dot, key = glyph.get(state, ("○", "dead"))
        # STATE column is 12 wide (header). The glyph + space take 2, so the
        # state string is padded to 10 → 12 total, keeping HB aligned. Using
        # <7 collided HB with longer states ("running" → "runningfresh").
        line = (f"{r['role']:<19}{r['tool']:<7}{r['lifecycle']:<13}"
                f"{dot} {state:<10}{r['heartbeat']:<7}{r['doing']}")
        out.append(_paint(_clip(line, width), key, color))
    return out


def _task_num(tid: Any) -> str:
    """Just the leading task number (``0001`` from
    ``0001-verify-full-deploy-…``) — the long slug blew the ID column and
    broke alignment; the number alone identifies the task in the table."""
    head = str(tid).split("-", 1)[0]
    return head if head.isdigit() else str(tid)


def _render_tasks(rows: list[dict[str, Any]], width: int, color: bool) -> list[str]:
    out = [_paint(f"TASKS  (active: {len(rows)})", "head", color)]
    if not rows:
        out.append(_paint("  no active tasks", "dead", color))
        return out
    out.append(_clip(f"{'ID':<6}{'STATE':<18}{'OWNER':<19}TITLE", width))
    for r in rows:
        line = (f"{_task_num(r['id']):<6}{r['queue']:<18}{r['owner']:<19}"
                f"{r['title']}")
        out.append(_clip(line, width))
    return out


def _render_stand(st: dict[str, Any], width: int, color: bool) -> list[str]:
    key = {"free": "alive", "ready": "alive", "preparing": "idle",
           "down": "dead"}.get(st["state"], "idle")
    head = _paint("STAND", "head", color)
    dot = _paint("●", key, color)
    if st["holder"]:
        lease = f"lease {st['lease']} · {st['holder']}"
        if st["task"]:
            lease += f" · {st['task']}"
    else:
        lease = "lease: none"
    qpart = (f"queue: {st['queue_len']} (next {st['queue_next']})"
             if st["queue_len"] else "queue: empty")
    out = [f"{head}  {dot} {st['state']:<10}{lease} · {qpart}"]
    if st["last_change_at"]:
        out.append(_clip(f"       last change: {st['last_change_at']} "
                         f"by {st['last_change_by']}", width))
    if st["down_reason"]:
        # down_reason may carry a multi-line ansible log (PLAY/TASK ***
        # banners + newlines) — collapse to a single clean line so it
        # doesn't shred the panel.
        reason = " ".join(str(st["down_reason"]).split())
        out.append(_clip(f"       down: {reason}", width))
    return out


def render_dashboard(snapshot: dict[str, Any], width: int = 100,
                     now_str: str = "", color: bool = False) -> str:
    width = max(40, width)
    title = f"{snapshot['session']}"
    header = title if not now_str else f"{title} · {now_str}"
    lines: list[str] = [_clip(_paint(header, "head", color), width),
                        _rule(width, color)]
    lines += _render_agents(snapshot["agents"], width, color)
    lines.append(_rule(width, color))
    lines += _render_tasks(snapshot["tasks"], width, color)
    lines.append(_rule(width, color))
    lines += _render_stand(snapshot["stand"], width, color)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _term_width(default: int = 100) -> int:
    import shutil
    try:
        return shutil.get_terminal_size((default, 40)).columns
    except OSError:
        return default


@click.command(name="dashboard",
               help="live read-only fleet status table (agents / tasks / "
                    "stand). Non-scrolling; Ctrl-C to exit.")
@click.option("--interval", type=float, default=2.0, show_default=True,
              help="refresh interval in seconds.")
@click.option("--once", is_flag=True, default=False,
              help="print a single frame and exit (no clear, no loop).")
@click.option("--color/--no-color", "color", default=None,
              help="ANSI color. Default: auto (on when stdout is a TTY).")
def dashboard(interval: float, once: bool, color: bool | None) -> None:
    coord = find_coord_dir()
    use_color = sys.stdout.isatty() if color is None else color

    def frame() -> str:
        now_str = time.strftime("%H:%M:%S")
        width = _term_width()
        return render_dashboard(collect_snapshot(coord), width, now_str,
                                use_color)

    if once:
        click.echo(frame(), nl=False)
        return

    # Live loop: hide cursor, clear once, then home+clear-below each frame
    # (no full-screen wipe → minimal flicker, no scrollback growth).
    if use_color:
        sys.stdout.write("\033[?25l")
    sys.stdout.write("\033[2J")
    try:
        while True:
            sys.stdout.write("\033[H\033[J" + frame())
            sys.stdout.flush()
            time.sleep(max(0.2, interval))
    except KeyboardInterrupt:
        pass
    finally:
        if use_color:
            sys.stdout.write("\033[?25h")  # restore cursor
        sys.stdout.flush()
