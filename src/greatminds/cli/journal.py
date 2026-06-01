"""greatminds journal — read-only view of the coordination journal.

0338 (DOD2): the append-only ``coordination/journal.ndjson`` was only
reachable by a raw ``tail`` of the file, which the CLI-only coordination
access rule (0337) forbids. ``greatminds journal tail`` provides a
clean, read-only view with ``-n``/``--role``/``--task`` filters. It
NEVER writes.

Each journal line is a JSON object::

    {"t": <iso>, "actor"|"role": <ROLE>, "task": <id>,
     "from": <q>, "to": <q>, "reason": <str>, "intent_id": <str>}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir


JOURNAL_NAME = "journal.ndjson"
_SEQ_RE = re.compile(r"^(\d{1,4})")


def _entry_role(entry: dict) -> str:
    """The acting role — writers use ``actor`` (most) or ``role`` (mv)."""
    return str(entry.get("actor") or entry.get("role") or "")


def _role_matches(entry: dict, role: str) -> bool:
    return _entry_role(entry).upper() == role.upper()


def _seq(s: str) -> str | None:
    m = _SEQ_RE.match(s or "")
    return f"{int(m.group(1)):04d}" if m else None


def _task_matches(entry: dict, task_filter: str) -> bool:
    """Match a journal entry's ``task`` against the filter — exact,
    prefix, or same leading zero-padded seq (so ``0337`` matches a full
    ``0337-slug`` and vice-versa)."""
    t = str(entry.get("task") or "")
    if not t:
        return False
    if t == task_filter or t.startswith(task_filter):
        return True
    fs, ts = _seq(task_filter), _seq(t)
    return fs is not None and fs == ts


def _format(entry: dict) -> str:
    t = entry.get("t") or entry.get("ts") or "?"
    role = _entry_role(entry) or "?"
    task = entry.get("task") or "-"
    frm = entry.get("from") or ""
    to = entry.get("to") or ""
    arrow = f"{frm} → {to}" if (frm or to) else ""
    reason = entry.get("reason") or ""
    parts = [str(t), str(role), str(task)]
    if arrow:
        parts.append(arrow)
    if reason:
        parts.append(f"({reason})")
    return "  ".join(parts)


@click.group(name="journal", help=__doc__)
def journal() -> None:
    pass


@journal.command(name="tail",
                 short_help="read-only view of recent journal entries")
@click.option("-n", "--num", "num", type=int, default=20,
              help="show the last N matching entries (default 20)")
@click.option("--role", "role", default=None,
              help="only entries by this acting role")
@click.option("--task", "task_filter", default=None,
              help="only entries for this task (short id / full id)")
@click.option("--project-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="project root containing coordination/ (default: cwd)")
def tail(num: int, role: str | None, task_filter: str | None,
         project_dir: Path | None) -> None:
    """Print the last ``-n`` journal entries (read-only), optionally
    filtered by ``--role`` and/or ``--task``."""
    if num <= 0:
        raise click.UsageError("-n/--num must be a positive integer")
    coord = find_coord_dir(project_dir) if project_dir else find_coord_dir()
    path = coord / JOURNAL_NAME
    if not path.is_file():
        raise GreatMindsError(f"journal not found at {path}", exit_code=2)
    matched: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if role is not None and not _role_matches(entry, role):
                continue
            if task_filter is not None and not _task_matches(entry, task_filter):
                continue
            matched.append(entry)
    for entry in matched[-num:]:
        click.echo(_format(entry))
