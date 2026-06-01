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
    # 0330 (0311 Phase 5): per-lifecycle override of the heartbeat
    # stale threshold. A self-loop role (e.g. MAINTAINER) touches its
    # heartbeat once per long cadence (~1h), so the global 600s flags it
    # stale ~50min/hour while its pid is alive and ticking on schedule.
    # The canon schema maps self-loop → cadence+margin; a role may also
    # set roles.<ROLE>.heartbeat_stale_seconds for an explicit override.
    "heartbeat_stale_seconds_by_lifecycle": {},
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


def _heartbeat_threshold(hb_name: str, roles_cfg: dict,
                         by_lifecycle: dict, default: float) -> float:
    """0330: resolve the stale threshold for a ``heartbeat.<role>`` file.

    Precedence: explicit ``roles.<ROLE>.heartbeat_stale_seconds`` →
    ``heartbeat_stale_seconds_by_lifecycle[<role lifecycle>]`` → the
    global default. The heartbeat filename suffix is the lowercased
    (hyphenated) role name (e.g. ``heartbeat.stand-keeper`` →
    STAND-KEEPER); match it case-insensitively against the schema role
    keys. Unknown roles fall through to the default."""
    suffix = hb_name.split(".", 1)[1] if "." in hb_name else hb_name
    spec = None
    for role_key, role_spec in (roles_cfg or {}).items():
        if isinstance(role_key, str) and role_key.lower() == suffix.lower():
            spec = role_spec if isinstance(role_spec, dict) else None
            break
    if spec is not None:
        override = spec.get("heartbeat_stale_seconds")
        if isinstance(override, (int, float)) and not isinstance(override, bool):
            return float(override)
        lifecycle = spec.get("lifecycle")
        if isinstance(lifecycle, str) and lifecycle in (by_lifecycle or {}):
            try:
                return float(by_lifecycle[lifecycle])
            except (TypeError, ValueError):
                pass
    return float(default)


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

    # ---- Heartbeats (0330: per-role / per-lifecycle stale threshold)
    default_hb = thresholds["heartbeat_stale_seconds"]
    by_lifecycle = thresholds.get("heartbeat_stale_seconds_by_lifecycle") or {}
    roles_cfg = schema.get("roles") or {}
    threshold = default_hb  # reused by the all-fresh message below
    stale_heartbeats: list[tuple[str, float, float]] = []
    for hb in sorted(coord.glob("heartbeat.*")):
        if not hb.is_file():
            continue
        age = now - hb.stat().st_mtime
        thr = _heartbeat_threshold(hb.name, roles_cfg, by_lifecycle,
                                   default_hb)
        if age > thr:
            stale_heartbeats.append((hb.name, age, thr))

    if stale_heartbeats:
        findings += len(stale_heartbeats)
        warn(f"STALE HEARTBEATS ({len(stale_heartbeats)}):")
        for name, age, thr in stale_heartbeats:
            warn(f"  {name}: last touched {fmt_age(age)} ago "
                 f"(threshold {fmt_age(thr)})")
        click.echo()
    elif not quiet:
        info(f"heartbeats: all fresh (default threshold "
             f"{fmt_age(default_hb)}; per-lifecycle overrides apply)")

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

    # ---- 0185: orphan worktree sweep
    #
    # A worktree at <base_path>/<task-id>/ whose task_id is no longer
    # in any active queue is an orphan — left behind by an aborted
    # mv, a crashed agent, or pre-cutover state. Report (don't auto-
    # prune) so the operator sees the count + can run
    # `greatminds worktree prune` deliberately.
    orphan_worktrees: list[str] = []
    try:
        from greatminds.cli import worktree as wt_mod
        policy = wt_mod.load_worktree_policy()
        base = project_dir / policy.base_path
        if base.is_dir():
            active_ids: set[str] = set()
            for q in coord.iterdir():
                if not q.is_dir() or q.name.startswith("."):
                    continue
                if q.name in ("verified", "archive", "stand_done",
                              "inbox", "intent"):
                    continue
                for f in q.iterdir():
                    if f.suffix in (".yaml", ".md"):
                        active_ids.add(f.stem)
                        if len(f.stem) > 4:
                            active_ids.add(f.stem[:4])
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name not in active_ids:
                    orphan_worktrees.append(child.name)
    except Exception:
        pass

    if orphan_worktrees:
        findings += len(orphan_worktrees)
        warn(f"ORPHAN WORKTREES ({len(orphan_worktrees)}):")
        for name in orphan_worktrees:
            warn(f"  .worktrees/{name} (no active task — "
                 f"run `greatminds worktree prune`)")
        click.echo()
    elif not quiet:
        info("worktrees: 0 orphans")

    if findings == 0 and not quiet:
        ok("\nAll clear.")


if __name__ == "__main__":
    watchdog()  # allows `python -m greatminds.cli.watchdog --help`
