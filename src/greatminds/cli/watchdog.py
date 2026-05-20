#!/usr/bin/env python3
"""Read-only watchdog for the coordination filesystem.

Usage:
    watchdog [--project-dir <dir>] [--canon-dir <dir>] [--quiet]

Reports:
  - heartbeat files older than schema.watchdog.heartbeat_stale_seconds
  - orphaned intent files older than schema.watchdog.intent_orphan_seconds
  - tasks in active queues older than task_stale_in_active_queue_seconds
  - tasks in review queues older than task_stale_in_review_queue_seconds

ARCHITECT-REVIEWER is expected to run this at the start of every tick and
follow up on flagged items. The watchdog never moves files or alters state.

Exit code:
  0 always (informational tool). --quiet only prints sections with findings.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

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


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-watchdog`` in pyproject.toml."""
    from greatminds.core.paths import find_canon_dir

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--canon-dir",
        type=Path,
        default=None,
        help="canon data directory (default: packaged greatminds.data)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if args.canon_dir is None:
        args.canon_dir = find_canon_dir()

    # Resolve coordination/: if --project-dir already points at a coordination/
    # directory, use it directly; otherwise append coordination/.
    if args.project_dir.name == "coordination" and (args.project_dir / "stand_requests").is_dir():
        coord = args.project_dir
    else:
        coord = args.project_dir / "coordination"
    if not coord.is_dir():
        print(f"error: {coord} not found", file=sys.stderr)
        return 1

    schema = load_schema(args.canon_dir)
    thresholds = {**DEFAULT_THRESHOLDS, **(schema.get("watchdog") or {})}
    queues = (schema.get("queues") or {})

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
        print(f"STALE HEARTBEATS ({len(stale_heartbeats)}, threshold {fmt_age(threshold)}):")
        for name, age in stale_heartbeats:
            print(f"  {name}: last touched {fmt_age(age)} ago")
        print()
    elif not args.quiet:
        print(f"heartbeats: all fresh (threshold {fmt_age(threshold)})")

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
        print(f"ORPHANED INTENTS ({len(orphans)}, threshold {fmt_age(threshold)}):")
        for name, age in orphans:
            print(f"  {name}: {fmt_age(age)} old")
        print()
    elif not args.quiet:
        print(f"intent/: 0 orphans (threshold {fmt_age(threshold)})")

    # ---- Dead agent pids (registry says alive, /proc disagrees)
    registry_dir = coord / ".agent_registry"
    dead_pids: list[tuple[str, int, str]] = []  # (role, pid, started_at)
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
        print(f"DEAD AGENT PIDS ({len(dead_pids)}):")
        for role, pid, started in dead_pids:
            print(f"  {role}: pid={pid} dead (started_at: {started})")
        print()
    elif not args.quiet:
        print("agent pids: all alive")

    # ---- Stale tasks per queue
    active_threshold = thresholds["task_stale_in_active_queue_seconds"]
    review_threshold = thresholds["task_stale_in_review_queue_seconds"]
    stale_tasks: list[tuple[str, str, float]] = []  # (queue, name, age)

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
        print(f"STALE TASKS ({len(stale_tasks)}):")
        for queue, name, age in stale_tasks:
            t = review_threshold if queue in REVIEW_QUEUES else active_threshold
            print(f"  {queue}/{name}: {fmt_age(age)} old (threshold {fmt_age(t)})")
        print()
    elif not args.quiet:
        print("active queues: 0 stale tasks")

    if findings == 0 and not args.quiet:
        print("\nAll clear.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
