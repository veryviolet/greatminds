"""intent-clean — remove orphan intent files whose task has already moved.

An intent file says "I'm about to mv <task> from <queue> to <queue>".
After the mv, the agent is supposed to delete its intent. If an agent
crashes mid-tick, its intent file is left behind. This command garbage-
collects them: if the task is no longer in the intent's ``from`` queue,
the mv has completed (or the task was withdrawn) and the intent is
stale.

Safety: only removes intents older than ``--min-age-sec`` (default 300).
Recently-created intents may be legitimate ongoing claims.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import click

from greatminds.cli._colors import info, ok, warn


def task_in_queue(coord: Path, queue: str, task_id: str) -> bool:
    """True if ``task_id.{md,yaml}`` exists in ``coord/queue/``."""
    qdir = coord / queue
    if not qdir.is_dir():
        return False
    return any((qdir / f"{task_id}.{ext}").is_file() for ext in ("yaml", "md"))


def strip_coord_prefix(s: str) -> str:
    if s.startswith("coordination/"):
        s = s[len("coordination/"):]
    return s


@click.command(name="intent-clean", short_help="garbage-collect orphaned intent files", help=__doc__)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root (default: cwd)")
@click.option("--min-age-sec", type=int, default=300,
              help="only consider intents older than this; default 300s")
@click.option("--dry-run", is_flag=True, help="report what would be removed; don't delete")
@click.option("--quiet", is_flag=True, help="suppress summary line on no-ops")
def intent_clean(project_dir: Path | None, min_age_sec: int, dry_run: bool, quiet: bool) -> None:
    project_dir = project_dir or Path.cwd()
    coord = project_dir / "coordination"
    if not coord.is_dir():
        if not quiet:
            warn(f"intent-clean: {coord} not found")
        return
    idir = coord / "intent"
    if not idir.is_dir():
        if not quiet:
            info("intent/: no directory, nothing to do")
        return

    now = time.time()
    removed = 0
    kept_active = 0
    kept_recent = 0
    for f in sorted(idir.glob("*.json")):
        age = now - f.stat().st_mtime
        if age < min_age_sec:
            kept_recent += 1
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            warn(f"  unparseable intent {f.name} (age {int(age)}s)")
            continue
        from_q = strip_coord_prefix(str(data.get("from", "")))
        task_id = str(data.get("task", ""))
        # If from-queue resolves and the task still sits there, the agent
        # is in the middle of a (long) operation; do NOT delete.
        if from_q and task_id and task_in_queue(coord, from_q, task_id):
            kept_active += 1
            continue
        if dry_run:
            info(f"  [dry] would remove {f.name} (age {int(age)}s, from {from_q})")
        else:
            try:
                f.unlink()
                ok(f"  removed {f.name} (age {int(age)}s, from {from_q})")
            except OSError as exc:
                warn(f"  cannot unlink {f.name}: {exc}")
                continue
        removed += 1

    if not quiet or removed:
        info(f"intent-clean: removed={removed} kept_active={kept_active} kept_recent={kept_recent}")


if __name__ == "__main__":
    intent_clean()
