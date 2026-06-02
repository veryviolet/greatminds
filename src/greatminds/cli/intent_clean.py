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


def reap_orphan_intents(coord: Path, min_age_sec: float = 300.0,
                        dry_run: bool = False,
                        log=None) -> dict[str, int]:
    """0345: garbage-collect orphaned intent files. Shared core used by
    the ``intent-clean`` CLI AND coordd's periodic reaper.

    An intent is reaped when it is older than ``min_age_sec`` AND its
    task no longer sits in the intent's ``from`` queue (the mv completed
    or the task was withdrawn). Recently-created intents and intents
    whose task is still in-flight are kept. ``log`` is an optional
    ``(level, message)`` callback (``level`` ∈ removed/dry/warn) for the
    CLI's coloured output; coordd passes None (silent).

    Returns ``{removed, kept_active, kept_recent}``.
    """
    counts = {"removed": 0, "kept_active": 0, "kept_recent": 0}
    idir = coord / "intent"
    if not idir.is_dir():
        return counts
    now = time.time()
    for f in sorted(idir.glob("*.json")):
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age < min_age_sec:
            counts["kept_recent"] += 1
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if log:
                log("warn", f"unparseable intent {f.name} (age {int(age)}s)")
            continue
        from_q = strip_coord_prefix(str(data.get("from", "")))
        task_id = str(data.get("task", ""))
        # Task still in its from-queue → a (long) operation is in
        # progress; do NOT delete.
        if from_q and task_id and task_in_queue(coord, from_q, task_id):
            counts["kept_active"] += 1
            continue
        if dry_run:
            if log:
                log("dry", f"would remove {f.name} (age {int(age)}s, "
                           f"from {from_q})")
        else:
            try:
                f.unlink()
            except OSError as exc:
                if log:
                    log("warn", f"cannot unlink {f.name}: {exc}")
                continue
            if log:
                log("removed", f"removed {f.name} (age {int(age)}s, "
                               f"from {from_q})")
        counts["removed"] += 1
    return counts


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

    def _log(level: str, message: str) -> None:
        if level == "removed":
            ok(f"  {message}")
        elif level == "dry":
            info(f"  [dry] {message}")
        else:
            warn(f"  {message}")

    counts = reap_orphan_intents(coord, float(min_age_sec),
                                 dry_run=dry_run, log=_log)
    if not quiet or counts["removed"]:
        info(f"intent-clean: removed={counts['removed']} "
             f"kept_active={counts['kept_active']} "
             f"kept_recent={counts['kept_recent']}")


if __name__ == "__main__":
    intent_clean()
