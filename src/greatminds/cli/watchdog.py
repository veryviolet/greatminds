"""Read-only watchdog for the coordination filesystem.

Reports:
  - heartbeat files older than ``schema.watchdog.heartbeat_stale_seconds``
  - orphaned intent files older than ``schema.watchdog.intent_orphan_seconds``
  - tasks in active queues older than ``task_stale_in_active_queue_seconds``
  - tasks in review queues older than ``task_stale_in_review_queue_seconds``
  - registry entries whose ``pid`` is no longer alive

ARCHITECT-REVIEWER is expected to run this at the start of every tick and
follow up on flagged items. The watchdog never moves files or alters state.

Exit code: 0 always (informational tool). ``--quiet`` only prints sections
with findings.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, ok, warn


DEFAULT_THRESHOLDS = {
    "heartbeat_stale_seconds": 600,
    "intent_orphan_seconds": 300,
    "task_stale_in_active_queue_seconds": 86400,
    "task_stale_in_review_queue_seconds": 43200,
}

REVIEW_QUEUES = {"feature_review", "feature_docs_review"}


def load_schema(canon_dir: Path) -> dict:
    schema_path = canon_dir / "schema.yaml"
    if not schema_path.exists():
        return {}
    try:
        return yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


@click.command(
    short_help="report stale heartbeats / orphan intents / dead pids / stale tasks",
    help=__doc__,
)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root containing coordination/ (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data directory (default: packaged greatminds.data)")
@click.option("--quiet", is_flag=True, help="only print sections with findings")
def watchdog(project_dir: Path | None, canon_dir: Path | None, quiet: bool) -> None:
    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    if project_dir.name == "coordination" and (project_dir / "stand_requests").is_dir():
        coord = project_dir
    else:
        coord = project_dir / "coordination"
    if not coord.is_dir():
        err(f"error: {coord} not found")
        raise click.exceptions.Exit(1)

    schema = load_schema(canon_dir)
    thresholds = {**DEFAULT_THRESHOLDS, **(schema.get("watchdog") or {})}
    queues = schema.get("queues") or {}

    now = time.time()
    findings = 0

    # ---- Heartbeats
    threshold = thresholds["heartbeat_stale_seconds"]
    stale_heartbeats: list[tuple[str, float]] = []
    for hb in sorted(coord.glob("heartbeat.*")):
        if not hb.is_file():
            continue
        age = now - hb.stat().st_mtime
        if age > threshold:
            stale_heartbeats.append((hb.name, age))

    if stale_heartbeats:
        findings += len(stale_heartbeats)
        warn(f"STALE HEARTBEATS ({len(stale_heartbeats)}, threshold {fmt_age(threshold)}):")
        for name, age in stale_heartbeats:
            warn(f"  {name}: last touched {fmt_age(age)} ago")
        click.echo()
    elif not quiet:
        info(f"heartbeats: all fresh (threshold {fmt_age(threshold)})")

    # ---- Orphaned intents
    threshold = thresholds["intent_orphan_seconds"]
    intent_dir = coord / "intent"
    orphans: list[tuple[str, float]] = []
    if intent_dir.is_dir():
        for f in sorted(intent_dir.glob("*.json")):
            age = now - f.stat().st_mtime
            if age > threshold:
                orphans.append((f.name, age))
    if orphans:
        findings += len(orphans)
        warn(f"ORPHANED INTENTS ({len(orphans)}, threshold {fmt_age(threshold)}):")
        for name, age in orphans:
            warn(f"  {name}: {fmt_age(age)} old")
        click.echo()
    elif not quiet:
        info(f"intent/: 0 orphans (threshold {fmt_age(threshold)})")

    # ---- Dead agent pids (registry says alive, /proc disagrees)
    registry_dir = coord / ".agent_registry"
    dead_pids: list[tuple[str, int, str]] = []
    if registry_dir.is_dir():
        for f in sorted(registry_dir.glob("*.json")):
            try:
                reg = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pid = reg.get("pid")
            try:
                pid_int = int(pid) if pid is not None else None
            except (TypeError, ValueError):
                continue
            if pid_int is None:
                continue
            try:
                os.kill(pid_int, 0)
            except (ProcessLookupError, PermissionError):
                dead_pids.append((f.stem, pid_int, str(reg.get("started_at", "?"))))
    if dead_pids:
        findings += len(dead_pids)
        warn(f"DEAD AGENT PIDS ({len(dead_pids)}):")
        for role, pid, started in dead_pids:
            warn(f"  {role}: pid={pid} dead (started_at: {started})")
        click.echo()
    elif not quiet:
        info("agent pids: all alive")

    # ---- Stale tasks per queue
    active_threshold = thresholds["task_stale_in_active_queue_seconds"]
    review_threshold = thresholds["task_stale_in_review_queue_seconds"]
    stale_tasks: list[tuple[str, str, float]] = []

    for queue_name, queue_meta in queues.items():
        if not isinstance(queue_meta, dict):
            continue
        if queue_meta.get("kind") != "active":
            continue
        d = coord / queue_name
        if not d.is_dir():
            continue
        threshold = review_threshold if queue_name in REVIEW_QUEUES else active_threshold
        for f in sorted(d.glob("*.md")):
            if f.name == "_TEMPLATE.md":
                continue
            age = now - f.stat().st_mtime
            if age > threshold:
                stale_tasks.append((queue_name, f.name, age))

    if stale_tasks:
        findings += len(stale_tasks)
        warn(f"STALE TASKS ({len(stale_tasks)}):")
        for queue, name, age in stale_tasks:
            t = review_threshold if queue in REVIEW_QUEUES else active_threshold
            warn(f"  {queue}/{name}: {fmt_age(age)} old (threshold {fmt_age(t)})")
        click.echo()
    elif not quiet:
        info("active queues: 0 stale tasks")

    if findings == 0 and not quiet:
        ok("\nAll clear.")


if __name__ == "__main__":
    watchdog()  # allows `python -m greatminds.cli.watchdog --help`
