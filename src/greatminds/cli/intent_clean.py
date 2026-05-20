#!/usr/bin/env python3
"""intent-clean — remove orphan intent files whose task has already moved.

An intent file says "I'm about to mv <task> from <queue> to <queue>".
After the mv, the agent is supposed to delete its intent. UI-DEVELOPER
has been crashing mid-tick repeatedly and leaving intents behind. This
script garbage-collects them: if the task is NOT in the intent's `from`
queue any more, the mv has completed (or the task was withdrawn) and
the intent is stale.

Safety: only removes intents older than --min-age-sec (default 300).
Recently-created intents may be legitimate ongoing claims.

Usage:
  intent-clean [--project-dir <dir>] [--min-age-sec N] [--dry-run]

Exit code 0 always. Prints what was removed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def task_in_queue(coord: Path, queue: str, task_id: str) -> bool:
    """True if task_id.md exists in coord/queue/."""
    qdir = coord / queue
    if not qdir.is_dir():
        return False
    return (qdir / f"{task_id}.md").is_file()


def queue_name_from_intent_path(p: str) -> str:
    """'coordination/feature_plan/' -> 'feature_plan'."""
    s = p.strip().rstrip("/")
    if s.startswith("coordination/"):
        s = s[len("coordination/"):]
    return s


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-intent-clean`` in pyproject.toml."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-dir", type=Path, default=Path.cwd())
    ap.add_argument("--min-age-sec", type=int, default=300,
                    help="Only consider intents older than this. Default 300s.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    coord = args.project_dir / "coordination"
    if not coord.is_dir():
        if not args.quiet:
            print(f"intent-clean: error: {coord} not found", file=sys.stderr)
        return 0
    idir = coord / "intent"
    if not idir.is_dir():
        return 0

    now = time.time()
    removed: list[str] = []
    kept_active: list[str] = []
    kept_recent: list[str] = []

    for f in idir.glob("*.json"):
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age < args.min_age_sec:
            kept_recent.append(f.name)
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # malformed — leave it alone, don't delete arbitrary files
            continue
        if not isinstance(data, dict):
            continue
        task_id = data.get("task_id") or ""
        from_q = queue_name_from_intent_path(data.get("from") or "")
        if not task_id or not from_q:
            continue
        if task_in_queue(coord, from_q, task_id):
            # task is still where the intent says it was — mv did NOT happen,
            # intent is legitimate ongoing claim. Leave alone.
            kept_active.append(f.name)
            continue
        # task is no longer in from_q — mv done (or task withdrawn). Stale.
        if args.dry_run:
            removed.append(f.name)
        else:
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass

    if not args.quiet:
        print(f"intent-clean: removed={len(removed)} "
              f"kept_active={len(kept_active)} "
              f"kept_recent={len(kept_recent)}")
        for name in removed:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
