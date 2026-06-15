"""Read-only driven-agent event stream.

``coordd`` writes short JSONL lifecycle events here; operators watch them in a
dedicated tmux pane. This is intentionally separate from ``dashboard``:
dashboard is a non-scrolling current-state view, while this command is the
chronological "what just happened" feed for driven roles.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import click

from greatminds.core.paths import find_coord_dir


EVENT_LOG_REL = Path(".events") / "driven.ndjson"

_RESET = "\033[0m"
_C = {
    "head": "\033[1m",
    "muted": "\033[90m",
    "heartbeat": "\033[36m",
    "accepted": "\033[34m",
    "completed": "\033[32m",
    "message": "\033[35m",
    "error": "\033[31m",
    "stand": "\033[33m",
}
_EVENT_COLOR = {
    "heartbeat": "heartbeat",
    "turn_start": "accepted",
    "turn_pending": "message",
    "turn_finish": "completed",
    "retry": "stand",
    "error": "error",
}


def event_log_path(coord: Path) -> Path:
    return coord / EVENT_LOG_REL


def append_event(
    coord: Path,
    *,
    event: str,
    role: str,
    tool: str = "",
    task: str = "",
    message: str = "",
    detail: str = "",
    log_path: str = "",
) -> None:
    """Best-effort append of one driven lifecycle event.

    Event logging must never break the scheduler. All filesystem errors are
    intentionally swallowed; the authoritative state remains the existing task,
    lock, stand, and turn-log files.
    """
    payload = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "at_epoch": time.time(),
        "event": event,
        "role": role.upper(),
    }
    for key, value in (
        ("tool", tool),
        ("task", task),
        ("message", message),
        ("detail", detail),
        ("log_path", log_path),
    ):
        if value:
            payload[key] = str(value)
    path = event_log_path(coord)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if limit > 0:
        lines = lines[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            data = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def _paint(text: str, key: str, color: bool) -> str:
    if not color:
        return text
    code = _C.get(key)
    return f"{code}{text}{_RESET}" if code else text


def format_event(event: dict[str, Any], *, color: bool = False,
                 width: int = 120) -> str:
    at = str(event.get("at") or "")[11:19] or "--:--:--"
    kind = str(event.get("event") or "event")
    role = str(event.get("role") or "UNKNOWN")
    tool = str(event.get("tool") or "")
    task = str(event.get("task") or "")
    msg = str(event.get("message") or "")
    detail = str(event.get("detail") or "")
    log_name = Path(str(event.get("log_path") or "")).name
    label = {
        "heartbeat": "heartbeat",
        "turn_start": "accepted",
        "turn_pending": "pending",
        "turn_finish": "completed",
        "retry": "retry",
        "error": "error",
    }.get(kind, kind)
    parts = [f"{at}", f"{role:<18}", f"{label:<10}"]
    if tool:
        parts.append(tool)
    if task:
        parts.append(task)
    if msg:
        parts.append(msg)
    if detail:
        parts.append(detail)
    if log_name:
        parts.append(f"log:{log_name}")
    line = "  ".join(parts)
    line = line if len(line) <= width else line[: max(0, width - 1)] + "…"
    return _paint(line, _EVENT_COLOR.get(kind, "muted"), color)


def render_events(events: Iterable[dict[str, Any]], *, color: bool = False,
                  width: int = 120) -> str:
    head = _paint("DRIVEN EVENTS", "head", color)
    lines = [head]
    count = 0
    for event in events:
        lines.append(format_event(event, color=color, width=width))
        count += 1
    if count == 0:
        lines.append(_paint("  no driven events yet", "muted", color))
    return "\n".join(lines) + "\n"


def _term_width(default: int = 120) -> int:
    import shutil
    try:
        return shutil.get_terminal_size((default, 40)).columns
    except OSError:
        return default


@click.command(name="driven-log",
               help="read-only driven-agent event stream. Ctrl-C to exit.")
@click.option("--lines", type=int, default=200, show_default=True,
              help="initial events to print.")
@click.option("--follow/--no-follow", default=True, show_default=True,
              help="follow new events.")
@click.option("--interval", type=float, default=1.0, show_default=True,
              help="poll interval while following.")
@click.option("--color/--no-color", "color", default=None,
              help="ANSI color. Default: auto (on when stdout is a TTY).")
def driven_log(lines: int, follow: bool, interval: float,
               color: bool | None) -> None:
    coord = find_coord_dir()
    path = event_log_path(coord)
    use_color = sys.stdout.isatty() if color is None else color
    width = _term_width()
    click.echo(render_events(read_events(path, limit=lines),
                             color=use_color, width=width), nl=False)
    if not follow:
        return
    try:
        pos = path.stat().st_size if path.exists() else 0
        while True:
            time.sleep(interval)
            if not path.exists():
                continue
            try:
                size = path.stat().st_size
                if size < pos:
                    pos = 0
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict):
                    click.echo(format_event(event, color=use_color, width=width))
    except KeyboardInterrupt:
        return
